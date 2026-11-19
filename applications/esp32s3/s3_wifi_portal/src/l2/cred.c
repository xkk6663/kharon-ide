/*
 * cred —— 凭据表（NVS settings 里的 "portal/wifi" + 官方 wifi_credentials 存储）
 *
 * L2 服务层。只回答"存了哪些网络、第几条是谁"，不知道 HTTP、不知道串口，
 * 更不知道"现在该连谁"——那是 wifi_link 的事。
 */
#include <string.h>
#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/wifi_credentials.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/settings/settings.h>

#include "portal_cfg.h"
#include "cred.h"

LOG_MODULE_REGISTER(portal_cred, LOG_LEVEL_INF);



#define CRED_TABLE_MAGIC 0xC7

struct cred_table {
	uint8_t magic;   /* CRED_TABLE_MAGIC：用来和旧固件的单条格式区分 */
	uint8_t count;
	struct cred_item items[CRED_MAX];
};

#define CRED_SETTINGS_KEY "portal/wifi"
/* 上次真正连成功的 SSID：只用来决定上电先连谁（见 cred_store_sync 尾部） */
#define LASTOK_SETTINGS_KEY "portal/lastok"

static struct cred_table g_creds = { .magic = CRED_TABLE_MAGIC, .count = 0 };
int cred_index(const char *ssid, size_t ssid_len)
{
	if (ssid_len == 0) {
		return -1;
	}

	for (int i = 0; i < g_creds.count; i++) {
		if (g_creds.items[i].ssid_len == ssid_len &&
		    memcmp(g_creds.items[i].ssid, ssid, ssid_len) == 0) {
			return i;
		}
	}

	return -1;
}

/* 把一条凭据写进 Zephyr 官方 wifi_credentials 存储（settings -> NVS）。
 * 同 SSID 覆盖不新增，因此这里可以无脑调用。 */
static int cred_store_save(const struct cred_item *it)
{
	struct wifi_credentials_personal cred;

	memset(&cred, 0, sizeof(cred));
	cred.header.type = (it->psk_len > 0) ? WIFI_SECURITY_TYPE_PSK : WIFI_SECURITY_TYPE_NONE;
	memcpy(cred.header.ssid, it->ssid, it->ssid_len);
	cred.header.ssid_len = it->ssid_len;
	cred.header.timeout = CONNECT_TIMEOUT_MS;
	cred.password_len = it->psk_len;
	memcpy(cred.password, it->psk, it->psk_len);

	int err = wifi_credentials_set_personal_struct(&cred);

	if (err) {
		LOG_ERR("wifi_credentials_set failed for \"%s\" (%d)", it->ssid, err);
	}

	return err;
}

/* 把一条凭据放到表头（最近使用优先），密码随之更新；表满时挤掉最旧的一条 */
int cred_upsert(const char *ssid, size_t ssid_len, const char *psk, size_t psk_len)
{
	if (ssid_len == 0 || ssid_len > WIFI_SSID_MAX_LEN || psk_len > 64) {
		return -EINVAL;
	}

	int idx = cred_index(ssid, ssid_len);

	if (idx == 0 && g_creds.items[0].psk_len == psk_len &&
	    memcmp(g_creds.items[0].psk, psk, psk_len) == 0) {
		/* 已经在表头、密码也没变：不用重复写 flash（模组 flash 寿命有限） */
		return 0;
	}

	struct cred_item item = {
		.ssid_len = (uint8_t)ssid_len,
		.psk_len = (uint8_t)psk_len,
	};

	memcpy(item.ssid, ssid, ssid_len);
	item.ssid[ssid_len] = '\0';
	memcpy(item.psk, psk, psk_len);
	item.psk[psk_len] = '\0';

	if (idx < 0) {
		if (g_creds.count < CRED_MAX) {
			g_creds.count++;
		} else {
			/* 表满：下面 memmove 会把最旧一条（items[CRED_MAX-1]）挤掉，
			 * 官方存储里那条也要同步删掉，否则内存和 NVS 会对不上 */
			wifi_credentials_delete_by_ssid(g_creds.items[CRED_MAX - 1].ssid,
							g_creds.items[CRED_MAX - 1].ssid_len);
		}
		/* 表满时这里会把最旧的一条（items[CRED_MAX-1]）挤掉 */
		memmove(&g_creds.items[1], &g_creds.items[0],
			(g_creds.count - 1) * sizeof(struct cred_item));
	} else {
		memmove(&g_creds.items[1], &g_creds.items[0], idx * sizeof(struct cred_item));
	}

	g_creds.items[0] = item;

	LOG_INF("credential remembered: \"%s\" (%u saved)", item.ssid, g_creds.count);

	/* 落盘交给官方库；内存表只作为 UI/自动连接的快照 */
	return cred_store_save(&g_creds.items[0]);
}

