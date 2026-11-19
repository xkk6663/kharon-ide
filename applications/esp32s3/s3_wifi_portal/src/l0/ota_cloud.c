/*
 * ota_cloud —— 公网 OTA 拉取端（HTTPS，只出站）
 *
 * 通道选择 pull 而不是 push 的原因：设备在 NAT 后面，公网不可直达；
 * 让设备主动连 VPS 拿 OFFER/固件，防火墙只需要出站 8443，板子零监听。
 * VPS（cloudhub.py）只搬运不裁决 —— PSK 不出设备、不存 VPS，HMAC 由
 * PC 发布端算好放进 OFFER，设备端独立验。
 *
 * 与局域网通道的关系：写槽后端（ota_backend_get）、门禁（ota_req_check +
 * ota_auth_ok）、升级收尾（ota_arm_upgrade）全部复用，见 ota.h 的导出原语。
 * 两个通道互斥 —— 局域网 OTA 在跑时这里直接退避，下个周期再试。
 *
 * 安全清单：
 *   T1  TLS1.2 起（IPPROTO_TLS_1_2），CA 内嵌（hub_ca.inc，证书固定）
 *   T2  TLS_HOSTNAME 必须设成 IP —— mbedTLS 靠它匹配服务器证书的 IP SAN，
 *       不设等于关掉主机名校验，中间人拿任意受信证书就能过
 *   T3  X-Dev-Token 经 NVS 写入（portal otoken），不进源码/不进 git
 *   T4  HMAC 覆盖 ver|size|sha256|nonce|force；nonce 单调递增持久化
 *   T5  版本显式递增 + 槽容量硬门禁（公网通道不给 force 弱口令档）
 *   T6  OFFER.dev 必须等于本机 dev_id，发给别人家的 OFFER 直接丢弃
 *   T7  整包 SHA-256 比对 + flash 回读（backend finish 内），不符不切槽
 *   T8  report 里不回传固件内容、不回传 token，只回结果与版本
 *
 * 分层纪律：l0 不许 include l2 的 wifi_link.h —— 链路状态由 main.c 的
 * 周期 work 喂进来（ota_cloud_link_feed），连上瞬间顺带触发首次 check。
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/settings/settings.h>
#include <zephyr/data/json.h>
#include <zephyr/sys/reboot.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ota.h"
#include "ota_cloud.h"
#include "ota_engine.h"
#include "ops.h"
#include "cloud_ops.h"

LOG_MODULE_REGISTER(ota_cloud, LOG_LEVEL_INF);

#include "hub_ca.inc"

/* 前向声明：K_THREAD_DEFINE 引用的线程入口在文件后半段定义 */
void ota_cloud_worker(void *a, void *b, void *c);

/* ---------------------------------------------------------------- 配置 */
#define HUB_HOST      "120.76.230.17"
#define HUB_PORT      8443
#define HUB_SEC_TAG   1

/* 设备 ID：云上 devices 表的 dev_id，也是 MQTT ACL 的用户名。不敏感。 */
#define CLOUD_DEV_ID  "s3-0001"

/* 心跳/巡检周期。原先是死写 15 min —— 对"远程下发一条 reboot"来说太长，
 * 现在默认 3 min，并支持云端 `cfg_set hb=<sec>` 在 60..3600 s 内调整。
 * 失败按 1→2→5→10→15 min 退避（见 backoff_ms），避免服务端 5xx 时无意义重试。 */
#define CHECK_PERIOD_MS_DEF  (3u * 60u * 1000u)
#define FAST_PERIOD_MS       (60u * 1000u)
#define BACKOFF_MAX_MS       (15u * 60u * 1000u)

static const uint32_t backoff_ms[] = {
	60u * 1000u, 2u * 60u * 1000u, 5u * 60u * 1000u, 10u * 60u * 1000u,
};

#define RX_STACK 12288

/* OFFER JSON 上限：9 个字段，file/sha/auth 都是定长类，够用 */
/* OFFER 应答上限：v3 应答是 {dev,proto,cmd,cmdname,cmdargs,+OFFER 六字段}，
 * 实测 300~400 B。1024 留足余量，同时省掉 1 KB 静态 ram（dram 已到 99%+）。 */
#define OFFER_BUF_MAX 1024

/* ---------------------------------------------------------------- 状态 */
enum cloud_st {
	CLOUD_IDLE = 0,
	CLOUD_CHECK,     /* 正在 check */
	CLOUD_GATE,      /* 门禁判定中 */
	CLOUD_DL,        /* 正在下载 */
	CLOUD_VERIFY,    /* 整包校验 + 切槽 */
	CLOUD_REPORT,    /* 上报结果 */
	CLOUD_COUNT
};

static struct {
	volatile int st;
	volatile int32_t last_err;     /* 最后一次失败的 errno 类值 */
	volatile uint32_t offer_ver;   /* 最近一次处理的 OFFER 版本 */
	volatile uint32_t dl_bytes;    /* 已写入字节数（下载中实时） */
	volatile uint32_t dl_total;    /* 声明的整包长度 */
	uint32_t last_check_ms;        /* 上次 check 的 k_uptime（s，快照用） */
	char why[96];                  /* 最后一次失败的人话原因 */
} g_cl;

