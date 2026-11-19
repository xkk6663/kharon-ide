/*
 * ops.c —— WiFi 事件日志 / 启动崩溃记录 / 栈水位 / 口令，四件都不占热路径
 */
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>
#include <zephyr/net/wifi_mgmt.h>   /* ops_drop_json 里用 wifi_disconn_reason_txt() */

/* ESP32 的复位原因比"我们自己记的 dirty 标志"准得多：
 * 刷机、按复位键、掉电、panic 在 dirty 上看起来都一样，但复位原因能把它们分开。
 * 只在 ESP32 系列上引入，别的 SoC 编译时自动跳过。 */
#if defined(CONFIG_SOC_SERIES_ESP32) || defined(CONFIG_SOC_SERIES_ESP32S2) ||              \
	defined(CONFIG_SOC_SERIES_ESP32S3) || defined(CONFIG_SOC_SERIES_ESP32C3) ||       \
	defined(CONFIG_SOC_SERIES_ESP32C6) || defined(CONFIG_SOC_SERIES_ESP32H2)
#include <esp_system.h>
#define OPS_HAVE_RST_REASON 1
#else
#define OPS_HAVE_RST_REASON 0
#endif

#include "ops.h"

LOG_MODULE_REGISTER(ops, LOG_LEVEL_INF);

/* ======================================================= WiFi 事件日志 */
#define EV_MAGIC 0x574C4F47u /* "WLOG" */

struct ops_ev_store {
	uint32_t magic;
	uint32_t head; /* 下一个要写的位置（环形） */
	uint32_t count;
	struct ops_ev_rec rec[OPS_EV_N];
};

static struct ops_ev_store g_ev = { .magic = EV_MAGIC };
/* 落盘节流：同一条事件不重复写，NVS 一次写约 600 B，掉线密集时也不至于刷穿 flash */
static uint32_t g_ev_last_save_ms;
static bool g_ev_save_pending;

static void ev_save(void)
{
	int rc = settings_save_one("wlog/blk", &g_ev, sizeof(g_ev));

	if (rc) {
		LOG_WRN("wlog save failed (%d)", rc);
	}
	g_ev_last_save_ms = k_uptime_get_32();
	g_ev_save_pending = false;
}

/* 读回：没有这个回调时 settings_save_one 等于往黑洞里写 —— 上电后账本永远是空的。
 * 这正是不挂串口就"查不到上一次为什么断"的根因（实测踩到）。 */
static int wlog_set_cb(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	if (strcmp(name, "blk") != 0) {
		return -ENOENT;
	}

	/* 结构体加过字段（conn_s），旧 blob 长度会对不上。
	 * 不对就整块丢弃并重新开始：宁可丢历史，也不要读出一个错位的"现场"。 */
	if (len != sizeof(g_ev)) {
		LOG_WRN("wlog: stored blob %u B != %u B, history dropped", (unsigned)len,
			(unsigned)sizeof(g_ev));
		memset(&g_ev, 0, sizeof(g_ev));
		g_ev.magic = EV_MAGIC;
		return 0;
	}

	struct ops_ev_store tmp;

	if (read_cb(cb_arg, &tmp, sizeof(tmp)) < 0) {
		return -EIO;
	}

	if (tmp.magic != EV_MAGIC || tmp.head >= OPS_EV_N || tmp.count > OPS_EV_N) {
		LOG_WRN("wlog: bad blob (magic=0x%08x head=%u count=%u), ignored", tmp.magic,
			(unsigned)tmp.head, (unsigned)tmp.count);
		return 0;
	}

	g_ev = tmp;
	LOG_INF("wlog: restored %u event(s), head=%u", (unsigned)g_ev.count, (unsigned)g_ev.head);
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ops_wlog, "wlog", NULL, wlog_set_cb, NULL, NULL);