bool cred_remove(const char *ssid, size_t ssid_len)
{
	int idx = cred_index(ssid, ssid_len);

	if (idx < 0) {
		return false;
	}

	memmove(&g_creds.items[idx], &g_creds.items[idx + 1],
		(g_creds.count - idx - 1) * sizeof(struct cred_item));
	g_creds.count--;
	memset(&g_creds.items[g_creds.count], 0, sizeof(struct cred_item));

	int err = wifi_credentials_delete_by_ssid(ssid, ssid_len);

	if (err && err != -ENOENT) {
		LOG_ERR("wifi_credentials_delete failed for \"%s\" (%d)", ssid, err);
	}

	LOG_INF("credential forgotten (%u left)", g_creds.count);

	return true;
}

static bool cred_item_valid(const struct cred_item *it)
{
	return it->ssid_len > 0 && it->ssid_len <= WIFI_SSID_MAX_LEN &&
	       it->psk_len <= 64 && it->ssid[it->ssid_len] == '\0' &&
	       it->psk[it->psk_len] == '\0';
}

/* settings 在 settings_load() 时会回调这里，把键 "wifi" 的值读出来 */
/* 上电该先连哪个网络。
 *
 * 为什么不能只靠"内存表表头"：真值在 wifi_credentials 里，
 * cred_store_sync() 重建 g_creds 时用的是库内部的遍历顺序（插入/轮换序），
 * 而不是我们 upsert 时调过的顺序。实测重启后日志是
 *   wifi_credentials: 3 saved network(s) → default network for boot: "501A"
 * 即"刚连成功的 Power"没被当成上电目标，板子先对 501A 连 3 次
 * （每次 status 4，白等约 15 s）才轮到真正在线的那个。
 * 所以单独记一个"上次连成功的是谁"，重启时把它排到表头。 */
static char g_lastok_ssid[WIFI_SSID_MAX_LEN + 1];
static uint8_t g_lastok_len;

static int cred_settings_set(const char *name, size_t len, settings_read_cb read_cb,
			     void *cb_arg)
{
	if (strcmp(name, "lastok") == 0) {
		if (len == 0 || len > WIFI_SSID_MAX_LEN) {
			return -EINVAL;
		}
		int rc = read_cb(cb_arg, g_lastok_ssid, len);

		if (rc < 0) {
			return rc;
		}
		g_lastok_ssid[len] = '\0';
		g_lastok_len = (uint8_t)len;

		return 0;
	}

	if (strcmp(name, "wifi") != 0) {
		return -ENOENT;
	}

	if (len == sizeof(struct cred_table)) {
		struct cred_table t;
		int rc = read_cb(cb_arg, &t, sizeof(t));

		if (rc < 0) {
			return rc;
		}

		if (t.magic != CRED_TABLE_MAGIC || t.count > CRED_MAX) {
			LOG_WRN("stored credential table is corrupted");
			return -EINVAL;
		}

		g_creds = t;

		/* 把表头那条填进当前连接参数：上电就自动连最近用过的那个网络 */
		/* 表头那条就是"最近用过的网络"。这里只报告，不再直接改连接参数 ——
		 * 真正发起连接是 main 的启动序列调 link_boot_autoconnect()，
		 * 凭据层不该知道有"目标 SSID"这回事。 */
		if (t.count > 0 && cred_item_valid(&t.items[0])) {
			LOG_INF("NVS credentials loaded: %u saved, latest \"%s\"",
				t.count, t.items[0].ssid);
		}

		return 0;
	}

	if (len == sizeof(struct cred_item)) {
		/* 旧固件只存一条（sizeof(cred_item) 字节）。就地升级成新表格式，
		 * 用户不必重新配网。真正的落盘留给 main（settings_load 期间不急）。 */
		struct cred_item it;
		int rc = read_cb(cb_arg, &it, sizeof(it));

		if (rc < 0) {
			return rc;
		}

		if (!cred_item_valid(&it)) {
			LOG_WRN("legacy credential is corrupted");
			return -EINVAL;
		}

		memset(&g_creds, 0, sizeof(g_creds));
		g_creds.magic = CRED_TABLE_MAGIC;
		g_creds.count = 1;
		g_creds.items[0] = it;

		LOG_INF("legacy credential migrated: \"%s\"", it.ssid);

		return 0;
	}

	LOG_WRN("stored credential size mismatch (%u)", (unsigned)len);

	return -EINVAL;
}

SETTINGS_STATIC_HANDLER_DEFINE(portal_cred, "portal", NULL, cred_settings_set, NULL, NULL);