static char g_dev_token[52];   /* X-Dev-Token：NVS s3cloud/token，不进源码 */
static bool g_token_ok;
static volatile bool g_link_up;   /* main.c 周期喂进来的链路状态 */
static volatile bool g_force_next; /* portal ocheck 置位，绕过节流 */

/* 节奏与命令记账 */
static uint32_t g_period_ms = CHECK_PERIOD_MS_DEF;  /* 云端可调（cfg_set hb=） */
static uint32_t g_fast_until_s;                     /* 命令下行后的快轮询窗口 */
static uint8_t g_fail_cnt;                          /* 连续失败次数 → 退避 */
static uint32_t g_last_cmd_id;                      /* NVS s3cloud/lastcmd：幂等 */
static volatile int32_t g_last_cmd_rc;              /* 最近一次命令结果（快照用） */

/* 专属线程 + 踢醒信号量。check/下载/report 全部在这个线程里跑（阻塞式
 * socket + TLS 握手不能碰系统工作队列）。 */
K_THREAD_DEFINE(cloud_thread, RX_STACK, ota_cloud_worker,
		NULL, NULL, NULL, 7, 0, 0);
K_SEM_DEFINE(cloud_kick, 0, 1);

/* ---------------------------------------------------------------- NVS token */
/* s3cloud 子树里两把钥匙：
 *   token    —— X-Dev-Token（l1 的 portal otoken 写）
 *   lastcmd  —— 最近执行过的命令 id（幂等：云端重推同一条不会再执行一次）
 * 其余键一律忽略，避免以后新增键时被错当 token 读进来。 */
static int cloud_settings_set(const char *name, size_t len, settings_read_cb read_cb,
			      void *cb_arg)
{
	if (strcmp(name, "lastcmd") == 0) {
		char tmp[16] = "";

		if (len == 0 || len >= sizeof(tmp)) {
			return 0;
		}
		if (read_cb(cb_arg, tmp, len) < 0) {
			return 0;
		}
		tmp[len] = '\0';
		g_last_cmd_id = (uint32_t)strtoul(tmp, NULL, 10);
		LOG_INF("lastcmd loaded from NVS (%u)", g_last_cmd_id);
		return 0;
	}

	if (strcmp(name, "token") != 0) {
		return 0;
	}
	if (len == 0 || len >= sizeof(g_dev_token)) {
		LOG_WRN("dev_token size %u out of range, ignored", (unsigned)len);
		return 0;
	}
	ssize_t n = read_cb(cb_arg, g_dev_token, len);

	if (n < 0) {
		return n;
	}
	g_dev_token[n] = '\0';
	g_token_ok = true;
	LOG_INF("dev_token loaded from NVS (%u B)", (unsigned)n);
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ota_cloud, "s3cloud", NULL, cloud_settings_set, NULL, NULL);

/* shell 写入（l1 的 portal otoken 调这里）。token 不打日志。 */
int ota_cloud_token_set(const char *hex)
{
	if (hex == NULL || strlen(hex) < 16 || strlen(hex) >= sizeof(g_dev_token)) {
		return -EINVAL;
	}
	int rc = settings_save_one("s3cloud/token", hex, strlen(hex));

	if (rc == 0) {
		strncpy(g_dev_token, hex, sizeof(g_dev_token) - 1);
		g_dev_token[sizeof(g_dev_token) - 1] = '\0';
		g_token_ok = true;
	}
	return rc;
}

/* ---------------------------------------------------------------- TLS */
static int hub_connect(void)
{
	int fd = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TLS_1_2);

	if (fd < 0) {
		return -errno;
	}

	/* T1/T2：CA 内嵌 + 主机名（IP）校验 —— 这两条缺一不可 */
	int tag = HUB_SEC_TAG;
	int rc = zsock_setsockopt(fd, SOL_TLS, TLS_SEC_TAG_LIST, &tag, sizeof(tag));

	if (rc) {
		goto out;
	}
	rc = zsock_setsockopt(fd, SOL_TLS, TLS_HOSTNAME, HUB_HOST, strlen(HUB_HOST));
	if (rc) {
		goto out;
	}

	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(HUB_PORT) };

	rc = zsock_inet_pton(AF_INET, HUB_HOST, &addr.sin_addr);
	if (rc != 1) {
		rc = -EINVAL;
		goto out;
	}

	rc = zsock_connect(fd, (struct sockaddr *)&addr, sizeof(addr));
	if (rc) {
		rc = -errno;
		goto out;
	}

	/* 单次 recv/send 兜底 30 s：云端进程卡死（accept 了却不应答）时，
	 * 不能让线程无限期挂在 send/recv 上 —— 那会把 st 钉在 busy 态。 */
	struct timeval tv = { .tv_sec = 30, .tv_usec = 0 };

	zsock_setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	zsock_setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	return fd;

out:
	zsock_close(fd);
	return rc;
}

