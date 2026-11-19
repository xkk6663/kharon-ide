/*
 * portal_ap —— SoftAP 本身 + AP 客户端表 + 信道对齐
 *
 * L2 服务层。ESP32 是单射频，AP 与 STA 必须停在同一信道；"把 AP 搬到目标
 * 信道"这件事只有这里做，wifi_link 通过 ap_retarget() 请求。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/net/dhcpv4_server.h>

#include "portal_cfg.h"
#include "portal_ap.h"
#include "wifi_scan.h"
#include "wifi_link.h"

LOG_MODULE_REGISTER(portal_ap, LOG_LEVEL_INF);

static struct net_if *ap_iface;
static struct in_addr ap_pool_addr;
/* AP_ENABLE_RESULT 可能会来多次，DHCP 服务只起一次 */
static bool ap_dhcp_started;

/* SoftAP 参数放在文件级：STA 发起连接前要按目标 AP 的信道临时的改这里的 channel。
 * ESP32 是单射频，AP 和 STA 必须停在同一信道；当 AP 上已经有客户端（手机连
 * 着配网页）时，STA 再去连别的信道的路由器会被驱动/路由器踢掉 —— 串口里
 * 表现为 connect failed status 4（ASSOC_EXPIRE），用户看到的是"连什么都连不上"。
 * 做法：连之前把 SoftAP 重启到目标信道，手机重连后两者同信道，STA 就能连了。 */
static struct wifi_connect_req_params g_ap_params;
static uint8_t g_ap_retarget_channel;
/* SoftAP 当前"认为"自己停在哪个信道。
 * 只维护内存状态、不查驱动：wifi_iface_status 对 AP 返回的是配置值（我们配 0=自动），
 * 查不到真实信道；而在系统工作队列里同步等 net_mgmt 又有卡死的风险。
 * 0 = 自动（上电默认 ch1，或跟着 STA 走，无法确定）。 */
static uint8_t g_ap_active_channel;

/* ------------------------------------------------ SoftAP 已连客户端（内存表）
 * 这一版 Zephyr 没有"查询 AP 客户端"的 net_mgmt 请求（NET_REQUEST_WIFI_AP_* 里
 * 只有 enable/disable），只能靠 NET_EVENT_WIFI_AP_STA_CONNECTED / _DISCONNECTED
 * 自己维护。而且 struct wifi_ap_sta_info 只带 MAC，没有 RSSI，也没有 IP
 * （DHCP server 侧没暴露租约查询）——所以这里能给的只有"谁连着、连了多久"。
 * 上限与 CONFIG_WIFI_MGMT_AP_MAX_NUM_STA（8）一致。 */

static struct ap_client g_ap_clients[AP_CLIENT_MAX];

/* ------------------------------------------------------- RSSI 历史（环形缓冲）
 * 只有瞬时 RSSI 看不出"信号是不是在变差"。每 10 s 采一个点，存 60 个（10 分钟），
 * 网页画成一条折线。断线期间不采样，避免把 0 当成"信号掉到 0 dBm"。 */
static void ap_reopen_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(ap_reopen_work, ap_reopen_handler);

uint8_t ap_current_channel(void)
{
	/* 优先报"STA 真实关联信道"：单射频下 STA 连着谁，AP 就在谁的信道。
	 * wifi_iface_status 对 STA 返回的是 ap_info.primary（真值），对 AP 返回的
	 * 只是我们自己的配置值（见 esp_wifi_drv.c 的 status 分支）。
	 * 拿不到才退回配置值，这比"永远 0"对页面有用得多。 */
	uint8_t pinned = ap_pinned_channel();

	if (pinned != 0) {
		return pinned;
	}

	struct net_if *sta = link_sta_iface();
	struct wifi_iface_status st;

	if (sta == NULL) {
		return 0;
	}

	memset(&st, 0, sizeof(st));
	if (net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, sta, &st, sizeof(st)) != 0) {
		return 0;
	}

	return (uint8_t)st.channel;
}

/* AP 是否被"钉"在某个具体信道。
 *
 * 0 = auto：ESP-IDF 的语义是 SoftAP 跟着 STA 走，这时**不需要**任何人去搬信道，
 * 强行 retarget 只会让 AP 关 900 ms 再开 1500 ms、把已连的手机踢掉。
 * 只有一个例外才 pin：连了两次都失败（见 wifi_link.c 的 ALIGN_STEP_MAX），
 * 说明驱动确实不肯自己跟，那时才把 AP 摁到目标信道。 */
