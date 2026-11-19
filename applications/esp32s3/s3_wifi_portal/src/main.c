/*
 * main —— L3 编排层（本工程唯一"知道全局顺序"的地方）
 *
 * 改造前：这个文件 4399 行、83 个函数、104 个全局，任何两层之间都能互相摸。
 * 改造后：本文件只剩三件事
 *   1) 注册 net_mgmt 回调，把事件**翻译**成模块调用（不写业务逻辑）；
 *   2) 上电时序：等驱动 → 开 AP → DNS 劫持 → HTTP → 自检 → 读 NVS → 起 OTA → 自动连；
 *   3) 把"谁先谁后"这件事单独留在这里 —— 顺序是产品行为，不属于任何一层服务。
 *
 * 依赖方向：main → L1(selftest) / L2(全部服务) / L0(ops, ota)，不反向。
 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/net/http/server.h>
#include <zephyr/settings/settings.h>

#include "portal_cfg.h"
#include "cred.h"
#include "wifi_scan.h"
#include "wifi_link.h"
#include "portal_ap.h"
#include "time_sync.h"
#include "netcheck.h"
#include "led_status.h"
#include "dns_hijack.h"
#include "selftest.h"
#include "ota.h"
#include "ota_cloud.h"
#include "cloud_ops.h"
#include "ops.h"

LOG_MODULE_REGISTER(portal, LOG_LEVEL_INF);

/* ---------------------------------------------------------------- 事件分派
 * 这里只做"事件 → 谁负责"的映射。原来每个 case 里塞着十几行状态赋值，
 * 现在一行调用；想知道连上之后做了什么，去 wifi_link.c 看 link_ev_connect_result。
 */
static void wifi_event_handler(struct net_mgmt_event_callback *cb, uint64_t event,
			       struct net_if *iface)
{
	switch (event) {
	case NET_EVENT_WIFI_SCAN_RESULT:
		scan_cache_add((const struct wifi_scan_result *)cb->info);
		break;

	case NET_EVENT_WIFI_SCAN_DONE:
		/* 扫描结束必须复位"正在扫描"，否则之后的扫描请求会被一直吃掉。
		 * 这件事属于扫描缓存，不属于分派器。 */
		scan_event_done();
		break;

	case NET_EVENT_WIFI_CONNECT_RESULT: {
		const struct wifi_status *st = (const struct wifi_status *)cb->info;

		link_ev_connect_result(st ? st->status : 0);
		break;
	}

	case NET_EVENT_WIFI_DISCONNECT_RESULT:
		link_ev_disconnect_result((const struct wifi_status *)cb->info);
		break;

	case NET_EVENT_WIFI_AP_ENABLE_RESULT:
		LOG_INF("SoftAP \"%s\" is up, join it and open http://%s.local/", AP_SSID,
			CONFIG_NET_HOSTNAME);
		/* DHCP 服务必须在 AP 真正 up 之后才能起，提前调会返回失败 */
		ap_ev_enabled(iface);
		break;

	case NET_EVENT_WIFI_AP_STA_CONNECTED:
		ap_ev_client_add((const struct wifi_ap_sta_info *)cb->info);
		break;

	case NET_EVENT_WIFI_AP_STA_DISCONNECTED:
		ap_ev_client_remove((const struct wifi_ap_sta_info *)cb->info);
		break;

	default:
		break;
	}
}

/* 拿到 STA IP 之后的四步：记 IP → 身份自检 → 设默认出口 → 补默认路由 → 起对时/自检。
 * 每一步都落在一个模块里，顺序由这里决定（"先能出网，才值得对时"）。 */
static void ipv4_event_handler(struct net_mgmt_event_callback *cb, uint64_t event,
			       struct net_if *iface)
{
	if (event != NET_EVENT_IPV4_ADDR_ADD || iface != link_sta_iface()) {
		return;
	}

	const struct in_addr *addr = (const struct in_addr *)cb->info;

	if (addr == NULL) {
		return;
	}

	link_ev_got_ip(iface, addr);

	/* 局域网侧的身份自检：连自己的 STA 地址，期望响应头 X-Portal-Scope: dev */
	http_self_test_scope(link_ip(), "dev");

	/* 开发者接口的自检只能从这里做：DEV_GUARD 规定它们对 AP 侧一律回 404，
	 * 从 192.168.4.1 打过去只会得到三条假失败（改造前日志里那三条 404 就是
	 * 这么来的，看着像"接口没注册"，其实是权限分层的正常行为）。
	 * 口令取运行中的 ops_auth_pw()，别把默认口令抄第二份。 */
	{
		char path[96];

		snprintf(path, sizeof(path), "/api/ota/slots?k=%s", ops_auth_pw());
		http_self_test_host(link_ip(), path);
		snprintf(path, sizeof(path), "/api/drops?k=%s", ops_auth_pw());
		http_self_test_host(link_ip(), path);
	}

	/* AP 和 STA 同时在线，默认 net_if 不一定是 STA（实测是 AP）。
	 * 而 sntp_simple() / http_client_req() 这类没有 iface 参数的 API，
	 * 出网时按默认接口走 —— 走 AP 就永远出不去，表现为 SNTP -ETIMEDOUT、
	 * HTTP -EINVAL 且耗时接近 0。把 STA 显式设为默认接口即可。 */
	net_if_set_default(iface);

	char gw[NET_IPV4_ADDR_LEN] = "";

	ensure_default_route(iface, link_ip(), gw, sizeof(gw));
	link_set_gw(gw);