static int hub_send_all(int fd, const char *buf, size_t len)
{
	size_t off = 0;

	while (off < len) {
		ssize_t n = zsock_send(fd, buf + off, len - off, 0);

		if (n <= 0) {
			return n < 0 ? -errno : -ECONNRESET;
		}
		off += (size_t)n;
	}
	return 0;
}

static void hub_close(int *fd)
{
	if (*fd >= 0) {
		zsock_close(*fd);
		*fd = -1;
	}
}

/* ---------------------------------------------------------------- 小 HTTP */
struct resp {
	char head[1024];    /* 响应头（\r\n\r\n 前） */
	size_t head_len;
	uint8_t pre[512];   /* 头结束后顺手带进来的一截 body */
	size_t pre_len;
	uint16_t code;
	uint32_t content_len;
};

/* 全模块共用一份响应结构：所有云端 HTTP 都在 cloud_thread 里串行执行
 * （check → shadow → download → report 没有交叠），4 个函数各留一份
 * static 副本纯属浪费 —— 每份 1552 B，4 份就是 6.2 KB。 */
static struct resp g_resp;

/* 收响应头。返回 0 = 拿到完整头（pre 里可能有 body 前段）；负值失败。 */
static int hub_recv_head(int fd, struct resp *r)
{
	memset(r, 0, sizeof(*r));
	size_t cap = sizeof(r->head) - 1;

	while (r->head_len < cap) {
		ssize_t n = zsock_recv(fd, (uint8_t *)r->head + r->head_len,
				       cap - r->head_len, 0);

		if (n <= 0) {
			return n < 0 ? -errno : -ECONNRESET;
		}
		r->head_len += (size_t)n;
		r->head[r->head_len] = '\0';

		char *end = strstr(r->head, "\r\n\r\n");

		if (end) {
			size_t hdr_total = (size_t)(end - r->head) + 4;
			size_t extra = r->head_len - hdr_total;

			/* 头后面可能已经带了一截 body（cloudhub 16KB 一块，概率高） */
			if (extra > sizeof(r->pre)) {
				extra = sizeof(r->pre);
			}
			if (extra) {
				memcpy(r->pre, end + 4, extra);
				r->pre_len = extra;
			}
			r->head_len = hdr_total;
			r->head[hdr_total] = '\0';
			break;
		}
	}

	/* 状态行 + Content-Length。cloudhub 是自家服务器，字段大小写固定，
	 * 不做大小写不敏感匹配（minimal libc 没有 strcasestr）。 */
	if (strncmp(r->head, "HTTP/1.1 ", 9) != 0) {
		return -EPROTO;
	}
	r->code = (uint16_t)atoi(r->head + 9);
	const char *cl = strstr(r->head, "Content-Length:");

	if (cl) {
		r->content_len = (uint32_t)strtoul(cl + 15, NULL, 10);
	}
	return 0;
}

/* ---------------------------------------------------------------- OFFER */
struct cloud_offer {
	int32_t proto;
	int32_t ver;
	int32_t size;
	int32_t nonce;
	int32_t force;
	int32_t cmd;        /* 命令 id（0/缺省 = 本轮无命令） */
	char *cmdname;      /* 白名单命令名 */
	char *cmdargs;      /* 参数，可为空串 */
	char *dev;
	char *file;
	char *sha256;
	char *auth;
};

static const struct json_obj_descr offer_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, proto, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, ver, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, size, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, nonce, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, force, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, cmd, JSON_TOK_NUMBER),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, cmdname, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, cmdargs, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, dev, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, file, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, sha256, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct cloud_offer, auth, JSON_TOK_STRING),
};

/* T6/T7 的本机关联性 + 基本形状。鉴权/版本/nonce 门禁由 ota_req_check 统一做
 *（这里不做第二份，避免两通道口径分叉）。 */
static int offer_sanity(const struct cloud_offer *o, char *why, size_t why_n)
{
	if (o->proto != OTA_PROTO_VER) {
		snprintf(why, why_n, "proto %d != %d", (int)o->proto, OTA_PROTO_VER);
		return -EPROTO;
	}
	if (o->dev == NULL || strcmp(o->dev, CLOUD_DEV_ID) != 0) {
		snprintf(why, why_n, "offer is for another dev (%s)",
			 o->dev ? o->dev : "?");
		return -EACCES;
	}
	if (o->file == NULL || o->sha256 == NULL || o->auth == NULL) {
		snprintf(why, why_n, "missing file/sha/auth field");
		return -EPROTO;
	}
	if (strlen(o->sha256) != 64) {
		snprintf(why, why_n, "sha256 not 64 hex");
		return -EPROTO;
	}
	if (o->ver <= 0 || o->size <= 0 || o->nonce <= 0) {
		snprintf(why, why_n, "bad ver/size/nonce (%d/%d/%d)",
			 (int)o->ver, (int)o->size, (int)o->nonce);
		return -EPROTO;
	}
	return 0;
}

/* 门禁：把 OFFER 字段翻成局域网协议的伪头，走同一个 ota_req_check。
 * canonical 由 PC 发布端拼好（"ver|size|sha256|nonce|force"）放进 auth，
 * ota_auth_ok 只管比对 —— 这就是"两端同一把尺"。 */
