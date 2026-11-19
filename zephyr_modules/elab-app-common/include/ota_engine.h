/*
 * ota_engine —— OTA 升级协议层（平台无关）
 *
 * 这里放"任何芯片、任何 RTOS 上的 OTA 接收端都必须做同一件事"的部分：
 *   1) 协议原语：找 HTTP 头结束、取整型/十六进制字段、定长 hex 比较与编码、分段算术
 *   2) 请求门禁（ota_req_check）：四条硬规则 —— 头齐全 / 带期望摘要 / 鉴权 / 体积 / 防重放 / 版本
 *   3) 会话推进（ota_sess_run）：把 body 按 2 KB 一块吃进来，逐块交给后端写，直到收满
 *   4) 失败语义：每一类失败对应的 HTTP 码与 JSON token（协议文本属于协议层）
 *
 * 刻意不做的事（留在 app 侧，因为那是"策略"或"平台"）：
 *   · 谁来跑这个 socket / 线程模型      → 平台
 *   · 写到哪里、怎么写、怎么擦         → 平台（flash_area / mcuboot / 裸 flash / 文件系统）
 *   · 整包摘要用哪套实现、回读怎么比   → 平台（PSA / 硬件 SHA / mbedtls）
 *   · 断点记录存在哪、凭什么算可续传   → 平台（分区 / EEPROM / 文件）
 *   · 用哪个 PSK、要不要校验           → 产品策略（以回调注入）
 *   · 切槽、确认、回滚                 → bootloader 契约（mcuboot 专属）
 *
 * 换一颗芯片做 OTA，重写的是 app 侧那几个 ota_backend 回调；
 * 这个文件与它的实现可以原样搬走。
 */
#ifndef OTA_ENGINE_H_
#define OTA_ENGINE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ---------------------------------------------------------------- 会话阶段
 * 顺序就是一次成功升级的时间线；fail 是终态，可从任意阶段进入。 */
enum ota_phase {
	OTA_PHASE_IDLE = 0,
	OTA_PHASE_HEAD,      /* 正在收请求头 */
	OTA_PHASE_RECV,      /* 正在收固件体 */
	OTA_PHASE_VERIFY,    /* 收到尾包，校验整包摘要 */
	OTA_PHASE_DONE,      /* 已落盘，等 reboot */
	OTA_PHASE_FAIL,
	OTA_PHASE_COUNT
};

const char *ota_phase_str(enum ota_phase ph);

/* ------------------------------------------------------------ 协议原语 */
int ota_find_hdr_end(const uint8_t *buf, size_t len);
const char *ota_hdr_get(const char *hdr, const char *name);
uint32_t ota_hdr_get_u32(const char *hdr, const char *name, uint32_t dflt);
size_t ota_hdr_get_hex(const char *hdr, const char *name, char *dst, size_t dst_len);

/* 定长、不提前返回的比较：避免"第几个字节不同"从耗时里泄出去。 */
bool ota_hex_eq(const char *a, const char *b, size_t n);
void ota_hex_encode(const uint8_t *in, size_t n, char *out);

/* 分段（resume checkpoint）算术：以"段"为单位记进度，断电最多丢一段 */
uint32_t ota_seg_index(uint32_t off, uint32_t seg_size);
uint32_t ota_seg_base(uint32_t seg_idx, uint32_t seg_size);
uint32_t ota_seg_count(uint32_t total, uint32_t seg_size);

/* ------------------------------------------------------------ 请求门禁 */
/* 从请求头解析出来的、本次升级关心的全部字段 */
struct ota_req {
	uint32_t total;      /* Content-Length：固件体字节数 */
	uint32_t ver;        /* X-Version */
	uint32_t nonce;      /* X-Nonce：严格递增，防重放 */
	bool force;          /* X-Force：只允许越过"版本不升"这一条 */
	bool resume;         /* X-Resume: 1 → 发起端要求续传握手 */
	char sha[65];        /* X-Sha256：发起端声明的整包摘要（64 hex + '\0'） */
};

/* 门禁结果。顺序与判定顺序一致，app 只需把它翻成"日志 + 一个 JSON"。 */
enum ota_check {
	OTA_CHECK_OK = 0,
	OTA_CHECK_NO_HDR,    /* Content-Length / X-Version / X-Nonce 缺失 */
	OTA_CHECK_NO_SHA,    /* X-Sha256 缺失或不是 64 个 hex */
	OTA_CHECK_AUTH,      /* 鉴权失败（HMAC 或 X-Key 不对） */
	OTA_CHECK_TOO_BIG,   /* 超过槽容量 */
	OTA_CHECK_REPLAY,    /* nonce 不递增 */
	OTA_CHECK_OLD_VER,   /* 版本不比本机新，且没带 force */
};

