/*
 * cred.h —— 凭据表（L2）
 *
 * 边界：外部只能"数一数有几条、取第几条、增删改、记一条 last-good"，
 * 拿不到表本身；明文密码只在本层与 wifi_link 之间流动。
 */
#ifndef CRED_H_
#define CRED_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/net/wifi_mgmt.h>

#define CRED_MAX 8

struct cred_item {
	uint8_t ssid_len;
	uint8_t psk_len;
	char ssid[WIFI_SSID_MAX_LEN + 1];
	char psk[65];
};

/* 读 */
uint8_t cred_count(void);
const struct cred_item *cred_at(int idx);      /* NULL = 越界 */
int     cred_index(const char *ssid, size_t ssid_len);

/* 写 */
int  cred_upsert(const char *ssid, size_t ssid_len, const char *psk, size_t psk_len);
bool cred_remove(const char *ssid, size_t ssid_len);
void cred_mark_last_good(const char *ssid, size_t len);
void cred_migrate_legacy(void);

#endif /* CRED_H_ */
