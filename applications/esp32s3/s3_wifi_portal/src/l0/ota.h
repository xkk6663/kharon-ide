/*
 * OTA 接收端：局域网内 HTTP POST 上传固件 -> 流式写 slot1 -> 切槽前校验 -> 重启
 *
 * 设计遵循 embedded-ota-pipeline 的分层：
 *   S0 打点   —— timing dl= verify= total= throughput= size=，切槽前输出
 *   S1 传输   —— 独立 TCP 端口 + 手写 HTTP 解析，绕开 http_server 的 body 缓冲
 *   S2 完整性 —— 流式 SHA-256 累积 + 切槽前 flash 回读比对，不符不切
 *   S3 鉴权   —— PSK / 口令覆盖元数据（version/size/nonce），nonce 持久化递增
 *   S4 门禁   —— 显式递增版本号；force 只让发起端放行，设备端独立裁决
 *   S5 回滚   —— 本板用 PERMANENT 语义，不做 TEST+自证（原因见 ota.c 切槽处注释：
 *                ESP32 XIP 下往运行中的 slot0 内 trailer 返回 -EIO，TEST 镜像永远转不了正），
 *                改用应用侧自证窗口 + 显式回退
 *   S6 体积   —— size 超过槽容量直接拒，不落盘
 */
#ifndef OTA_H_
#define OTA_H_

#include <stdbool.h>

/* 运行期版本号：每次要"允许被覆盖"的固件必须把这个数字调大。
 * 这是显式递增号，不是 git describe 派生——后者（v2.0-4-gxxxx）会被解析成
 * 2.0.0，同一个主版本下任意两次构建都"看不出新旧"，开发期只能靠 force 放行。 */
#define OTA_LOCAL_VERSION 162

/* 协议版本：字段或规范化串变更时 +1，旧发起端会拿到明确拒绝码。
 * v3（2026-11-19）：check 支持 X-Proto 协商 —— 老固件不带该头仍拿纯 OFFER，
 * 新固件带 3 才可能收到「命令 + OFFER」同体应答（字段 cmd/cmdname/cmdargs）。 */
#define OTA_PROTO_VER 3

/* 上传端口。刻意与配网页的 80 分开：
 * - http_server 把 body 交给 handler 时走自己的缓冲，几百 KB 固件不适合挤在里面
 * - 单独端口能自己控 MTU 对齐、自己打点、自己做断点
 * 网页在 80，上传发到 8080，所以返回头常带 CORS。 */
#define OTA_PORT 8080

/* 协议 v2（不兼容升级，双端同步发布）：上传必须带
 *   X-Sha256   发起端声明的整包 SHA-256（64 位小写 hex），切槽前跟设备端算出来的比
 *   X-Auth     hex(HMAC-SHA256(PSK, "ver|size|sha256|nonce|force")) —— 脚本走这条
 *   或 X-Key   配网页口令（页面走这条，弱一档：明文共享密钥，不是 HMAC）
 * 旧发起端会被拒绝：401 noauth/badauth（缺 X-Sha256 时是 400 missing sha）。 */

/* 槽容量上限的物理红线（1344 KB 减去 mcuboot 头与尾部 TLV 的余量）。
 * 超过就当场拒，不写一个字节 —— 这是 S6 的"只把物理溢出当 FAIL"。 */
#define OTA_SLOT_MAX_BYTES (1344 * 1024 - 64 * 1024)

/* 断点续传（skill 的可选档，向后兼容，不算协议不兼容升级）：
 *   发起端带 X-Resume: 1 且先只发头，设备端握手后回：
 *     206 + X-Resume-Offset: <已确认偏移>   -> 从该偏移发剩下的字节
 *     200 {"resume":0}                      -> 记录不可用，从 0 发全量
 * 不带 X-Resume 的旧发起端行为完全不变（一次性发 body，走全量路径）。
 * 断点只落段边界（256 KB），记录在 NVS s3ota/rb，含 magic/总长/版本/期望摘要/
 * 已确认偏移/已完成段摘要；加载时做 stale 校验，不符就丢弃走全量。 */
#define OTA_RESUME_SEG (256 * 1024)

/* 启动后要活多久、且链路正常，才认为新固件可信并转正（自证窗口）。
 * 零点 = 应用 main() 开始跑，不是"升级完成之后"。 */
#define OTA_SELFTEST_MS (60 * 1000)

int ota_srv_start(void);

/* 给 /api/status 拼字段用的快照，返回的是不带外层大括号的 JSON 片段 */
const char *ota_snapshot_json(void);

