/*
 * wifi_link.h —— STA 链路状态机（L2，g_state 的唯一写者）
 *
 * 改造前 12 个全局对 13 个函数可写；现在它们全是 wifi_link.c 的 static，
 * 外面只能用 link_view_get() 取一份只读快照，或用 link_ev_* 推进状态。
 */
#ifndef WIFI_LINK_H_
#define WIFI_LINK_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/wifi_mgmt.h>

enum link_state {
	LINK_IDLE = 0,
	LINK_CONNECTING,
	LINK_CONNECTED,
};

struct link_view {
	int         state;
	const char *target_ssid;
	const char *link_ssid;
	const char *ip;
	const char *gw;
	const char *msg;
	const char *last_err;
	int         rssi;
	uint32_t    reconnects;
	uint32_t    connected_at;
};

void link_view_get(struct link_view *v);

/* 只读 */
struct net_if *link_sta_iface(void);
int  link_state(void);
bool link_is_connected(void);
int  link_sta_channel(void);
/* 信道对齐走到了第几步（0/1/2）：串口日志会断，/api/status 里能看到它 */
int  link_align_step(void);
bool link_target_is(const char *ssid, size_t len);
const char *link_ip(void);
void link_set_gw(const char *gw);
int  wifi_rssi(void);
const char *link_target_ssid(void);
const char *link_link_ssid(void);
const char *link_msg(void);
const char *link_err(void);
const char *link_gw(void);
uint32_t link_reconnects(void);

/* 动作 */
int  portal_disconnect(void);
int  link_request_connect(const char *ssid, size_t ssid_len,
			  const char *psk, size_t psk_len);
void link_boot_autoconnect(void);
void link_connect_now(void);
void link_connect_deferred(uint32_t delay_ms);
void link_rssi_sampling_start(void);
void link_rssi_hist_json(char *out, size_t len);

/* 事件 */
void link_ev_connect_result(int status);
void link_ev_disconnect_result(const struct wifi_status *st);
void link_ev_got_ip(struct net_if *iface, const struct in_addr *addr);

#endif /* WIFI_LINK_H_ */