void ops_ev_add(uint8_t kind, int reason, uint16_t reason_code, uint16_t status_code,
		int8_t rssi, const char *ssid, uint32_t conn_s)
{
	struct ops_ev_rec *r = &g_ev.rec[g_ev.head];

	memset(r, 0, sizeof(*r));
	r->up_s = k_uptime_get_32() / 1000u;
	r->conn_s = conn_s;
	r->kind = kind;
	r->dreason = (uint8_t)(reason < 0 ? 0 : reason);
	r->reason_code = reason_code;
	r->status_code = status_code;
	r->rssi = rssi;
	if (ssid) {
		strncpy(r->ssid, ssid, OPS_SSID_MAX - 1);
	}

	g_ev.head = (g_ev.head + 1u) % OPS_EV_N;
	if (g_ev.count < OPS_EV_N) {
		g_ev.count++;
	}

	LOG_INF("wlog: kind=%u reason=%d rc=%u sc=%u rssi=%d conn=%us ssid=%s", kind, reason,
		reason_code, status_code, rssi, conn_s, ssid ? ssid : "-");

	/* 立刻落盘，不做延迟合并：掉线之后紧接着可能就是复位或掉电，
	 * 延迟写等于把唯一的现场丢掉。一次 ~600 B，一天几十次也伤不到 flash。 */
	ev_save();
}

uint8_t ops_ev_count(void)
{
	return (uint8_t)g_ev.count;
}

void ops_ev_json(char *buf, size_t len)
{
	size_t used = 0;
	uint32_t n = g_ev.count;

	if (len < 4) {
		if (len) {
			buf[0] = '\0';
		}
		return;
	}

	used += snprintf(buf + used, len - used, "[");

	/* 从最旧的一条开始输出 */
	for (uint32_t i = 0; i < n && used < len - 1; i++) {
		uint32_t idx = (g_ev.head + OPS_EV_N - g_ev.count + i) % OPS_EV_N;
		const struct ops_ev_rec *r = &g_ev.rec[idx];

		used += snprintf(buf + used, len - used,
				 "%s{\"t\":%u,\"k\":%u,\"dr\":%u,\"rc\":%u,\"sc\":%u,\"rssi\":%d,"
				 "\"hold\":%u,\"ssid\":\"%s\"}",
				 i ? "," : "", r->up_s, r->kind, r->dreason, r->reason_code,
				 r->status_code, r->rssi, r->conn_s, r->ssid);
	}

	if (used < len) {
		snprintf(buf + used, len - used, "]");
	}
}

void ops_drop_json(char *buf, size_t len)
{
	uint32_t conn = 0, disc = 0, fail = 0, abn = 0;
	const struct ops_ev_rec *last_drop = NULL;
	const struct ops_ev_rec *last_fail = NULL;
	const struct ops_ev_rec *last_abn = NULL;

	for (uint32_t i = 0; i < g_ev.count; i++) {
		uint32_t idx = (g_ev.head + OPS_EV_N - g_ev.count + i) % OPS_EV_N;
		const struct ops_ev_rec *r = &g_ev.rec[idx];

		if (r->kind == OPS_EV_CONNECT) {
			conn++;
		} else if (r->kind == OPS_EV_DISCONNECT) {
			disc++;
			last_drop = r;   /* 最新的掉线（"连上了又断"） */
		} else if (r->kind == OPS_EV_CONN_FAIL) {
			fail++;
			last_fail = r;   /* 最新的连不上 */
		} else if (r->kind == OPS_EV_ABNORMAL_REBOOT) {
			abn++;
			last_abn = r;    /* 最新一次"上次没干净退出" */
		}
	}

	/* 三类分开报：把"掉线"和"连不上"合成一个 last 会让排障时误读成
	 * "网络掉过"，而实际可能是"从来没连上"；掉电/崩溃又是第三类。 */
	size_t used = snprintf(buf, len,
			       "{\"total\":%u,\"conn\":%u,\"disc\":%u,\"fail\":%u,\"abn_boot\":%u",
			       (unsigned)g_ev.count, conn, disc, fail, abn);

	if (last_drop != NULL && used < len) {
		used += snprintf(buf + used, len - used,
				 ",\"last_drop\":{\"t\":%u,\"held_s\":%u,\"dr\":%u,\"drtxt\":\"%s\","
				 "\"rc\":%u,\"rctxt\":\"%s\",\"sc\":%u,\"rssi\":%d,\"ssid\":\"%s\"}",
				 last_drop->up_s, last_drop->conn_s, last_drop->dreason,
				 wifi_disconn_reason_txt(last_drop->dreason),
				 last_drop->reason_code,
				 ops_reason_code_txt(last_drop->reason_code), last_drop->status_code,
				 last_drop->rssi, last_drop->ssid);
	} else if (used < len) {
		used += snprintf(buf + used, len - used, ",\"last_drop\":null");
	}

	if (last_fail != NULL && used < len) {
		used += snprintf(buf + used, len - used,
				 ",\"last_fail\":{\"t\":%u,\"sc\":%u,\"rssi\":%d,\"ssid\":\"%s\"}",
				 last_fail->up_s, last_fail->status_code, last_fail->rssi,
				 last_fail->ssid);
	} else if (used < len) {
		used += snprintf(buf + used, len - used, ",\"last_fail\":null");
	}

	if (used < len) {
		snprintf(buf + used, len - used, "}");
	}
}