static int offer_gate(const struct cloud_offer *o, struct ota_req *req,
		      char *why, size_t why_n)
{
	char hdr[384];
	enum ota_check chk;

	snprintf(hdr, sizeof(hdr),
		 "\r\nContent-Length: %d\r\nX-Version: %d\r\nX-Sha256: %s\r\nX-Nonce: %d\r\nX-Force: %d\r\nX-Auth: %s\r\n",
		 (int)o->size, (int)o->ver, o->sha256, (int)o->nonce,
		 o->force ? 1 : 0, o->auth);

	chk = ota_req_check(hdr, OTA_LOCAL_VERSION, OTA_SLOT_MAX_BYTES,
			    ota_nonce_get(), ota_auth_ok, NULL, req, why, why_n);
	if (chk != OTA_CHECK_OK) {
		/* why 里已经是人话；把 check 码也带上方便对表 */
		size_t n = strlen(why);

		snprintf(why + n, why_n - n, " [check=%d]", (int)chk);
		return -EACCES;
	}
	return 0;
}

/* ---------------------------------------------------------------- 上报 */
static int hub_report(int ver, const char *result, const char *note)
{
	char req[640];
	char body[192];
	int fd = hub_connect();

	if (fd < 0) {
		LOG_WRN("report: connect failed (%d)", fd);
		return fd;
	}
	snprintf(body, sizeof(body), "{\"ver\":%d,\"result\":\"%s\",\"note\":\"%s\"}",
		 ver, result, note);
	snprintf(req, sizeof(req),
		 "POST /v1/ota/report?dev=%s HTTP/1.1\r\n"
		 "Host: %s:%d\r\nX-Dev-Token: %s\r\n"
		 "Content-Type: application/json\r\nContent-Length: %d\r\n"
		 "Connection: close\r\n\r\n%s",
		 CLOUD_DEV_ID, HUB_HOST, HUB_PORT, g_dev_token,
		 (int)strlen(body), body);
	int rc = hub_send_all(fd, req, strlen(req));

	if (rc == 0) {
		rc = hub_recv_head(fd, &g_resp);
		if (rc == 0 && g_resp.code != 200) {
			LOG_WRN("report: HTTP %u", g_resp.code);
		}
	}
	hub_close(&fd);
	return rc;
}

/* ---------------------------------------------------------------- 影子 / 命令回执 */
static const char *st_name(void)
{
	static const char *const names[] = {
		"idle", "check", "gate", "dl", "verify", "report"
	};
	int st = g_cl.st < 0 || g_cl.st >= CLOUD_COUNT ? 0 : g_cl.st;

	return names[st];
}

/* JSON 字符串内容消毒：引号/反斜杠转义，控制字符变空格。
 * 影子里的 why 来自我们的格式化串（理论上干净），但走一遍更安全，
 * 否则一个引号就能把整份 JSON 弄坏、云端解析失败。 */
static size_t json_sanitize(char *dst, size_t n, const char *src)
{
	size_t i = 0;
	size_t o = 0;

	if (dst == NULL || n == 0) {
		return 0;
	}
	while (src != NULL && src[i] != '\0' && o + 3 < n) {
		char c = src[i++];

		if (c == '"' || c == '\\') {
			dst[o++] = '\\';
			dst[o++] = c;
		} else if ((unsigned char)c < 0x20) {
			dst[o++] = ' ';
		} else {
			dst[o++] = c;
		}
	}
	dst[o] = '\0';
	return o;
}

/* POST /v1/shadow：每轮 check 前上报一次快照（云端 upsert，只保留最新）。
 * 单独一条短连接（不复用 check 的连接）：实现简单、语义清楚，
 * 多出来的一次握手由 CONFIG_MBEDTLS_SSL_SESSION_TICKETS 的会话复用吸收。 */
static int cloud_shadow(void)
{
	static char body[448];
	static char req[768];
	static char env[192];
	static char why[64];
	int fd = hub_connect();

	if (fd < 0) {
		return fd;   /* 影子失败不影响本轮 OTA；worker 只记日志 */
	}
	json_sanitize(why, sizeof(why), g_cl.why);
	cloud_ops_env_json(env, sizeof(env));

	snprintf(body, sizeof(body),
		 "{\"dev\":\"%s\",\"proto\":%d,\"ver\":%d,\"boot\":%u,\"crash\":%u,"
		 "\"up\":%u%s,\"stage\":\"%s\",\"err\":\"%s\",\"last\":%u,"
		 "\"cmdid\":%u,\"hbm\":%u}",
		 CLOUD_DEV_ID, OTA_PROTO_VER, OTA_LOCAL_VERSION,
		 ops_boot_count(), ops_boot_crash_count(),
		 (unsigned)(k_uptime_get() / 1000), env, st_name(), why,
		 g_cl.last_check_ms, g_last_cmd_id,
		 (unsigned)(g_period_ms / 60000u));

	snprintf(req, sizeof(req),
		 "POST /v1/shadow?dev=%s HTTP/1.1\r\n"
		 "Host: %s:%d\r\nX-Dev-Token: %s\r\n"
		 "Content-Type: application/json\r\nContent-Length: %d\r\n"
		 "Connection: close\r\n\r\n%s",
		 CLOUD_DEV_ID, HUB_HOST, HUB_PORT, g_dev_token,
		 (int)strlen(body), body);

	int rc = hub_send_all(fd, req, strlen(req));

	if (rc == 0) {
		rc = hub_recv_head(fd, &g_resp);
		if (rc == 0 && g_resp.code != 200) {
			LOG_WRN("shadow: HTTP %u", g_resp.code);
		}
	}
	hub_close(&fd);
	return rc;
}