/* 鉴权以回调注入：PSK、HMAC 实现、X-Key 校验都在平台/产品侧。
 * 返回 true 表示放行。auth 传 NULL 表示"不做鉴权"（自测用，生产别这么配）。 */
typedef bool (*ota_auth_cb)(void *ctx, const char *hdr, const char *canonical);

/* 解析 + 逐条过门禁。why 里写"人话原因"（含数字），给日志用。
 * 判定顺序是有意的：鉴权放在体积/版本门禁之前 —— 未授权方连"包有多大"都问不出来。 */
enum ota_check ota_req_check(const char *hdr, uint32_t local_ver, uint32_t slot_max,
			     uint32_t last_nonce, ota_auth_cb auth, void *auth_ctx,
			     struct ota_req *out, char *why, size_t why_len);

/* 门禁 → HTTP 码 / JSON token（协议文本只在这一处定义，app 不许另写一份） */
uint16_t ota_check_http(enum ota_check c);
const char *ota_check_token(enum ota_check c);

/* ------------------------------------------------------------ 后端接口 */
struct ota_backend {
	/* 开一包。返回值语义：
	 *   < 0  → 失败（错误码）
	 *   = 0  → 全量上传，从 0 写
	 *   > 0  → 可续传，返回断点偏移（引擎会把 body 从该偏移继续吃）
	 * 实现里该做的：初始化存储、起整包/分段摘要上下文、判"上次的记录能不能用"。 */
	int32_t (*begin)(void *ctx, const struct ota_req *req);

	/* 吃一段数据：写存储 + 累积整包摘要 + 累积分段摘要。
	 * 失败时把 err_msg 填成人话，返回负错误码。 */
	int (*write)(void *ctx, const uint8_t *data, size_t len);

	/* 收尾：刷缓存、结束整包摘要、与 req->sha 比对、回读校验。
	 * 0 = 通过（此后 app 才允许切槽）。 */
	int (*finish)(void *ctx, const struct ota_req *req);

	/* 放弃本次会话：释放摘要上下文与断点记录（不断链也要调）。 */
	void (*abort)(void *ctx);

	void *ctx;
	char err_msg[64];    /* begin/write/finish 失败时由后端填：人话原因 */
	const char *err_token; /* 失败时 JSON 里的 err 值（"flush"/"hash"/"flash write"…） */
};

/* 收数据的来源（socket / UDP / 文件 / 测试桩），返回 0 表示成功读到 *got 字节 */
typedef int (*ota_recv_cb)(void *ctx, uint8_t *buf, size_t want, size_t *got);

/* ------------------------------------------------------------ 会话推进 */
struct ota_sess {
	enum ota_phase phase;
	uint32_t total;      /* 本次要收的固件体字节数 */
	uint32_t ver;
	uint32_t nonce;
	uint32_t written;    /* 已接收（含续传起点） */
	uint32_t start_off;  /* 本次实际起点；吞吐的分母要用"增量字节" */
	int32_t err;         /* 失败时的错误码 */
	char err_msg[64];    /* 失败时的人话原因 */
	uint16_t http_code;  /* 失败时该回的 HTTP 码 */
	const char *token;   /* 失败时 JSON 里的 err 值 */
};

void ota_sess_init(struct ota_sess *s, const struct ota_req *req);

/* 把 body 全部消费掉（pre 是"读完请求头时顺手带进来的一截 body"）。
 * 返回 0 表示收满；负值表示失败，此时 s->err / err_msg / http_code / token 都已填好。
 * 不碰 socket、不碰 flash —— 两件事分别由 recv_cb 与 backend 提供。 */
int ota_sess_run(struct ota_sess *s, const struct ota_backend *be,
		 ota_recv_cb recv, void *recv_ctx,
		 const uint8_t *pre, size_t pre_len);

/* 收尾（调 be->finish 并把失败信息写进 sess）。返回 0 表示整包校验通过。 */
int ota_sess_finish(struct ota_sess *s, const struct ota_backend *be,
		    const struct ota_req *req);

/* 放弃（调 be->abort，并把 phase 置 FAIL） */
void ota_sess_abort(struct ota_sess *s, const struct ota_backend *be);

#endif /* OTA_ENGINE_H_ */
