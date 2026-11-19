/*
 * time_sync —— SNTP 对时（把 HTTP Date 头也当一份可用时间源）
 *
 * L2 服务层。对时成功后除了更新自己的 UTC 基准，还负责把呼吸灯并到全局时间。
 */
#include <string.h>
#include <stdlib.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/sntp.h>
#include <zephyr/net/http/client.h>
#include <zephyr/net/socket.h>

#include "portal_cfg.h"
#include "time_sync.h"
#include "breath_sync.h"
#include "netcheck.h"
#include "led_status.h"

LOG_MODULE_REGISTER(portal_time, LOG_LEVEL_INF);

#define SNTP_SERVER_NAME "ntp.aliyun.com"
#define NETCHECK_URL     "http://www.baidu.com/"

static volatile bool g_time_ok;
static int64_t  g_unix_at_sync;      /* 同步那一刻的 UNIX 秒 */
static uint32_t g_uptime_at_sync;    /* 同步那一刻的 uptime ms */

/*
 * 呼吸灯规范 v1.0 的 UTC 估计器（docs\参考\同步呼吸灯规则.md）。
 * SNTP 对上一次就把它喂进来（breath_clock_observe_src），此后每一帧的亮度
 * 都由「绝对 UTC」算出来：相位 = UTC mod 4 s，跟本机开机多久、连了几个客户端
 * 都无关 —— 所以几块板子只要都对上表，灯就同相，不需要互相通信。
 *
 * 刚上电、还没连上路由时是「等级 0」：灯照常呼吸，但被半亮守卫压一半。
 * 这恰好和“红灯 = 还没配网”的语义一致：没时间就没法跟别的设备同拍。
 */

/* 把"对时/自检走到哪一步"暴露出去。
 * 之前只看到 clock 空、netcheck=0，DND 失败和 UDP 不通混在一起，只能靠猜；
 * 这几个字段让页面直接告诉你"名字没解开"还是"包出不去"。 */
static volatile int  g_sntp_rc = 999;          /* 999 = 还没跑完 */
static char          g_sntp_srv[24] = "-";


/* 配网页只显示"现在几点"，所以不引时区库：UTC+8 直接加 8 小时。
 * 有 SNTP 之前返回空串，前端看到空就不画这一行。 */
void clock_hms(char *buf, size_t n)
{
	if (!g_time_ok) {
		snprintf(buf, n, "");
		return;
	}

	int64_t now = g_unix_at_sync +
		      (int64_t)((k_uptime_get_32() - g_uptime_at_sync) / 1000U);
	int64_t local = now + 8 * 3600;
	int hh = (int)((local / 3600) % 24);
	int mm = (int)((local / 60) % 60);
	int ss = (int)(local % 60);

	snprintf(buf, n, "%02d:%02d:%02d", hh, mm, ss);
}

/* SNTP 的 DNS 解析在这一层做单独一步：sntp_simple() 内部只认主机名/IP，
 * 解析失败时报 -116，看起来像"网络不通"，实际是名字没解开。分开报才查得快。 */
#define SNTP_FALLBACK_IP "203.107.6.88" /* ntp.aliyun.com 的固定入口，PC 侧实测 50~104 ms */

K_THREAD_STACK_DEFINE(sntp_stack, 4096);
static struct k_thread sntp_tcb;

void sntp_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	struct sntp_time ts;
	uint64_t t0 = k_uptime_get();
	char ip[NET_IPV4_ADDR_LEN] = { 0 };
	int dns_rc = dns_resolve_host(SNTP_SERVER_NAME, ip, sizeof(ip));
	const char *server = SNTP_SERVER_NAME;

	if (dns_rc == 0) {
		server = ip;
		net_dns_note(0, ip);
		LOG_INF("SNTP: %s -> %s", SNTP_SERVER_NAME, ip);
	} else {
		/* DNS 挂了不该把对时一起废掉：直接拿固定 IP 硬连，
		 * 但 dns 这一步单独打出来，别再让 -116 背"网络不通"的锅。 */
		server = SNTP_FALLBACK_IP;
		net_dns_note(dns_rc, NULL);
		LOG_WRN("SNTP: dns failed (%d), fallback to %s", dns_rc, server);
	}

	strncpy(g_sntp_srv, server, sizeof(g_sntp_srv) - 1);

	int rc = sntp_simple(server, 8000, &ts);

	g_sntp_rc = rc;

	if (rc < 0) {
		LOG_WRN("SNTP failed: %d (after %llu ms, server %s)", rc, k_uptime_get() - t0,
			server);
		return;
	}

	/* 这版 Zephyr 的 sntp.c 在返回前已经把 OFFSET_1970_JAN_1 减掉了（见
	 * subsys/net/lib/sntp/sntp.c:172），ts.seconds 本身就是一个 UNIX 时间戳。
	 * 再减一次会直接掉进 1900 之前的负数区间 —— 实测 clock 显示成 "-8:-29:-44"。 */
	g_unix_at_sync = (int64_t)ts.seconds;
	g_uptime_at_sync = k_uptime_get_32();
	g_time_ok = true;

	/*
	 * 喂给呼吸灯规范。两个细节值得写下来：
	 *   1) UTC 必须带上 fraction（1/2^32 秒）。只用整秒的话相位误差最高 1 s，
	 *      一致性等级直接从「3（≤25 ms）」掉到「1（≤1 s）」；
	 *   2) mono 用 k_uptime_get()（毫秒精度，±1 ms 远小于 25 ms 阈值）。
	 *      两者必须是“同一瞬间”的采样，所以紧挨着取，中间不要再插别的事。
	 * rsp_delay_us 是 SNTP 估的“服务器发出→本机收到”的延迟，正好当不确定度用。
	 */
	{
		int64_t mono_us = (int64_t)k_uptime_get() * 1000;
		int64_t utc_us = (int64_t)ts.seconds * 1000000 +
				 (int64_t)(((uint64_t)ts.fraction * 1000000ull) >> 32);
		int32_t unc_us = (int32_t)ts.rsp_delay_us + 5000;

		led_clock_observe(mono_us, utc_us, BREATH_SRC_NETWORK, unc_us);
	}

	LOG_INF("SNTP ok: %s in %llu ms (rsp delay %u us)",
		SNTP_SERVER_NAME, k_uptime_get() - t0, ts.rsp_delay_us);
}