/* POST /v1/cmd/result：命令执行完立刻回执（含 rc 与截断后的输出）。 */
static int cloud_cmd_result(int32_t id, int rc_cmd, const char *out)
{
	static char body[448];
	static char req[768];
	static char safe[320];
	int fd = hub_connect();

	if (fd < 0) {
		return fd;
	}
	json_sanitize(safe, sizeof(safe), out);
	snprintf(body, sizeof(body),
		 "{\"dev\":\"%s\",\"id\":%d,\"rc\":%d,\"out\":\"%s\",\"up\":%u}",
		 CLOUD_DEV_ID, (int)id, rc_cmd, safe,
		 (unsigned)(k_uptime_get() / 1000));
	snprintf(req, sizeof(req),
		 "POST /v1/cmd/result?dev=%s HTTP/1.1\r\n"
		 "Host: %s:%d\r\nX-Dev-Token: %s\r\n"
		 "Content-Type: application/json\r\nContent-Length: %d\r\n"
		 "Connection: close\r\n\r\n%s",
		 CLOUD_DEV_ID, HUB_HOST, HUB_PORT, g_dev_token,
		 (int)strlen(body), body);
	rc_cmd = hub_send_all(fd, req, strlen(req));
	if (rc_cmd == 0) {
		rc_cmd = hub_recv_head(fd, &g_resp);
		if (rc_cmd == 0 && g_resp.code != 200) {
			LOG_WRN("cmd result: HTTP %u", g_resp.code);
		}
	}
	hub_close(&fd);
	return rc_cmd;
}
/* 下载中实时进度：tls_recv_cb 只累加这个数，再由 ota_progress_bytes 写进快照。
 * 不用 sess.written 是因为回调里拿不到 sess（引擎内部持有）。 */
static volatile uint32_t s_live_written;

static int tls_recv_cb(void *ctx, uint8_t *buf, size_t want, size_t *got)
{
	int fd = *(int *)ctx;
	ssize_t n = zsock_recv(fd, buf, want, 0);

	if (n < 0) {
		return -errno;
	}
	if (n == 0) {
		return -ECONNRESET;
	}
	*got = (size_t)n;

	s_live_written += (uint32_t)n;
	ota_progress_bytes(s_live_written);
	return 0;
}

/* GET /fw/<file>（Range 续传）→ ota_sess_run 从 TLS socket 吃 body。
 * 门禁已通过（req 合法），这里只管搬运与校验。 */
