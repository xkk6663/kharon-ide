/*
 * portal_ap.h —— SoftAP + AP 客户端表（L2）
 *
 * ESP32 单射频：AP 与 STA 必须同信道。所以"把 AP 搬到目标信道"是一个**动作**
 * 而不是一个字段 —— wifi_link 只能 ap_retarget(ch)，不能自己改配置。
 */
#ifndef PORTAL_AP_H_
#define PORTAL_AP_H_

#include <stdbool.h>
#include <stdint.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/wifi_mgmt.h>

#define AP_CLIENT_MAX 8

struct ap_client {
	bool used;
	uint8_t mac[6];
	uint32_t joined_at;
};

/* 动作 */
struct net_if *ap_iface_get(void);
void enable_softap(void);
int  ap_retarget(uint8_t ch);

/* 读 */
uint8_t ap_current_channel(void);     /* 优先报 STA 真实信道，0 = 不知道 */
uint8_t ap_pinned_channel(void);      /* 0 = auto（不用搬信道），非 0 = 被钉住 */
uint8_t ap_channel_of_ssid(const char *ssid);
int  ap_client_count(void);
const struct ap_client *ap_client_at(int i);

/* 事件（由 main.c 的分派器调用） */
void ap_ev_enabled(struct net_if *iface);
void ap_ev_client_add(const struct wifi_ap_sta_info *info);
void ap_ev_client_remove(const struct wifi_ap_sta_info *info);

#endif /* PORTAL_AP_H_ */
