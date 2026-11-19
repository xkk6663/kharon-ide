/*
 * ota_cloud —— 公网 OTA 拉取端：设备只出站，不监听任何公网端口
 *
 * 时序（与 VPS 上 cloudhub.py 的协议逐字对齐，VPS 只搬运不裁决）：
 *   1  TLS1.2 连 120.76.230.17:8443（CA 内嵌 + IP SAN 校验，中间人换证书必死）
 *   2  GET /v1/ota/check?dev=<id>   头 X-Dev-Token
 *        204 → 没有待升级，继续睡
 *        200 → OFFER{proto,ver,file,size,sha256,nonce,force,auth}
 *   3  门禁（复用 ota_engine 四条硬规则 + 局域网通道同一把 PSK）：
 *        HMAC-SHA256(PSK,"ver|size|sha256|nonce|force") 比对 OFFER.auth
 *        nonce 严格递增（NVS 账本，重放必死；nonce 由 PC 从 /v1/nonce/next 领）
 *        ver 必须大于本机版本（force 无豁免 —— 公网发起端没有配网页的弱口令档）
 *        size ≤ 槽容量
 *   4  GET /fw/<file>?dev=<id>（带 X-Dev-Token；续传时带 Range: bytes=<off>-）
 *        206/200 流式写 slot1 —— 同一个 ota_backend，SHA-256 累积 + 回读
 *   5  POST /v1/ota/report?dev=<id>   {ver,result,...}  → admin 在 /v1/ota/status 查
 *   6  ota_arm_upgrade()：PERMANENT 切槽 + nonce 提交 + 自证窗口 → 重启
 *
 * 设备 token 不写死在源码里（这文件会进 git）：NVS s3cloud/token 优先，
 * 用 `portal otoken <hex>` 写入；没写就没有公网能力（编译期只给 dev_id）。
 *
 * 线程模型：独立线程 + 信号量。周期默认 3 min（云端 cfg_set hb= 可调，
 * 失败按 1→2→5→10→15 min 退避）；`portal ocheck` 手动触发绕过节流。
 * 局域网 OTA 正在跑时（ota_lan_idle()==false）绝不并发 ——
 * 写槽上下文（flash_img / 摘要 / 断点记录）是两个通道共用的全局。
 */
#ifndef OTA_CLOUD_H_
#define OTA_CLOUD_H_

#include <stdbool.h>
#include <stdint.h>

/* 启动线程与周期 work。失败返回负错误码（线程起不来的场合主流程照常）。 */
int ota_cloud_start(void);

/* 链路状态喂入（main.c 的 10 s 周期 work 调；l0 不许 include l2 的头）。
 * 从断到通的边沿会触发一次 check。 */
void ota_cloud_link_feed(bool up);

/* 一次性写入设备 token 到 NVS（s3cloud/token）。l1 的 portal otoken 调。 */
int ota_cloud_token_set(const char *hex);

/* 手动触发一次 check（绕过节流）。实际动作在专属线程里排队执行。 */
void ota_cloud_check_now(void);

/* 改心跳/巡检周期（秒，60..3600）。云端 cfg_set hb=<sec> 调；立刻生效。 */
void ota_cloud_set_period(uint32_t sec);

/* 正在 check / 下载 / 收尾（true 时禁止再触发） */
bool ota_cloud_busy(void);

/* 给 /api/status / shell 拼快照：不带外层大括号的 JSON 片段 */
const char *ota_cloud_snapshot_json(void);

#endif /* OTA_CLOUD_H_ */