static int cloud_download(const struct cloud_offer *off, struct ota_req *req,
			  char *why, size_t why_n)
{
	char url[256];
	char reqbuf[512];
	int rc;

	snprintf(url, sizeof(url), "/fw/%s?dev=%s", off->file, CLOUD_DEV_ID);

	/* 引擎会话 + backend 开包（决定断点；NVS 记录可用 → Range 从那续） */
	const struct ota_backend *be = ota_backend_get();
	struct ota_sess sess;

	int32_t start = be->begin(be->ctx, req);

	if (start < 0) {
		snprintf(why, why_n, "backend begin failed: %s",
			 be->err_msg[0] ? be->err_msg : "?");
		return (int)start;
	}
	ota_sess_init(&sess, req);
	if (start > 0) {
		sess.written = (uint32_t)start;
		sess.start_off = (uint32_t)start;
	}

	int fd = hub_connect();

	if (fd < 0) {
		be->abort(be->ctx);
		snprintf(why, why_n, "TLS connect failed (%d) at download", fd);
		return fd;
	}

	char range[64] = "";

	if (start > 0) {
		snprintf(range, sizeof(range), "Range: bytes=%d-\r\n", (int)start);
	}
	snprintf(reqbuf, sizeof(reqbuf),
		 "GET %s HTTP/1.1\r\nHost: %s:%d\r\nX-Dev-Token: %s\r\n"
		 "%s"
		 "Connection: close\r\n\r\n",
		 url, HUB_HOST, HUB_PORT, g_dev_token, range);
	rc = hub_send_all(fd, reqbuf, strlen(reqbuf));
	if (rc == 0) {
		rc = hub_recv_head(fd, &g_resp);
	}
	if (rc) {
		snprintf(why, why_n, "download request failed (%d)", rc);
		goto out_fail;
	}

	/* 200=全量 / 206=续传。要续传拿到 200 说明云端不在断点态（理论几乎
	 * 不可能，除非记录 stale）→ 清断点记录后全量重跑一次，不死循环。 */
	if (g_resp.code != 200 && g_resp.code != 206) {
		snprintf(why, why_n, "download HTTP %u", g_resp.code);
		rc = -EPROTO;
		goto out_fail;
	}
	if (g_resp.code == 200 && start > 0) {
		LOG_WRN("resume rejected by hub (200), invalidate and retry full");
		be->abort(be->ctx);
		hub_close(&fd);
		ota_resume_invalidate();
		return cloud_download(off, req, why, why_n);
	}

	/* S0 打点从"本次实际传输"起算（续传时分母是增量字节，与局域网口径一致） */
	uint64_t t0 = k_uptime_get();

	/* 灯变紫：stage 从这一刻起是 recv，直到 ready/fail */
	s_live_written = (uint32_t)start;
	ota_progress_mark("recv", (uint32_t)start, (uint32_t)off->size,
			  (uint32_t)off->ver);

	rc = ota_sess_run(&sess, be, tls_recv_cb, &fd, g_resp.pre, g_resp.pre_len);
	g_cl.dl_bytes = sess.written;

	if (rc) {
		snprintf(why, why_n, "transfer failed: %s",
			 sess.err_msg[0] ? sess.err_msg : "?");
		rc = sess.err ? sess.err : rc;
		goto out_fail;
	}

	g_cl.st = CLOUD_VERIFY;
	ota_progress_mark("verify", sess.written, (uint32_t)off->size,
			  (uint32_t)off->ver);
	rc = ota_sess_finish(&sess, be, req);
	if (rc) {
		snprintf(why, why_n, "verify failed: %s",
			 sess.err_msg[0] ? sess.err_msg : "?");
		rc = sess.err ? sess.err : rc;
		goto out_fail;
	}

	/* ---- 切槽收尾（PERMANENT + nonce 提交 + 自证窗口），两通道同一把尺 */
	rc = ota_arm_upgrade((uint32_t)off->ver, req->nonce);
	if (rc) {
		snprintf(why, why_n, "arm_upgrade failed (%d)", rc);
		goto out_fail;
	}

	uint32_t ms = (uint32_t)(k_uptime_get() - t0);
	uint32_t kb_s = ms > 0 ? (uint32_t)(((uint64_t)(sess.written - sess.start_off) *
					    1000) / (ms * 1024)) : 0u;

	ota_progress_mark("ready", sess.written, (uint32_t)off->size,
			  (uint32_t)off->ver);

	LOG_INF("cloud ota ready: ver=%u size=%u total=%ums %uKB/s resumed=%d off=%d",
		(unsigned)off->ver, (unsigned)off->size, ms, kb_s,
		start > 0 ? 1 : 0, (int)start);
	hub_close(&fd);
	return 0;

out_fail:
	ota_progress_mark("fail", g_cl.dl_bytes, g_cl.dl_total, g_cl.offer_ver);
	be->abort(be->ctx);
	hub_close(&fd);
	return rc;
}

/* ---------------------------------------------------------------- 主流程 */
/* 一次完整尝试：check → (offer) → gate → download → verify → arm → report。
 * 返回 0 = 正常走完（含"无升级"）；负值 = 本次失败（why 里有人话）。
 *
 * 注意本函数有多条提前 return（TLS 失败 / 收头失败 / 204 无升级 / OFFER 收不全 /
 * JSON 解析失败），它们都不能负责收尾，所以外面必须再包一层把 st 归零 —— 见
 * cloud_run_once()。 */
