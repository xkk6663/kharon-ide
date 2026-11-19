/*
 * wifi_scan —— 扫描结果缓存（唯一持有 g_aps / g_ap_count / g_scanning 的地方）
 *
 * L2 服务层。net_mgmt 的扫描事件推进这里的状态，L1 只通过 scan_* 取数。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/wifi_mgmt.h>

#include "portal_cfg.h"
#include "wifi_scan.h"
#include "wifi_link.h"

LOG_MODULE_REGISTER(portal_scan, LOG_LEVEL_INF);



static struct ap_entry g_aps[MAX_APS];
static int g_ap_count;
static volatile bool g_scanning;
static uint32_t g_scan_deadline;
/* 最后一次收到 SCAN_DONE 的时刻。连接流程要靠它判断"缓存里的信道还能不能信"：
 * 缓存被新扫描清空、或扫描被连接打断没等到 SCAN_DONE 时，缓存是"空但不能代表
 * 附近没有这个网络"，只有时间戳能区分这两种情况。 */
static uint32_t g_scan_last_done;
static K_MUTEX_DEFINE(ap_lock);

static void scan_cache_reset(void)
{
	k_mutex_lock(&ap_lock, K_FOREVER);
	g_ap_count = 0;
	k_mutex_unlock(&ap_lock);
}

void scan_cache_add(const struct wifi_scan_result *res)
{
	if (res == NULL || res->ssid_length == 0 || res->ssid_length > WIFI_SSID_MAX_LEN) {
		return;
	}

	k_mutex_lock(&ap_lock, K_FOREVER);

	/* 同名的只留信号最强的那条，列表更短更好点 */
	for (int i = 0; i < g_ap_count; i++) {
		if (strncmp(g_aps[i].ssid, (const char *)res->ssid, res->ssid_length) == 0 &&
		    strlen(g_aps[i].ssid) == res->ssid_length) {
			if (res->rssi > g_aps[i].rssi) {
				g_aps[i].rssi = res->rssi;
			}
			k_mutex_unlock(&ap_lock);
			return;
		}
	}

	if (g_ap_count < MAX_APS) {
		memcpy(g_aps[g_ap_count].ssid, res->ssid, res->ssid_length);
		g_aps[g_ap_count].ssid[res->ssid_length] = '\0';
		g_aps[g_ap_count].rssi = res->rssi;
		g_aps[g_ap_count].channel = res->channel;
		g_aps[g_ap_count].security = (uint8_t)res->security;
		g_ap_count++;
	}

	k_mutex_unlock(&ap_lock);
}

void wifi_scan_start(void)
{
	if (link_sta_iface() == NULL) {
		return;
	}

	/* 正在扫描就别重复发请求；但超过 deadline 还没等到 SCAN_DONE
	 * （扫描可能被连接流程打断）就兜底复位，允许重发一次。 */
	if (g_scanning && k_uptime_get_32() < g_scan_deadline) {
		return;
	}

	scan_cache_reset();
	g_scanning = true;
	/* ESP32 全信道（1~13）主动扫描实测要 ~10 s 才回 SCAN_DONE。
	 * deadline 必须大于这个值，否则页面轮询到 8 s 就判"超时"、重新发起扫描，
	 * 把刚扫到的结果 reset 掉，永远拿不到列表。 */
	g_scan_deadline = k_uptime_get_32() + 25000;

	int ret = net_mgmt(NET_REQUEST_WIFI_SCAN, link_sta_iface(), NULL, 0);

	if (ret) {
		LOG_WRN("scan request failed (%d)", ret);
		g_scanning = false;
	}
}

/* --------------------------------------------------------------- 连接动作 */

/* ===================== 对外接口：锁 + 快照 ===================== */
/* 锁是暴露的，不是内部消化：调用方拿锁期间看到的是一个自洽的画面，
 * 而不是"扫到一半"的数组。L2 不吐 JSON —— 字段名属于表达层。 */
void scan_lock(void)
{
	k_mutex_lock(&ap_lock, K_FOREVER);
}

void scan_unlock(void)
{
	k_mutex_unlock(&ap_lock);
}

const struct ap_entry *scan_entries(void)
{
	return g_aps;
}

int scan_count(void)
{
	return g_ap_count;
}

bool scan_is_active(void)
{
	return g_scanning && k_uptime_get_32() < g_scan_deadline;
}

uint8_t scan_channel_of(const char *ssid)
{
	uint8_t ch = 0;

	scan_lock();
	for (int i = 0; i < g_ap_count; i++) {
		if (strcmp(g_aps[i].ssid, ssid) == 0) {
			ch = g_aps[i].channel;
			break;
		}
	}
	scan_unlock();

	return ch;
}

void scan_event_done(void)
{
	g_scanning = false;
	g_scan_last_done = k_uptime_get_32();
	LOG_INF("scan done: %d AP(s) cached", g_ap_count);
}

/* 把缓存标成"不可信"。
 *
 * 连接失败（status 1/4）之后必须调一次：那时缓存要么是空的、要么是上一次扫描
 * 留下的旧信道。旧信道比空缓存更危险 —— 它会让人以为"这个网络就在 chX"，
 * 从而跳过重新扫描。
 *
 * 注意这里**不清数组**：清空是 wifi_scan_start() 的事。本函数只让"年龄"归零，
 * 让调用方按 CHANNEL_CACHE_TTL_MS 自己判断还要不要用。 */
void scan_invalidate(void)
{
	g_scan_last_done = 0;
}

/* 距离上一次 SCAN_DONE 过了多少毫秒；0 表示"从来没有成功扫完过"。 */
uint32_t scan_last_done_ms(void)
{
	return g_scan_last_done;
}