/* --------------------------------------------- 与官方凭据库的同步 / 迁移 */
/* wifi_credentials_for_each_ssid() 是持着库内 mutex 回调的，而
 * wifi_credentials_get_by_ssid_personal_struct() 会再去拿同一把 mutex——
 * zephyr 的 k_mutex 不可重入，回调里直接取密码会把自己锁死。
 * 所以分两段：先把 SSID 抄下来，出锁之后再逐个取密码。 */
struct cred_scan_ctx {
	char ssid[CRED_MAX][WIFI_SSID_MAX_LEN + 1];
	size_t len[CRED_MAX];
	uint8_t count;
};

static void cred_collect_cb(void *arg, const char *ssid, size_t ssid_len)
{
	struct cred_scan_ctx *ctx = arg;

	if (ctx->count >= CRED_MAX || ssid_len > WIFI_SSID_MAX_LEN) {
		return;
	}

	memcpy(ctx->ssid[ctx->count], ssid, ssid_len);
	ctx->ssid[ctx->count][ssid_len] = '\0';
	ctx->len[ctx->count] = ssid_len;
	ctx->count++;
}

/* 用官方存储里的内容重建内存快照（g_creds）。内存表只服务 UI、自动连接和 shell，
 * 真值永远在 wifi_credentials 里。 */
static void cred_store_sync(void)
{
	struct cred_scan_ctx ctx = {0};

	wifi_credentials_for_each_ssid(cred_collect_cb, &ctx);

	g_creds.count = 0;

	for (int i = 0; i < ctx.count && g_creds.count < CRED_MAX; i++) {
		struct wifi_credentials_personal cred;
		struct cred_item *it = &g_creds.items[g_creds.count];

		if (wifi_credentials_get_by_ssid_personal_struct(ctx.ssid[i], ctx.len[i],
									&cred) != 0) {
			continue;
		}

		memset(it, 0, sizeof(*it));
		it->ssid_len = (uint8_t)ctx.len[i];
		it->psk_len = (uint8_t)MIN(cred.password_len, sizeof(it->psk) - 1);
		memcpy(it->ssid, ctx.ssid[i], it->ssid_len);
		memcpy(it->psk, cred.password, it->psk_len);
		g_creds.count++;
	}

	LOG_INF("wifi_credentials: %u saved network(s)", g_creds.count);

	/* 把"上次连成功的那条"排到表头，重启后不用先白试一轮失效网络 */
	if (g_lastok_len > 0 && g_creds.count > 1) {
		for (int i = 1; i < g_creds.count; i++) {
			if (g_creds.items[i].ssid_len == g_lastok_len &&
			    memcmp(g_creds.items[i].ssid, g_lastok_ssid, g_lastok_len) == 0) {
				struct cred_item tmp = g_creds.items[i];

				memmove(&g_creds.items[1], &g_creds.items[0],
					i * sizeof(struct cred_item));
				g_creds.items[0] = tmp;
				LOG_INF("boot target pinned to last good: \"%s\"", g_lastok_ssid);
				break;
			}
		}
	}
}

/* 记下"这个网络真的连上过"。只在变化时写 flash，避免每次连上都擦一遍。 */
void cred_mark_last_good(const char *ssid, size_t len)
{
	if (len == 0 || len > WIFI_SSID_MAX_LEN) {
		return;
	}
	if (g_lastok_len == len && memcmp(g_lastok_ssid, ssid, len) == 0) {
		return;
	}

	memcpy(g_lastok_ssid, ssid, len);
	g_lastok_ssid[len] = '\0';
	g_lastok_len = (uint8_t)len;

	int rc = settings_save_one(LASTOK_SETTINGS_KEY, ssid, len);

	if (rc) {
		LOG_WRN("save last-good network failed (%d)", rc);
	}
}

/* 把第 idx 条已保存网络装进当前连接参数（g_ssid / g_psk）。 */
void cred_migrate_legacy(void)
{
	if (!wifi_credentials_is_empty()) {
		/* 已经迁移过：以官方存储为准，顺手清掉旧键 */
		cred_store_sync();
		settings_delete(CRED_SETTINGS_KEY);
		return;
	}

	if (g_creds.count == 0) {
		return;
	}

	uint8_t n = g_creds.count;

	for (int i = 0; i < n; i++) {
		cred_store_save(&g_creds.items[i]);
	}

	cred_store_sync();
	settings_delete(CRED_SETTINGS_KEY);

	LOG_INF("migrated %u credential(s) from legacy settings key", n);
}

/* ------------------------------------------------------- 扫描结果缓存 */

/* ===================== 对外只读接口 ===================== */
uint8_t cred_count(void)
{
	return g_creds.count;
}

const struct cred_item *cred_at(int idx)
{
	if (idx < 0 || idx >= g_creds.count) {
		return NULL;
	}

	return &g_creds.items[idx];
}