static int cloud_run_once_body(void)
{
	char req[384];
	int fd;
	int rc;

	/* ---- 1. check（带 X-Proto：云端据此决定回旧 OFFER 还是 {cmd,offer}） */
	g_cl.st = CLOUD_CHECK;
	fd = hub_connect();
	if (fd < 0) {
		snprintf(g_cl.why, sizeof(g_cl.why), "TLS connect failed (%d)", fd);
		return fd;
	}
	snprintf(req, sizeof(req),
		 "GET /v1/ota/check?dev=%s HTTP/1.1\r\n"
		 "Host: %s:%d\r\nX-Dev-Token: %s\r\nX-Proto: %d\r\n"
		 "Connection: close\r\n\r\n",
		 CLOUD_DEV_ID, HUB_HOST, HUB_PORT, g_dev_token, OTA_PROTO_VER);
	rc = hub_send_all(fd, req, strlen(req));
	if (rc == 0) {
		rc = hub_recv_head(fd, &g_resp);
	}
	if (rc) {
		snprintf(g_cl.why, sizeof(g_cl.why), "check send/recv failed (%d)", rc);
		hub_close(&fd);
		return rc;
	}
	if (g_resp.code == 204) {
		hub_close(&fd);
		return 0; /* 无待升级 */
	}
	if (g_resp.code != 200 || g_resp.content_len == 0 ||
	    g_resp.content_len > OFFER_BUF_MAX) {
		snprintf(g_cl.why, sizeof(g_cl.why), "check HTTP %u len=%u",
			 g_resp.code, g_resp.content_len);
		hub_close(&fd);
		return -EPROTO;
	}

	/* OFFER JSON 收齐 */
	static char jbuf[OFFER_BUF_MAX + 1];
	size_t got = 0;

	if (g_resp.pre_len) {
		size_t n = MIN(g_resp.pre_len, (size_t)g_resp.content_len);

		memcpy(jbuf, g_resp.pre, n);
		got = n;
	}
	while (got < g_resp.content_len) {
		ssize_t n = zsock_recv(fd, jbuf + got, g_resp.content_len - got, 0);

		if (n <= 0) {
			snprintf(g_cl.why, sizeof(g_cl.why), "offer recv short");
			hub_close(&fd);
			return -ECONNRESET;
		}
		got += (size_t)n;
	}
	hub_close(&fd);
	jbuf[got] = '\0';

	/* ---- 2. 解析 + 关联性 + 门禁 */
	g_cl.st = CLOUD_GATE;
	struct cloud_offer off;

	memset(&off, 0, sizeof(off));
	int prc = json_obj_parse(jbuf, got, offer_descr, ARRAY_SIZE(offer_descr), &off);

	if (prc < 0) {
		snprintf(g_cl.why, sizeof(g_cl.why), "offer json parse failed (%d)", prc);
		return -EPROTO;
	}

	/* ---- 2a. 命令（可能与 OFFER 同一条应答下发；先执行、先回执） */
	if (off.cmd > 0 && off.cmdname != NULL) {
		char out[512];
		char idbuf[16];
		struct cloud_ops_ret oret;

		if ((uint32_t)off.cmd <= g_last_cmd_id) {
			/* 幂等：云端重推同一条（回执丢了 / TTL 内重试）不再执行第二次 */
			LOG_INF("cmd %d already executed (lastcmd=%u), skip",
				(int)off.cmd, g_last_cmd_id);
			cloud_cmd_result(off.cmd, 0, "dup: already executed");
		} else {
			cloud_ops_run(off.cmdname, off.cmdargs, out, sizeof(out), &oret);
			cloud_cmd_result(off.cmd, oret.rc, out);
			g_last_cmd_rc = oret.rc;
			g_last_cmd_id = (uint32_t)off.cmd;
			snprintf(idbuf, sizeof(idbuf), "%u", g_last_cmd_id);
			(void)settings_save_one("s3cloud/lastcmd", idbuf, strlen(idbuf));

			uint32_t now_s = (uint32_t)(k_uptime_get() / 1000u);

			if (oret.fast_rounds > 0) {
				g_fast_until_s = now_s + 300u;   /* 5 min 快轮询 */
			}
			if (oret.want_reboot) {
				/* 回执已经发出去，现在才允许复位；清 dirty 走干净重启 */
				ops_boot_mark_clean();
				k_sleep(K_MSEC(300));
				sys_reboot(SYS_REBOOT_COLD);
			}
		}
	}

	/* 只有命令、没有 OFFER：本轮到此为止（不会走门禁/下载/上报） */
	if (off.ver <= 0) {
		return 0;
	}

	/* ---- 2b. 解析 + 关联性 + 门禁 */
	char why[96] = "";
	int ver = 0;

	rc = offer_sanity(&off, why, sizeof(why));
	if (rc == 0) {
		struct ota_req req_gate;

		rc = offer_gate(&off, &req_gate, why, sizeof(why));
		if (rc == 0) {
			/* ---- 3. 下载（backend begin / sess_run / finish / arm） */
			ver = (int)off.ver;
			g_cl.offer_ver = (uint32_t)off.ver;
			g_cl.dl_total = (uint32_t)off.size;
			rc = cloud_download(&off, &req_gate, why, sizeof(why));
		}
	}

	/* ---- 4. 上报（成败都报；VPS 上 admin 能查到设备看到了什么） */
	if (rc != 0) {
		g_cl.last_err = rc;
		strncpy(g_cl.why, why, sizeof(g_cl.why) - 1);
	}
	g_cl.st = CLOUD_REPORT;
	hub_report(ver, rc == 0 ? "ok" : "fail", rc == 0 ? "" : g_cl.why);

	/* OFFER 带 reboot=1：装好即重启进新槽。必须先报完再重启，否则 VPS 上
	 * 看不到这次结果。不重启的话，自证窗口会在旧镜像里到点把 marker 丢掉。 */
	if (rc == 0 && ver > 0) {
		LOG_INF("cloud ota armed: reboot into v%d", ver);
		ops_boot_mark_clean();
		k_sleep(K_MSEC(200));
		sys_reboot(SYS_REBOOT_COLD);
	}

	g_cl.st = CLOUD_IDLE;
	return rc;
}

/* 收尾保证：不管 body 从哪条提前 return 出来，都把 st 归零。
 *
 * 踩过的坑：check 收到 204（云端没有待升级）时直接 return，st 停在 CLOUD_CHECK，
 * 于是 ota_cloud_busy() 恒真 —— worker 之后每一次唤醒都在 "自己还在忙" 里 continue，
 * 现场表现就是 portal ocheck 永远回 busy=1，而 VPS 访问日志里再也看不到
 * /v1/ota/check。只有重启设备才解开。把收尾收敛到这一层，新增 early return
 * 也不会再把状态机钉死。 */
