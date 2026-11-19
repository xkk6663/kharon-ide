/*
 * ops —— 让板子"能被诊断"，而不是崩了之后只能猜
 *
 * 三块互不相干但都需要"断电不丢"的东西：
 *   1. WiFi 事件日志：连上/掉线/连接失败，把 802.11 原始 reason_code 记下来。
 *      "无缘无故断连"唯一能定性的字段就是这个 —— 4/34=不活跃超时、15=四次握手超时、
 *      2=认证过期、8=AP 主动踢人。只记一个 status（0/1/2）等于什么都没记。
 *   2. 启动/崩溃记录：每次都落盘，靠"上一次是不是干净退出"判断上次有没有崩。
 *      .noinit 在 ESP32 上受 bootloader 影响不一定可靠，所以走 NVS，宁可多写一次 flash。
 *   3. 线程栈水位：卡死时最想知道"是不是某个线程栈快没了"。采样后用 /api/boot 暴露，
 *      以后不用抓串口也能看到谁在临界。
 *
 * 外加配网页/OTA 共用的一个口令（Basic Auth 与 X-Portal-Key 同一份）。
 */
#ifndef OPS_H
#define OPS_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* --------------------------------------------------------------- WiFi 事件 */
#define OPS_EV_N 16
#define OPS_SSID_MAX 24

enum ops_ev_kind {
	OPS_EV_CONNECT = 1,
	OPS_EV_DISCONNECT = 2,
	OPS_EV_CONN_FAIL = 3,
	/* 4 = 上一次运行不是干净退出（掉电/毛刺/崩溃/看门狗）。
	 * 这条记录是在**下次上电时**补写的，用来把"掉电"也留在同一本时间线里：
	 *   status_code = 复位原因枚举（esp_reset_reason()）
	 *   ssid        = 复位原因的可读名（poweron / brownout / panic …）
	 *   conn_s      = 上一次运行了多少秒（来自 boot/up）
	 * 掉电那一刻什么都不可能写，所以只能"事后记账"——这是物理事实，不是实现偷懒。 */
	OPS_EV_ABNORMAL_REBOOT = 4,
};

struct ops_ev_rec {
	uint32_t up_s;        /* 事件发生时的上电秒数 */
	uint32_t conn_s;      /* 掉线前这条链路活了多少秒（连接事件为 0）*/
	uint8_t kind;         /* enum ops_ev_kind */
	uint8_t dreason;      /* enum wifi_disconn_reason，仅掉线有意义 */
	uint16_t reason_code; /* 802.11 Deauth/Disassoc reason，★ 定性靠它 */
	uint16_t status_code; /* 802.11 Auth/Assoc status，连不上时看它 */
	int8_t rssi;          /* 事件当时的 RSSI（0 = 没取到） */
	char ssid[OPS_SSID_MAX];
};

/* 记一条事件（内部会落盘）。reason/rc/sc 不适用时传 0；conn_s 只有掉线有意义。 */
void ops_ev_add(uint8_t kind, int reason, uint16_t reason_code, uint16_t status_code,
		int8_t rssi, const char *ssid, uint32_t conn_s);

/* 把最近事件写成 JSON 数组，buf 不够时会截断尾部（保证仍然是合法 JSON 前缀）。 */
void ops_ev_json(char *buf, size_t len);

/* 掉线汇总：{"total":..,"conn":..,"disc":..,"fail":..,"last":{...},"why":{...}}
 * 专门给"上电后回来查上一次为什么断"用，字段带上人话翻译。 */
void ops_drop_json(char *buf, size_t len);

/* 事件条数（给状态页做"有没有新异常"提示） */
uint8_t ops_ev_count(void);

/* ------------------------------------------------------------ 启动 / 崩溃 */
/* settings_load() 之后调一次：读上次状态、累加启动次数、判断上次是否异常退出。 */
void ops_boot_init(void);

/* 周期性调用（60 s 一次足够）：记录本次已运行时长，供下次启动时知道"跑多久才崩"。 */
void ops_boot_tick(uint32_t up_s);

/* 主动重启前调（OTA 重启、shell reboot）：把本次标记为干净退出。 */
void ops_boot_mark_clean(void);

/* {"n":..,"crash":..,"last_up":..,"clean":..} */
void ops_boot_json(char *buf, size_t len);

/* 启动/崩溃计数（自证窗口用来判"候选固件崩没崩"）。 */
uint32_t ops_boot_crash_count(void);
uint32_t ops_boot_count(void);

/* 本次启动时"上一次运行是否干净退出"。
 * 自证窗口用它判"候选固件在窗口内被拔电/硬复位" —— 掉电不会让 crash 计数 +1，
 * 只有这个标志能证明上一轮没走正常路径。 */
bool ops_boot_last_clean(void);

/* 本次启动的复位原因文本（poweron / brownout / panic …），窗口用它区分
 * "被拔电" 和 "主动软重启"：软重启是干净的，不该触发回退。 */
const char *ops_boot_rst_reason(void);

/* 线程栈水位 JSON：[{"name":"..","unused":..}, ...]，按剩余字节升序取最紧的几个。 */
void ops_stack_json(char *buf, size_t len, uint8_t max_items);

/* ------------------------------------------------------------------ 鉴权 */
/* 口令默认 <REDACTED-PSK>，可在页面改；存在 NVS 的 auth/pw。 */
const char *ops_auth_pw(void);
int ops_auth_set_pw(const char *pw);

/* 校验 HTTP 的 Authorization 头（"Basic base64(user:pass)"）。user 固定 admin。 */
bool ops_auth_ok(const char *authorization);

/* --------------------------------------------------------------- 名称工具 */
/* 802.11 reason_code → 人能看懂的短语（掉线原因翻译，和 wifi_disconn_reason_txt 互补）。 */
const char *ops_reason_code_txt(uint16_t code);

#endif /* OPS_H */