static int64_t ymd_to_epoch(int y, int mo, int d, int hh, int mm, int ss)
{
	static const int cum_days[] = { 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334 };
	bool leap = ((y % 4 == 0) && (y % 100 != 0)) || (y % 400 == 0);
	int64_t days = 0;

	for (int yy = 1970; yy < y; yy++) {
		bool ly = ((yy % 4 == 0) && (yy % 100 != 0)) || (yy % 400 == 0);

		days += ly ? 366 : 365;
	}

	if (mo >= 1 && mo <= 12) {
		days += cum_days[mo - 1];
	}
	if (mo > 2 && leap) {
		days++;
	}
	days += d - 1;

	return days * 86400LL + hh * 3600LL + mm * 60LL + ss;
}

/* minimal libc 不一定有 atoi，自己数几位就完事 */
static int scan_uint(const char **pp)
{
	int v = 0;

	while (**pp >= '0' && **pp <= '9') {
		v = v * 10 + (**pp - '0');
		(*pp)++;
	}
	/* 吃掉后面连续的空格，方便逐个字段往下走 */
	while (**pp == ' ') {
		(*pp)++;
	}

	return v;
}

/* UDP 123 被这条网络拦掉时（实测 SNTP 恒定 -116），退一步用 HTTP 响应的 Date 头对时。
 * 精度是秒级，对"配网页显示现在几点"够用，而且它顺带证明了出网 TCP 真的通了。
 * 格式固定是 RFC 1123： "Sun, 15 Nov 2026 07:12:00 GMT" */
void time_apply_http_date(const struct http_response *rsp)
{
	static const char *const mon[] = { "Jan", "Feb", "Mar", "Apr", "May", "Jun",
					   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };

	if (g_time_ok || rsp == NULL || rsp->recv_buf == NULL) {
		return;
	}

	const char *buf = (const char *)rsp->recv_buf;

	if (buf[0] != 'H') { /* 不是"HTTP/1.x ..."开头就别乱解析 */
		return;
	}

	const char *p = strstr(buf, "Date: ");

	if (p == NULL) {
		return;
	}
	p += 6;

	/* 跳过 "Sun, " 这样的星期前缀：找到第一个数字 */
	while (*p != '\0' && (*p < '0' || *p > '9')) {
		p++;
	}

	int day = scan_uint(&p); /* 之后 p 停在月份首字母 */

	int mo = 0;

	for (int i = 0; i < 12; i++) {
		if (strncmp(p, mon[i], 3) == 0) {
			mo = i + 1;
			break;
		}
	}
	if (mo == 0) {
		return;
	}
	p += 3;
	while (*p == ' ') {
		p++;
	}

	int year = scan_uint(&p); /* 之后 p 停在小时 */
	int hh = scan_uint(&p);

	if (*p == ':') {
		p++;
	}
	int mm = scan_uint(&p);

	if (*p == ':') {
		p++;
	}
	int ss = scan_uint(&p);

	if (year < 2020 || day < 1 || day > 31) {
		return;
	}

	int64_t epoch = ymd_to_epoch(year, mo, day, hh, mm, ss);

	g_unix_at_sync = epoch;
	g_uptime_at_sync = k_uptime_get_32();
	g_time_ok = true;

	/*
	 * 这条兜底通道也要喂给呼吸灯，否则会出现一个很难查的现象：
	 * 网页上时间明明是对的（g_time_ok 为真），但灯一直是半亮 ——
	 * 因为半亮守卫看的是「呼吸灯那个 UTC 估计器有没有授过时」，不是 g_time_ok。
	 *
	 * 不确定度按 1 s 记：Date 头只有整秒，HTTP 往返延迟也没测。
	 * 别传 0 —— 0 表示“用 NETWORK 的默认 25 ms”，那等于把秒级时间说成毫秒级。
	 * 结果是等级 1（能看出不齐，但确实对上了同一个时间轴）。
	 */
	{
		int64_t mono_us = (int64_t)k_uptime_get() * 1000;

		led_clock_observe(mono_us, epoch * 1000000, BREATH_SRC_NETWORK, 1000000);
	}

	LOG_INF("clock: from HTTP Date header (%04d-%02d-%02d %02d:%02d:%02d UTC)",
		year, mo, day, hh, mm, ss);
}


/* zsock_getaddrinfo 是同步封装，内部走 DNS 解析器。比 dns_resolve_name 的回调式
 * API 好写，也不用在自己线程里额外挂一个信号量去等回调。 */

/* ===================== 对外接口 ===================== */
void time_start(void)
{
	k_thread_create(&sntp_tcb, sntp_stack, K_THREAD_STACK_SIZEOF(sntp_stack),
			sntp_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(9), 0, K_NO_WAIT);
	k_thread_name_set(&sntp_tcb, "sntp");
}

bool time_is_ok(void)
{
	return g_time_ok;
}

int time_sntp_rc(void)
{
	return g_sntp_rc;
}

const char *time_sntp_srv(void)
{
	return g_sntp_srv;
}