/* ========================================================= 启动/崩溃记录 */
static struct {
	uint32_t boots;
	uint32_t crashes;
	uint32_t last_up;
	bool clean_last;   /* 上一次是不是干净退出 */
} g_boot = { .boots = 1 };

/* NVS 里读到的原始值（settings_load 期间填，init 时才做判断） */
static struct {
	uint32_t n;
	uint32_t crash;
	uint32_t up;
	uint32_t dirty;
	uint32_t tick_saved;
} g_raw;

/* 本次启动的复位原因（panic / task_wdt / poweron ...），来自 ESP-IDF 的
 * esp_reset_reason()。这是区分"真崩了"和"我刚烧了固件"的关键证据。 */
static char g_rst_reason[16] = "-";
/* 同时留一份原始枚举值：字符串映射再全也会漏（ESP-IDF 后面又加过
 * USB/JTAG/EFUSE/PWR_GLITCH/CPU_LOCKUP 几个），只报字符串就会出现
 * "rst=other" 这种什么都说明不了的结论 —— 实测踩过：板子被外部复位后
 * 两次启动都是 other，光看字符串根本判断不出是谁干的。 */
static int g_rst_code = -1;

static int boot_set_cb(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	int rc;

	if (settings_name_steq(name, "n", NULL)) {
		rc = read_cb(cb_arg, &g_raw.n, sizeof(g_raw.n));
		return rc < 0 ? rc : 0;
	}
	if (settings_name_steq(name, "crash", NULL)) {
		rc = read_cb(cb_arg, &g_raw.crash, sizeof(g_raw.crash));
		return rc < 0 ? rc : 0;
	}
	if (settings_name_steq(name, "up", NULL)) {
		rc = read_cb(cb_arg, &g_raw.up, sizeof(g_raw.up));
		return rc < 0 ? rc : 0;
	}
	if (settings_name_steq(name, "dirty", NULL)) {
		rc = read_cb(cb_arg, &g_raw.dirty, sizeof(g_raw.dirty));
		return rc < 0 ? rc : 0;
	}

	return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(ops_boot, "boot", NULL, boot_set_cb, NULL, NULL);

static void boot_save_all(void)
{
	uint32_t one = 1;

	settings_save_one("boot/n", &g_boot.boots, sizeof(g_boot.boots));
	settings_save_one("boot/crash", &g_boot.crashes, sizeof(g_boot.crashes));
	settings_save_one("boot/up", &g_boot.last_up, sizeof(g_boot.last_up));
	settings_save_one("boot/dirty", &one, sizeof(one));
}

void ops_boot_init(void)
{
	const char *reason = "-";

#if OPS_HAVE_RST_REASON
	g_rst_code = (int)esp_reset_reason();
	switch (esp_reset_reason()) {
	case ESP_RST_PANIC:     reason = "panic";     break; /* 异常/断言/栈溢出终止 */
	case ESP_RST_TASK_WDT:  reason = "task_wdt";  break;
	case ESP_RST_INT_WDT:   reason = "int_wdt";   break;
	case ESP_RST_WDT:       reason = "other_wdt"; break;
	case ESP_RST_BROWNOUT:  reason = "brownout";  break;
	case ESP_RST_SW:        reason = "software";  break;
	case ESP_RST_POWERON:   reason = "poweron";   break;
	case ESP_RST_EXT:       reason = "ext_pin";   break;
	case ESP_RST_DEEPSLEEP: reason = "deepsleep"; break;
	case ESP_RST_SDIO:      reason = "sdio";      break;
#ifdef ESP_RST_USB
	case ESP_RST_USB:       reason = "usb";       break; /* 原生 USB 拔插/重枚举 */
#endif
#ifdef ESP_RST_JTAG
	case ESP_RST_JTAG:      reason = "jtag";      break;
#endif
#ifdef ESP_RST_EFUSE
	case ESP_RST_EFUSE:     reason = "efuse";     break;
#endif
#ifdef ESP_RST_PWR_GLITCH
	case ESP_RST_PWR_GLITCH: reason = "pwr_glitch"; break; /* 电源毛刺：WiFi 突发电流打崩供电 */
#endif
#ifdef ESP_RST_CPU_LOCKUP
	case ESP_RST_CPU_LOCKUP: reason = "cpu_lockup"; break; /* 典型"卡死"：中断里死等/关中断 */
#endif
	default:                reason = "other";     break;
	}
#endif

	strncpy(g_rst_reason, reason, sizeof(g_rst_reason) - 1);

	/* 上一次留下的 dirty=1 表示"运行时没机会把它清零"。但刷机、按复位键、
	 * 拔电也会留 dirty=1 —— 之前全靠它计数，结果每烧一次固件 crash 就 +1，
	 * 数字看着像"崩了 6 次"，其实一次 panic 都没有。改成只有复位原因确实
	 * 指向 panic / 看门狗 / 掉电 时才计一次崩溃。 */
	g_boot.clean_last = (g_raw.dirty == 0);
	g_boot.boots = g_raw.n + 1u;

	bool abnormal = (strcmp(reason, "panic") == 0) || (strcmp(reason, "task_wdt") == 0) ||
			(strcmp(reason, "int_wdt") == 0) || (strcmp(reason, "brownout") == 0) ||
			(strcmp(reason, "other_wdt") == 0) ||
			/* 下面两条以前被归到 "other" 里，等于把"卡死"和"电源毛刺"当正常重启，
			 * 崩溃计数永远不涨，现象就变成"板子莫名其妙重启但 crash 不变"。 */
			(strcmp(reason, "cpu_lockup") == 0) ||
			(strcmp(reason, "pwr_glitch") == 0);

	g_boot.crashes = g_raw.crash + (abnormal ? 1u : 0u);
	g_boot.last_up = g_raw.up;

	LOG_INF("boot #%u, rst=%s, previous run %s (last_up=%us, abnormal_crashes=%u)",
		g_boot.boots, g_rst_reason, g_boot.clean_last ? "clean" : "dirty",
		g_boot.last_up, g_boot.crashes);

	/* 上次断连的现场在启动时就报一行：这样"板子自己重启了、串口也没接"的情况下，
	 * 下次上电第一屏就能看到最后一次为什么断，不必回查 /api/drops。 */
	for (uint32_t i = 0; i < g_ev.count; i++) {
		uint32_t idx = (g_ev.head + OPS_EV_N - g_ev.count + i) % OPS_EV_N;
		const struct ops_ev_rec *r = &g_ev.rec[idx];

		if (r->kind == OPS_EV_DISCONNECT || r->kind == OPS_EV_CONN_FAIL ||
		    r->kind == OPS_EV_ABNORMAL_REBOOT) {
			LOG_INF("wlog event: kind=%u ssid=%s dr=%u rc=%u(%s) sc=%u rssi=%d "
				"held=%us at up=%us",
				r->kind, r->ssid, r->dreason, r->reason_code,
				ops_reason_code_txt(r->reason_code), r->status_code, r->rssi,
				r->conn_s, r->up_s);
		}
	}

	/* 把"上次不是干净退出"写进同一本账：掉电那一刻写不了任何东西，
	 * 唯一能拿到的证据是上电时的复位原因 + 上次跑了多久。
	 * 只记三类：异常复位（panic/看门狗/毛刺/欠压）与 poweron。
	 * 刻意**不记** software / ext_pin / usb / jtag —— 那些是烧录、按键、
	 * 软重启、插拔 USB 造成的，记进去只会把真掉电淹掉。 */
	bool mark_boot = !g_boot.clean_last && g_boot.last_up > 0 &&
			 (abnormal || strcmp(reason, "poweron") == 0);

	if (mark_boot) {
		ops_ev_add(OPS_EV_ABNORMAL_REBOOT, 0, 0, (uint16_t)g_rst_code, 0, reason,
			   g_boot.last_up);
	}

	boot_save_all();
}

void ops_boot_tick(uint32_t up_s)
{
	g_boot.last_up = up_s;

	/* 前 60 s 落一次（能区分"秒崩"和"稳定"），之后每 5 分钟落一次。
	 * 不要把 60 s 一次当成常态：NVS 反复写同一键会加速磨损。 */
	if (up_s <= 60u || (up_s - g_raw.tick_saved) >= 300u) {
		g_raw.tick_saved = up_s;
		settings_save_one("boot/up", &g_boot.last_up, sizeof(g_boot.last_up));
	}
}

void ops_boot_mark_clean(void)
{
	uint32_t zero = 0;

	settings_save_one("boot/dirty", &zero, sizeof(zero));
	settings_save_one("boot/up", &g_boot.last_up, sizeof(g_boot.last_up));
	LOG_INF("boot: marked clean exit");
}

void ops_boot_json(char *buf, size_t len)
{
	snprintf(buf, len, "{\"n\":%u,\"crash\":%u,\"last_up\":%u,\"clean\":%s,\"rst\":\"%s\","
		 "\"rstc\":%d}",
		 g_boot.boots, g_boot.crashes, g_boot.last_up,
		 g_boot.clean_last ? "true" : "false", g_rst_reason, g_rst_code);
}

/* 自证窗口要用它判"候选固件是不是崩过"：OTA 签名那一刻记下当时的 crash 数，
 * 下次启动如果 crash 变大了，说明候选固件跑挂了（panic/WDT），不必等窗口走完。 */
uint32_t ops_boot_crash_count(void)
{
	return g_boot.crashes;
}

/* 窗口判"上一轮被拔电"用：掉电不会让 crash +1，只有 clean 标志会变 dirty。 */
bool ops_boot_last_clean(void)
{
	return g_boot.clean_last;
}

const char *ops_boot_rst_reason(void)
{
	return g_rst_reason;
}

uint32_t ops_boot_count(void)
{
	return g_boot.boots;
}

/* ============================================================== 栈水位 */
#if defined(CONFIG_THREAD_MONITOR) && defined(CONFIG_THREAD_STACK_INFO)
#define STK_MAX 16
struct stk_ent {
	const char *name;
	size_t unused;
};

static struct stk_ent g_stk[STK_MAX];
static uint8_t g_stk_n;

static void stk_cb(const struct k_thread *t, void *ud)
{
	size_t unused = 0;

	ARG_UNUSED(ud);

	if (g_stk_n >= STK_MAX) {
		return;
	}
	if (k_thread_stack_space_get(t, &unused) != 0) {
		return;
	}

	/* 按剩余字节插入排序，只留最紧的 STK_MAX 个 */
	uint8_t pos = g_stk_n;
	const char *nm = k_thread_name_get((struct k_thread *)t);

	while (pos > 0 && g_stk[pos - 1].unused > unused) {
		g_stk[pos] = g_stk[pos - 1];
		pos--;
	}
	g_stk[pos].name = nm ? nm : "?";
	g_stk[pos].unused = unused;
	if (g_stk_n < STK_MAX) {
		g_stk_n++;
	}
}
#endif

void ops_stack_json(char *buf, size_t len, uint8_t max_items)
{
#if defined(CONFIG_THREAD_MONITOR) && defined(CONFIG_THREAD_STACK_INFO)
	size_t used = 0;

	g_stk_n = 0;
	k_thread_foreach(stk_cb, NULL);

	if (max_items > g_stk_n) {
		max_items = g_stk_n;
	}

	used += snprintf(buf + used, len - used, "[");
	for (uint8_t i = 0; i < max_items && used < len - 1; i++) {
		used += snprintf(buf + used, len - used, "%s{\"n\":\"%s\",\"u\":%u}",
				 i ? "," : "", g_stk[i].name, (unsigned)g_stk[i].unused);
	}
	if (used < len) {
		snprintf(buf + used, len - used, "]");
	}
#else
	snprintf(buf, len, "[]");
#endif
}

/* ================================================================= 鉴权 */
#define AUTH_PW_DEFAULT "<REDACTED-PSK>"

static char g_pw[33] = AUTH_PW_DEFAULT;

static int auth_set_cb(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	char tmp[sizeof(g_pw)] = { 0 };

	if (!settings_name_steq(name, "pw", NULL)) {
		return -ENOENT;
	}
	if (len >= sizeof(tmp)) {
		return -EINVAL;
	}

	int rc = read_cb(cb_arg, tmp, len);

	if (rc < 0) {
		return rc;
	}
	tmp[len] = '\0';
	if (len == 0) {
		return -EINVAL;
	}

	memcpy(g_pw, tmp, len + 1);
	LOG_INF("auth password loaded from NVS");
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ops_auth, "auth", NULL, auth_set_cb, NULL, NULL);

const char *ops_auth_pw(void)
{
	return g_pw;
}

int ops_auth_set_pw(const char *pw)
{
	size_t n;

	if (!pw) {
		return -EINVAL;
	}
	n = strlen(pw);
	if (n < 4 || n >= sizeof(g_pw)) {
		return -EINVAL; /* 太短等于没设，太长存不下 */
	}

	memcpy(g_pw, pw, n + 1);
	return settings_save_one("auth/pw", g_pw, n + 1);
}

/* 极简 base64 解码：只给 Basic Auth 用，不追求通用（不处理空白、不做流式） */
static int b64_val(char c)
{
	if (c >= 'A' && c <= 'Z') {
		return c - 'A';
	}
	if (c >= 'a' && c <= 'z') {
		return c - 'a' + 26;
	}
	if (c >= '0' && c <= '9') {
		return c - '0' + 52;
	}
	if (c == '+') {
		return 62;
	}
	if (c == '/') {
		return 63;
	}
	return -1;
}

static int b64_decode(const char *in, size_t in_len, char *out, size_t out_max)
{
	size_t o = 0;
	size_t i = 0;

	while (i + 4 <= in_len && o + 3 <= out_max) {
		int v0 = b64_val(in[i]);
		int v1 = b64_val(in[i + 1]);

		if (v0 < 0 || v1 < 0) {
			break;
		}
		/* '=' 填充位按 0 处理，循环靠 out_max 和后续判断收敛 */
		int v2 = (in[i + 2] == '=') ? 0 : b64_val(in[i + 2]);
		int v3 = (in[i + 3] == '=') ? 0 : b64_val(in[i + 3]);

		if (v2 < 0 || v3 < 0) {
			break;
		}

		out[o++] = (char)((v0 << 2) | (v1 >> 4));
		if (in[i + 2] != '=') {
			if (o >= out_max) {
				break;
			}
			out[o++] = (char)(((v1 & 0x0F) << 4) | (v2 >> 2));
		}
		if (in[i + 3] != '=') {
			if (o >= out_max) {
				break;
			}
			out[o++] = (char)(((v2 & 0x03) << 6) | v3);
		}
		i += 4;
	}

	return (int)o;
}

bool ops_auth_ok(const char *authorization)
{
	char dec[96];
	char *colon;
	int n;

	if (!authorization) {
		return false;
	}
	/* 浏览器一律发 "Basic "，这里仍然忽略大小写，避免个别客户端踩坑 */
	if (strlen(authorization) < 7) {
		return false;
	}
	/* 手动比前 6 个字节（"Basic "，忽略大小写）：不依赖 minimal libc 的
	 * strncasecmp，那种东西在这套 libc 里不一定编得出来 */
	{
		static const char prefix[] = "basic ";

		for (int i = 0; i < 6; i++) {
			char c = authorization[i];

			if (c >= 'A' && c <= 'Z') {
				c = (char)(c + 32);
			}
			if (c != prefix[i]) {
				return false;
			}
		}
	}

	n = b64_decode(authorization + 6, strlen(authorization + 6), dec, sizeof(dec) - 1);
	if (n <= 0) {
		return false;
	}
	dec[n] = '\0';

	colon = strchr(dec, ':');
	if (!colon) {
		return false;
	}
	*colon = '\0';

	/* 用户名固定 admin：页面不给改，省得做成半吊子账户系统 */
	if (strcmp(dec, "admin") != 0) {
		return false;
	}
	return strcmp(colon + 1, g_pw) == 0;
}

/* =========================================================== reason 翻译 */
const char *ops_reason_code_txt(uint16_t code)
{
	/* IEEE 802.11-2020 Table 9-90 里真正会遇到的几个 */
	switch (code) {
	case 0:
		return "none";
	case 1:
		return "unspecified";
	case 2:
		return "auth expired";
	case 3:
		return "deauth leaving";
	case 4:
		return "inactivity";
	case 5:
		return "AP busy";
	case 6:
		return "class2 frame";
	case 7:
		return "class3 frame";
	case 8:
		return "STA leaving";
	case 9:
		return "assoc timeout";
	case 15:
		return "4-way handshake timeout";
	case 16:
		return "group key timeout";
	case 34:
		return "inactivity 34";
	case 36:
		return "peer kicked";
	default:
		return "other";
	}
}