	/* 有 IP 才算真的能出网，这时才值得去对时、去发探测请求 */
	start_net_services();
}

/* ---------------------------------------------------------------- 启动时序 */
int main(void)
{
	static struct net_mgmt_event_callback wifi_cb;
	static struct net_mgmt_event_callback ipv4_cb;

	LOG_INF("s3_wifi_portal booting ...");

	/* 呼吸灯先自由运行：此时还没连上路由，等级 0（灯半亮）。
	 * SNTP 成功那一刻起 time_sync 会把真实 UTC 喂进去，灯并到全局时间并恢复满亮。 */
	led_clock_start();

	net_mgmt_init_event_callback(&wifi_cb, wifi_event_handler,
				     NET_EVENT_WIFI_SCAN_RESULT |
				     NET_EVENT_WIFI_SCAN_DONE |
				     NET_EVENT_WIFI_CONNECT_RESULT |
				     NET_EVENT_WIFI_DISCONNECT_RESULT |
				     NET_EVENT_WIFI_AP_ENABLE_RESULT |
				     NET_EVENT_WIFI_AP_STA_CONNECTED |
				     NET_EVENT_WIFI_AP_STA_DISCONNECTED);
	net_mgmt_add_event_callback(&wifi_cb);

	net_mgmt_init_event_callback(&ipv4_cb, ipv4_event_handler, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);

	/* 接口对象归各自模块持有：AP 归 portal_ap，STA 归 wifi_link。
	 * 这里只是问它们"拿到没有"，不再自己存两个全局。 */
	if (ap_iface_get() == NULL || link_sta_iface() == NULL) {
		LOG_ERR("APSTA interfaces not available (ap=%p sta=%p)",
			ap_iface_get(), link_sta_iface());
		return 0;
	}

	/* Wi-Fi 驱动初始化要一点时间，等一下再开 AP */
	k_msleep(2000);

	enable_softap();

	/* AP 起来之后再开 DNS 劫持（线程里会 bind udp/53） */
	start_dns_hijack();

	int ret = http_server_start();

	if (ret) {
		LOG_ERR("HTTP server start failed (%d)", ret);
	}

	/* 把 NVS 里的凭据读回来；settings_load() 会回调 cred_settings_set() */
	ret = settings_subsys_init();
	if (ret) {
		LOG_ERR("settings init failed (%d)", ret);
	} else {
		ret = settings_load();
		if (ret) {
			LOG_ERR("settings load failed (%d)", ret);
		}
	}

	/* 启动/崩溃计数必须排在 settings_load() 之后：它靠 NVS 里上次留下的
	 * dirty 标志判断"上一次是不是异常退出"。 */
	ops_boot_init();

	/* 旧固件那张表已经（可能）在凭据表里了，搬到 wifi_credentials 存储去 */
	cred_migrate_legacy();

	/* OTA 接收端必须在 settings_load() 之后起：它要从 settings 里把上次用过的
	 * nonce 读回来，早于这里启动会退回 0，防重放就形同虚设。 */
	ota_srv_start();

	/* 公网 OTA 拉取端：settings_load() 之后起（token/nonce 都依赖 NVS）。
	 * 周期 check 由专属线程驱动，这里只做启动与 TLS 凭据注册。 */
	/* l0 的命令执行器不认识 l2 —— 把真实函数在这里挂进去（分层防火墙：
	 * l0 只看函数指针，不 include l2 的头）。 */
	{
		static const struct cloud_ops_hooks cloud_hooks = {
			.ip = link_ip,
			.ssid = link_target_ssid,
			.rssi = wifi_rssi,
			.net_http = net_http_status,
			.net_dns_rc = net_dns_rc,
			.led_test = led_status_test,
		};

		cloud_ops_bind(&cloud_hooks);
	}

	ota_cloud_start();

	/* 有已保存网络就自动连：逐条轮，每条试满 AUTO_RETRY_MAX 次再换下一条。
	 *
	 * ★ 这一步必须紧跟 settings_load()，不能排在那 5 个自检后面：
	 * 自检验的是"AP 网段的 HTTP 能不能通"，跟 STA 什么时候能出网无关，
	 * 把它们排在连接前面，等于让用户多等 4 s（实测 6.56 s 才开始连）。
	 * 连接本身跑在系统工作队列里，主线程后面做自检不会挡住它。 */
	link_boot_autoconnect();

	/* RSSI 采样：先排一次，之后就由 handler 自己 reschedule 下去 */
	link_rssi_sampling_start();

	LOG_INF("portal ready: AP=%s  url=http://%s.local/", AP_SSID, CONFIG_NET_HOSTNAME);

	/* 上电自检挪到连接发起之后（它只验 AP 侧）。
	 * 这里刻意不请求 /api/scan —— 那会真的发起一次全信道扫描（实测 ~10 s），
	 * 正好和紧接着的 STA 连接撞车，还白白清空扫描缓存。
	 * 开发者接口（/api/shell、/api/ota/slots、/api/drops）不在这里验：
	 * DEV_GUARD 规定它们对 AP 侧一律回 404，从 192.168.4.1 打过去只会得到
	 * 三条假失败；它们改在拿到 STA IP 之后从局域网侧验（见 ipv4_event_handler）。 */
	k_msleep(500);
	http_self_test("/api/wifi");
	http_self_test_scope(AP_IP, "user");
	k_msleep(1500);
	http_self_test("/api/saved");
	k_msleep(1500);
	http_self_test("/generate_204");
	dns_self_test();

	return 0;
}