uint8_t ap_pinned_channel(void)
{
	if (g_ap_params.ssid == NULL || g_ap_params.channel == WIFI_CHANNEL_ANY) {
		return 0;
	}

	return (uint8_t)g_ap_params.channel;
}


/* 从扫描缓存里查这个 SSID 用哪个信道；没扫过就返回 0（不知道，不做对齐） */
uint8_t ap_channel_of_ssid(const char *ssid)
{
	uint8_t ch = 0;

	scan_lock();

	for (int i = 0; i < scan_count(); i++) {
		if (strcmp(scan_entries()[i].ssid, ssid) == 0) {
			ch = scan_entries()[i].channel;
			break;
		}
	}

	scan_unlock();

	return ch;
}

/* 主动断开 STA。网页按钮和串口 "portal drop" 共用这一条路径。
 * 关键：先把重连状态关掉并把 g_user_disconnect 置位，否则
 * DISCONNECT_RESULT 会立刻按"掉线"走退避重连，用户点了断开等于没点。 */
void ap_client_add(const struct wifi_ap_sta_info *info)
{
	if (info == NULL || info->mac_length != 6) {
		return;
	}

	uint32_t now = k_uptime_get_32();
	int free_idx = -1;
	int oldest = 0;

	for (int i = 0; i < AP_CLIENT_MAX; i++) {
		if (g_ap_clients[i].used &&
		    memcmp(g_ap_clients[i].mac, info->mac, 6) == 0) {
			g_ap_clients[i].joined_at = now;   /* 重连，刷新时长 */
			return;
		}
		if (!g_ap_clients[i].used && free_idx < 0) {
			free_idx = i;
		}
		if (g_ap_clients[i].used && g_ap_clients[i].joined_at < g_ap_clients[oldest].joined_at) {
			oldest = i;
		}
	}

	if (free_idx < 0) {
		free_idx = oldest;   /* 满了，顶掉最老的 */
	}

	g_ap_clients[free_idx].used = true;
	memcpy(g_ap_clients[free_idx].mac, info->mac, 6);
	g_ap_clients[free_idx].joined_at = now;
}

void ap_client_remove(const struct wifi_ap_sta_info *info)
{
	if (info == NULL || info->mac_length != 6) {
		return;
	}

	for (int i = 0; i < AP_CLIENT_MAX; i++) {
		if (g_ap_clients[i].used &&
		    memcmp(g_ap_clients[i].mac, info->mac, 6) == 0) {
			g_ap_clients[i].used = false;
			return;
		}
	}
}

/* ------------------------------------------------------------------ 启动 */
	void enable_softap(void)
{
	struct net_if *iface = ap_iface_get();

	if (iface == NULL) {
		LOG_ERR("SoftAP iface not available, skip AP bring-up");
		return;
	}

	/* 首次调用时把参数填好；之后 channel 会被信道对齐逻辑改动 */
	if (g_ap_params.ssid == NULL) {
		g_ap_params.ssid = (const uint8_t *)AP_SSID;
		g_ap_params.ssid_length = sizeof(AP_SSID) - 1;
		g_ap_params.psk = (const uint8_t *)AP_PSK;
		g_ap_params.psk_length = sizeof(AP_PSK) - 1;
		g_ap_params.security = WIFI_SECURITY_TYPE_PSK;
		g_ap_params.channel = WIFI_CHANNEL_ANY;
		g_ap_params.band = WIFI_FREQ_BAND_2_4_GHZ;
	}

	struct in_addr addr, mask;

	net_addr_pton(AF_INET, AP_IP, &addr);
	net_addr_pton(AF_INET, AP_NETMASK, &mask);
	/* DHCP 池起点先记下来，等 AP_ENABLE_RESULT 再真正启动服务 */
	net_addr_pton(AF_INET, AP_POOL, &ap_pool_addr);

	net_if_ipv4_set_gw(iface, &addr);
	if (net_if_ipv4_addr_add(iface, &addr, NET_ADDR_MANUAL, 0) == NULL) {
		LOG_ERR("cannot set AP ip");
	}
	net_if_ipv4_set_netmask_by_addr(iface, &addr, &mask);

	int ret = net_mgmt(NET_REQUEST_WIFI_AP_ENABLE, iface, &g_ap_params,
			   sizeof(g_ap_params));

	/* 记住这次 SoftAP 停在哪儿：指定了信道就记信道，ANY 记 0（不确定） */
	g_ap_active_channel = (g_ap_params.channel == WIFI_CHANNEL_ANY) ?
			      0 : (uint8_t)g_ap_params.channel;

	if (ret) {
		LOG_ERR("AP enable failed (%d)", ret);
	}
}