/* 由主循环周期调用（本工程是 10 s 一次的 rssi work）：
 *   候选固件（刚 OTA 上去那一版）在自证窗口内证明自己活着 -> 清标记（转正）；
 *   崩过（mps crash 计数变大）、窗口走完链路还不通、或窗口内被拔电 -> pending 置
 *   mcuboot 搬回旧槽。
 * 注意这是应用侧回退：候选固件连启动都起不来时，窗口根本没有机会跑，
 * 那种情况只能靠 USB 重刷 —— 这条边界写进 17.9 / 17.11。 */
void ota_confirm_if_healthy(bool link_ok);

/* 当前是否处于"待确认"（mcuboot TEST 模式） */
bool ota_pending_verify(void);

/* 自证窗口：OTA 成功切槽前记下候选版本 + 当时的崩溃计数 */
void ota_arm_candidate(uint32_t ver, uint32_t crash_now);

/* ---- 进度打点（公网通道用）------------------------------------------------
 * 这两条只写 ota_snapshot_json() 里的 stage/written/total/ver —— 也就是 LED
 * （led_status 靠 "stage":"recv|verify|ready" 点亮紫色）和状态页读的那份快照；
 * 不动局域网状态机 g_ota.st，因此不会影响 fail() 的"别把 ready 抹掉"保护。
 * 局域网通道自己会在 ota.c 里写，公网通道必须显式调这两条，否则灯一直是绿的。 */
void ota_progress_mark(const char *stage, uint32_t written, uint32_t total,
		       uint32_t ver);
/* 每收一块调一次：纯赋值，别在里面做格式化/日志 */
void ota_progress_bytes(uint32_t written);
uint32_t ota_candidate_ver(void);
uint32_t ota_candidate_crash(void);
void ota_commit_candidate(void);

/* 显式回退：把非运行槽（里面是上一版合法镜像）标成待启动并重启。
 * 返回 0 表示标记成功（重启在内部完成）；负数表示 mcuboot 拒绝标记。 */
int ota_revert_to_secondary(const char *why);

/* ------------------------------------------------------------ 公网通道（ota_cloud）复用的门禁原语
 *
 * 局域网通道（HTTP POST 上传）与公网通道（HTTPS 拉取）共用同一套：
 *   · 写槽后端（flash_img 流式 + SHA-256 + 回读） —— ota_backend_get()
 *   · 鉴权 / 防重放 / 版本门禁 / 槽容量          —— 伪头 + ota_req_check（ota_engine）
 *   · 升级收尾（PERMANENT 切槽 + nonce 账本 + 自证窗口）
 *
 * 公网侧拿这些原语自己组会话，不许绕过任何一道门禁另写一份。 */

/* 写槽后端：begin/write/finish/abort（见 ota_engine.h 的 struct ota_backend）。
 * 两个通道互斥使用 —— 期间写槽上下文/摘要上下文是全局的。 */
struct ota_backend;
const struct ota_backend *ota_backend_get(void);

/* 当前 nonce 账本（持久化在 NVS s3ota/nonce），防重放的"必须严格递增"基准 */
uint32_t ota_nonce_get(void);

/* 升级收尾（两个通道共用）：PERMANENT 切槽 + nonce 提交 + 清断点 + 记自证窗口。
 * ver=新版本 nonce=本次升级用的 nonce。返回 0 = 已要求 mcuboot 切槽。
 * 调用前提：整包校验（backend finish）已通过 —— 这里不再复查。 */
int ota_arm_upgrade(uint32_t ver, uint32_t nonce);

/* 丢弃本地断点记录（公网通道的云端不在断点态时重跑全量前调） */
void ota_resume_invalidate(void);

/* 局域网接收端是否空闲（公网通道开工前必须查，写槽上下文是全局的） */
bool ota_lan_idle(void);

/* 一次性写入 OTA PSK 到 NVS（s3ota/psk，长度 < 48 B）。l1 的 `portal opsk` 调。
 * 量产换密钥走这条；不写就吃编译期默认值（只适合开发板）。写盘成功才切内存。 */
int ota_psk_set(const char *psk);

/* S3 鉴权回调（两通道共用）：伪头串里 X-Auth（HMAC）或 X-Key（口令）比对通过即 true。
 * 导出给 ota_cloud 传给 ota_req_check 的 auth 回调（ota_auth_cb 签名）——
 * 规范化串两端必须逐字一致：upgrade 用 "ver|size|sha256|nonce|force"。 */
bool ota_auth_ok(void *ctx, const char *hdr, const char *canonical);

#endif /* OTA_H_ */