static int cloud_run_once(void)
{
	int rc = cloud_run_once_body();

	g_cl.st = CLOUD_IDLE;
	return rc;
}

void ota_cloud_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		uint32_t now = (uint32_t)(k_uptime_get() / 1000u);
		uint32_t wait_ms = g_period_ms;

		/* 命令刚到 → 60 s 快轮询，让后续命令/回执连贯 */
		if (g_fast_until_s != 0 && now < g_fast_until_s) {
			wait_ms = FAST_PERIOD_MS;
		} else {
			g_fast_until_s = 0;
		}
		/* 连续失败退避：1 → 2 → 5 → 10 → 15(min) 封顶，成功即复位 */
		if (g_fail_cnt > 0) {
			uint32_t b = backoff_ms[MIN((uint32_t)g_fail_cnt - 1u,
						   ARRAY_SIZE(backoff_ms) - 1u)];

			if (b > wait_ms) {
				wait_ms = b;
			}
			if (wait_ms > BACKOFF_MAX_MS) {
				wait_ms = BACKOFF_MAX_MS;
			}
		}

		k_sem_take(&cloud_kick, K_MSEC(wait_ms));

		if (!g_token_ok) {
			continue;
		}
		if (!g_link_up || !ota_lan_idle() || ota_cloud_busy()) {
			continue;
		}
		now = (uint32_t)(k_uptime_get() / 1000u);

		/* 节流：手动踢（portal ocheck）置 force 位绕过 */
		if (!g_force_next && g_cl.last_check_ms &&
		    now - g_cl.last_check_ms < wait_ms / 1000u) {
			continue;
		}
		g_cl.last_check_ms = now;
		g_force_next = false;

		memset(g_cl.why, 0, sizeof(g_cl.why));

		/* 先报一份影子（失败不影响本轮 OTA，云端只失去一次可见性） */
		int src = cloud_shadow();

		if (src != 0) {
			LOG_WRN("shadow post failed (%d)", src);
		}

		int rc = cloud_run_once();

		if (rc == 0) {
			g_fail_cnt = 0;
			LOG_INF("cloud check done (idle / cmd / upgraded)");
		} else {
			if (g_fail_cnt < 255u) {
				g_fail_cnt++;
			}
			LOG_WRN("cloud run failed (%d): %s (backoff lvl %u)", rc, g_cl.why,
				g_fail_cnt);
		}
	}
}

/* ---------------------------------------------------------------- 对外接口 */
/* 云端 cfg_set hb=<sec> 调这里：改心跳周期并立刻踢一次（新周期马上生效）。 */
void ota_cloud_set_period(uint32_t sec)
{
	if (sec < 60u) {
		sec = 60u;
	}
	if (sec > 3600u) {
		sec = 3600u;
	}
	g_period_ms = sec * 1000u;
	g_force_next = true;
	k_sem_give(&cloud_kick);
}

void ota_cloud_check_now(void)
{
	g_force_next = true;
	k_sem_give(&cloud_kick);
}

/* main.c 的 rssi work 每周期喂链路状态；从断到通的边沿触发一次首查 */
void ota_cloud_link_feed(bool up)
{
	bool prev = g_link_up;

	g_link_up = up;
	if (up && !prev) {
		k_sem_give(&cloud_kick);
	}
}

bool ota_cloud_busy(void)
{
	return g_cl.st != CLOUD_IDLE;
}

const char *ota_cloud_snapshot_json(void)
{
	static const char *const names[] = {
		"idle", "check", "gate", "dl", "verify", "report"
	};
	static char buf[256];
	int st = g_cl.st < 0 || g_cl.st >= CLOUD_COUNT ? 0 : g_cl.st;

	snprintf(buf, sizeof(buf),
		 "\"cloud\":{\"st\":\"%s\",\"ver\":%u,\"dl\":%u,\"total\":%u,"
		 "\"last_check\":%u,\"token\":%s,\"hb\":%u,\"cmd\":%u,\"cmrc\":%d,"
		 "\"fail\":%u,\"err\":\"%.64s\"}",
		 names[st], g_cl.offer_ver, g_cl.dl_bytes, g_cl.dl_total,
		 g_cl.last_check_ms, g_token_ok ? "true" : "false",
		 (unsigned)(g_period_ms / 1000u), g_last_cmd_id, (int)g_last_cmd_rc,
		 (unsigned)g_fail_cnt, g_cl.why);
	return buf;
}

int ota_cloud_start(void)
{
	/* settings_load() 已在 main 里跑完，token 已进 g_dev_token。
	 * CA 证书注册进 TLS 凭据库（失败只影响公网通道，主流程照常）。 */
	int rc = tls_credential_add(HUB_SEC_TAG, TLS_CREDENTIAL_CA_CERTIFICATE,
				    hub_ca_pem, hub_ca_pem_len);

	if (rc) {
		LOG_ERR("CA credential add failed (%d) — cloud ota disabled", rc);
		return rc;
	}
	LOG_INF("cloud ota started: hub=%s:%d dev=%s token=%s",
		HUB_HOST, HUB_PORT, CLOUD_DEV_ID, g_token_ok ? "yes" : "MISSING");
	return 0;
}
