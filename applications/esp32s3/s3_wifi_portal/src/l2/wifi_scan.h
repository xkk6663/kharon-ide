/*
 * wifi_scan.h —— 扫描缓存（L2）
 *
 * 缓存由 net_mgmt 事件写、由 HTTP handler 读，所以锁是"暴露的"而不是内部消化的：
 * 调用方拿锁期间看到的是一份自洽画面。本层不吐 JSON —— 字段名属表达层。
 */
#ifndef WIFI_SCAN_H_
#define WIFI_SCAN_H_

#include <stdbool.h>
#include <stdint.h>
#include <zephyr/net/wifi_mgmt.h>

#define MAX_APS 20

struct ap_entry {
	char ssid[WIFI_SSID_MAX_LEN + 1];
	int8_t rssi;
	uint8_t channel;
	uint8_t security;
};

/* 动作 */
void wifi_scan_start(void);
void scan_cache_add(const struct wifi_scan_result *res);
void scan_event_done(void);
void scan_invalidate(void);                    /* 标记缓存不可信（年龄归零） */
uint32_t scan_last_done_ms(void);              /* 上次 SCAN_DONE 的时刻，0=从没扫完 */

/* 读（调用方负责持锁） */
void scan_lock(void);
void scan_unlock(void);
const struct ap_entry *scan_entries(void);
int  scan_count(void);
bool scan_is_active(void);
uint8_t scan_channel_of(const char *ssid);     /* 0 = 没扫到这个网络 */

#endif /* WIFI_SCAN_H_ */