/* 把 SoftAP 重启到 g_ap_retarget_channel 指定的信道，然后接着发起 STA 连接 */
static void ap_reopen_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	g_ap_params.channel = g_ap_retarget_channel;

	/* AP 要重来一遍，DHCP server 得跟着重启，否则手机重连后拿不到地址 */
	if (ap_dhcp_started) {
		net_dhcpv4_server_stop(ap_iface);
		ap_dhcp_started = false;
	}

	LOG_INF("reopening SoftAP on channel %u", (unsigned)g_ap_retarget_channel);
	enable_softap();

	link_connect_deferred(1500);
}

/* --------------------------------------------------- 上电自检（软件前提）
 * 在 AP 网段里向自己的 HTTP server 发一次 GET，用来验证静态 IP + HTTP server
 * 真的通了 —— 不用等手机接进来就能看出问题。
 */

/* ===================== 对外接口 ===================== */
int ap_client_count(void)
{
	int n = 0;

	for (int i = 0; i < AP_CLIENT_MAX; i++) {
		if (g_ap_clients[i].used) {
			n++;
		}
	}

	return n;
}

const struct ap_client *ap_client_at(int i)
{
	if (i < 0 || i >= AP_CLIENT_MAX || !g_ap_clients[i].used) {
		return NULL;
	}

	return &g_ap_clients[i];
}

/* 把 AP 搬到一个指定信道：唯一的入口。
 * 调用方（wifi_link）只表达"我要连的这个网络在 ch"，不碰 g_ap_params。 */
int ap_retarget(uint8_t ch)
{
	g_ap_retarget_channel = ch;

	/* 单射频：STA 连上之前必须先让 AP 停在目标信道，否则 STA 扫不到。
	 * 先关后开，异步等 900 ms —— 关得太快会让已连的手机立刻掉。 */
	if (net_mgmt(NET_REQUEST_WIFI_AP_DISABLE, ap_iface, NULL, 0)) {
		LOG_WRN("AP disable failed before retarget");
	}

	k_work_reschedule(&ap_reopen_work, K_MSEC(900));

	return 0;
}

/* AP_ENABLE_RESULT：第一次启用才起 DHCP server（重启 AP 时由 ap_reopen_handler 自管） */
void ap_ev_enabled(struct net_if *iface)
{
	if (iface != ap_iface || ap_dhcp_started) {
		return;
	}

	if (net_dhcpv4_server_start(ap_iface, &ap_pool_addr) < 0) {
		LOG_ERR("DHCPv4 server start failed");
		return;
	}

	ap_dhcp_started = true;
	LOG_INF("DHCPv4 server up on %s", AP_IP);
}

void ap_ev_client_add(const struct wifi_ap_sta_info *info)
{
	ap_client_add(info);

	if (info != NULL && info->mac_length == 6) {
		LOG_INF("AP client joined: %02x:%02x:%02x:%02x:%02x:%02x",
			info->mac[0], info->mac[1], info->mac[2],
			info->mac[3], info->mac[4], info->mac[5]);
	}
}

void ap_ev_client_remove(const struct wifi_ap_sta_info *info)
{
	ap_client_remove(info);

	if (info != NULL && info->mac_length == 6) {
		LOG_INF("AP client left: %02x:%02x:%02x:%02x:%02x:%02x",
			info->mac[0], info->mac[1], info->mac[2],
			info->mac[3], info->mac[4], info->mac[5]);
	}
}

/* 惰性解析 SoftAP 接口。
 *
 * 为什么不能只在某个事件里赋值：分层改造前 main.c 在启动早期做了
 *   ap_iface = net_if_get_wifi_sap();
 *   sta_iface = net_if_get_wifi_sta();
 * 这两句在拆分时被漏掉了，结果 ap_iface 恒为 NULL —— 启动日志打
 * "APSTA interfaces not available (ap=0 sta=0)" 后 main 直接 return 0，
 * 板子连 Wi-Fi 都不起（OTA 新固件刷上去就是这种"能启动、没网络"的状态）。
 * 现在的做法：谁第一次问接口就当场向驱动要一次，拿到就缓存。
 * 这一步只在 main 线程/系统工作队列里发生，不涉及并发写。 */
struct net_if *ap_iface_get(void)
{
	if (ap_iface == NULL) {
		ap_iface = net_if_get_wifi_sap();
	}
	return ap_iface;
}
