/*
 * portal_cfg.h —— 本工程共享的常量（只有宏，没有状态）
 *
 * 这些数原来是散在 main.c 顶部的 #define，被 4 个模块用到：改一次要翻三处。
 * 收在这里之后，"AP 密码 / 重连次数 / 退避上限"各有一处唯一定义。
 *
 * 注意：都是**编译期常量**，不引入 Kconfig —— 改 prj.conf 会触发全量重编，
 * 而改一个头文件只重编 include 它的 4~5 个文件。
 */
#ifndef PORTAL_CFG_H_
#define PORTAL_CFG_H_

/* ---- SoftAP 身份 ---- */
#define AP_SSID             "S3-Portal"
#define AP_PSK              "<REDACTED-PSK>"     /* WPA2 要求至少 8 位 */
#define AP_IP               "192.168.4.1"
#define AP_NETMASK          "255.255.255.0"
#define AP_POOL             "192.168.4.10"    /* DHCP 起始地址 */

/* ---- 一次连接尝试 ---- */
#define CONNECT_TIMEOUT_MS  20000

/* ---- 上电自动重连（逐条轮） ---- */
#define AUTO_RETRY_MAX      3
#define AUTO_RETRY_DELAY_MS 3000
/* 第一次失败后只等 0.5 s 就重试：实测在"AP 刚起、驱动还没扫过"的窗口里
 * 第一次 CONNECT 会在 0.6 s 内被拒（status 1），此时等满 3 s 纯属白等。
 * 后面几次仍用 3 s —— 那才是"真的连不上"该给的间隔。 */
#define AUTO_RETRY_FIRST_MS 500

/* ---- 信道对齐（单射频 APSTA 的核心约束） ---- */
/* 扫描缓存多久算"过期"：超过这个年龄就不拿它做信道判断，宁可先扫一次 */
#define CHANNEL_CACHE_TTL_MS 60000
/* 等了多久还拿不到 SCAN_DONE 就放弃等待（ESP32 全信道扫描实测 ~10 s） */
#define ALIGN_SCAN_WAIT_MS   15000
/* 对齐最多走几步：0 = 还没试过 → 1 = 已经做过一次 0.5 s 廉价重试 →
 * 2 = 已经搬过 AP 或扫过信道（到此封顶，之后回落到普通重试/退避）。
 * 封顶是为了防止"扫一次 → 连 → 失败 → 再扫"的无限循环。
 * 注意顺序：能靠缓存直接搬 AP 的第二档，比全信道扫描（~10 s）便宜得多，
 * 所以扫描只留给"缓存里压根没有这个网络"的情形。 */
#define ALIGN_STEP_MAX      2

/* ---- 运行期掉线重连（退避） ---- */
#define RT_RETRY_MAX        10
#define RT_RETRY_BASE_MS    3000
#define RT_RETRY_CAP_MS     24000
#define CONN_STABLE_MS      30000

#endif /* PORTAL_CFG_H_ */
