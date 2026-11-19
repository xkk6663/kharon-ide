/*
 * api_wifi —— 用户侧（AP 网段）HTTP 接口：扫描 / 连接 / 已保存 / 断开 / 客户端
 *
 * L1 接口层。只读 L2 的快照（link_view / scan_* / ap_client_* / cred_*），
 * 不持有任何状态；每个 handler 自带自己的 JSON 缓冲与它自己的 HTTP 资源注册。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/http/server.h>
#include <zephyr/net/http/service.h>
#include <zephyr/net/http/status.h>

#include "portal_cfg.h"
#include "http_core.h"
#include "cred.h"
#include "wifi_link.h"
#include "wifi_scan.h"
#include "portal_ap.h"
#include "time_sync.h"
#include "netcheck.h"
#include "ops.h"
#include "ota.h"

LOG_MODULE_REGISTER(portal_api_wifi, LOG_LEVEL_INF);

static char g_scan_json[1600];
/* 取当前 STA 的 RSSI；没连上或查询失败统一返回 0（网页据此显示 "-"）。
 * 放在这里是为了 status_handler 能直接用，而不用多传参数。 */
static char g_status_json[1536];
static char g_conn_json[64];

/* GET /api/scan：只读缓存，绝不发起扫描。
 * 页面轮询必须走这里 —— 如果每次轮询都调 wifi_scan_start()，里面的
 * scan_cache_reset() 会把刚扫到的结果清掉，表现就是"永远空列表"。 */
/* ---------------------------------------------------------- 配网页鉴权 */
static int scan_handler(struct http_client_ctx *client, enum http_transaction_status status,
			const struct http_request_ctx *request_ctx,
			struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	bool scanning = scan_is_active();

	size_t n = 0;

	n += snprintf(g_scan_json + n, sizeof(g_scan_json) - n, "{\"scanning\":%s,\"aps\":[",
		      scanning ? "true" : "false");

	/* 锁是扫描缓存自己的接口：拿锁期间数组不会在"扫到一半"时被改 */
	scan_lock();
	for (int i = 0; i < scan_count() && n < sizeof(g_scan_json) - 120; i++) {
		char esc[WIFI_SSID_MAX_LEN + 8];
		const struct ap_entry *e = &scan_entries()[i];

		portal_json_escape(esc, sizeof(esc), e->ssid);
		n += snprintf(g_scan_json + n, sizeof(g_scan_json) - n,
			      "%s{\"ssid\":\"%s\",\"rssi\":%d,\"ch\":%u,\"sec\":%u}",
			      i ? "," : "", esc, e->rssi, e->channel, e->security);
	}
	scan_unlock();

	snprintf(g_scan_json + n, sizeof(g_scan_json) - n, "]}");

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_scan_json;
	response_ctx->body_len = strlen(g_scan_json);
	response_ctx->final_chunk = true;

	return 0;
}

static char g_scanstate_json[32];

/* GET /api/scan/start：只有用户点"扫描"才走到这里，真正发起一次扫描。
 * 发起时 wifi_scan_start() 会重置缓存，这正是新扫描想要的；
 * 之后前端改用 /api/scan 纯读，结果就不会被下一次请求清掉。 */
static int scan_start_handler(struct http_client_ctx *client, enum http_transaction_status status,
			      const struct http_request_ctx *request_ctx,
			      struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	wifi_scan_start();

	snprintf(g_scanstate_json, sizeof(g_scanstate_json), "{\"scanning\":%s}",
		 (scan_is_active()) ? "true" : "false");

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_scanstate_json;
	response_ctx->body_len = strlen(g_scanstate_json);
	response_ctx->final_chunk = true;

	return 0;
}

static int status_handler(struct http_client_ctx *client, enum http_transaction_status status,
			  const struct http_request_ctx *request_ctx,
			  struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	DEV_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	const char *state_str = link_state() == LINK_CONNECTED ? "connected" :
				link_state() == LINK_CONNECTING ? "connecting" : "idle";
	char esc_ssid[WIFI_SSID_MAX_LEN + 8];
	char esc_msg[sizeof(link_msg()) + 8];
	char esc_err[sizeof(link_err()) + 8];

	/* 显示"真的连着的"那条：link_target_ssid() 是本次点击的目标，可能已经被改。
	 * 万一 link_link_ssid() 还没填（例如极端时序），退回 link_target_ssid() 不致于全空。 */
	const char *shown_ssid = (link_state() == LINK_CONNECTED) ?
		(link_link_ssid()[0] ? link_link_ssid() : link_target_ssid()) : "";

	portal_json_escape(esc_ssid, sizeof(esc_ssid), shown_ssid);
	portal_json_escape(esc_msg, sizeof(esc_msg), link_msg());
	portal_json_escape(esc_err, sizeof(esc_err), link_err());

	char clock[12];

	clock_hms(clock, sizeof(clock));

	char boot_json[96];

	ops_boot_json(boot_json, sizeof(boot_json));

	/* 尾部带四样新东西：
	 *   clock    —— SNTP 对时之后的本地时间（没对到就是空串）
	 *   netcheck —— 主动出网自检收到的 HTTP 状态码（0 = 还没结果）
	 *   wlog     —— 已记录的 WiFi 事件条数（页面据此提示"有新的断连"）
	 *   boot     —— 启动/崩溃计数，见 ops.c
	 *   ota      —— 由 ota.c 拼好的整段，含阶段/百分比/版本/是否待确认 */
	snprintf(g_status_json, sizeof(g_status_json),
		 "{\"state\":\"%s\",\"ssid\":\"%s\",\"ip\":\"%s\",\"msg\":\"%s\","
		 "\"rssi\":%d,\"reconnects\":%u,\"uptime\":%u,\"last_err\":\"%s\","
		 "\"clock\":\"%s\",\"netcheck\":%d,\"netrc\":%d,\"netdone\":%d,"
		 "\"dns\":%d,\"dnsip\":\"%s\",\"gw\":\"%s\",\"netip\":\"%s\","
		 "\"sntp\":%d,\"sntpsrv\":\"%s\",\"wlog\":%u,"
		 "\"channel\":{\"ap\":%u,\"sta\":%d,\"pinned\":%u,\"align\":%d},"
		 "\"boot\":%s,%s}",
		 state_str, esc_ssid, link_ip(), esc_msg,
		 wifi_rssi(), (unsigned)link_reconnects(),
		 (unsigned)(k_uptime_get_32() / 1000U), esc_err,
		 clock, net_http_status(), net_rc(), net_done() ? 1 : 0,
		 net_dns_rc(), net_dns_ip(), link_gw(), net_ip(),
		 time_sntp_rc(), time_sntp_srv(), (unsigned)ops_ev_count(),
		 (unsigned)ap_current_channel(), link_sta_channel(),
		 (unsigned)ap_pinned_channel(), link_align_step(),
		 boot_json, ota_snapshot_json());

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_status_json;
	response_ctx->body_len = strlen(g_status_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* GET /api/wifi —— 给 AP 侧用户页的最小状态。
 *
 * 为什么不复用 /api/status：那一个是开发者台的口径，里面带 boot 计数、复位原因、
 * netcheck、OTA 槽位/版本。这些既是"面向用户就不该出现"的信息，也完全没人用；
 * 用户页只想知道：现在连没连上、连的哪个、IP 多少、上一次失败的原因。 */
static char g_wifi_json[192];

static int wifi_handler(struct http_client_ctx *client, enum http_transaction_status status,
			const struct http_request_ctx *request_ctx,
			struct http_response_ctx *response_ctx, void *user_data)
{
	AUTH_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	const char *state_str = link_state() == LINK_CONNECTED ? "connected" :
				link_state() == LINK_CONNECTING ? "connecting" : "idle";
	char esc_ssid[WIFI_SSID_MAX_LEN + 8];
	char esc_msg[sizeof(link_msg()) + 8];
	char esc_err[sizeof(link_err()) + 8];

	const char *shown_ssid = (link_state() == LINK_CONNECTED) ?
		(link_link_ssid()[0] ? link_link_ssid() : link_target_ssid()) : "";

	portal_json_escape(esc_ssid, sizeof(esc_ssid), shown_ssid);
	portal_json_escape(esc_msg, sizeof(esc_msg), link_msg());
	portal_json_escape(esc_err, sizeof(esc_err), link_err());

	snprintf(g_wifi_json, sizeof(g_wifi_json),
		 "{\"state\":\"%s\",\"ssid\":\"%s\",\"ip\":\"%s\",\"msg\":\"%s\","
		 "\"last_err\":\"%s\",\"rssi\":%d}",
		 state_str, esc_ssid, link_ip(), esc_msg, esc_err, wifi_rssi());

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_wifi_json;
	response_ctx->body_len = strlen(g_wifi_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* POST /api/connect，body 是 application/x-www-form-urlencoded：
 *   ssid=xxx&psk=yyy
 * 请求体可能分几次回调送过来，所以先攒到 body_buf，最后一次再统一处理。
 */
static uint8_t body_buf[320];
static size_t body_len;

void url_decode(char *s)
{
	char *out = s;

	for (char *in = s; *in; in++) {
		if (*in == '+') {
			*out++ = ' ';
		} else if (*in == '%' && in[1] && in[2]) {
			int hi = in[1] <= '9' ? in[1] - '0' : (in[1] | 0x20) - 'a' + 10;
			int lo = in[2] <= '9' ? in[2] - '0' : (in[2] | 0x20) - 'a' + 10;

			*out++ = (char)((hi << 4) | lo);
			in += 2;
		} else {
			*out++ = *in;
		}
	}
	*out = '\0';
}

bool parse_form_field(const char *body, const char *key, char *dst, size_t dst_len,
			     size_t *dst_len_out)
{
	size_t klen = strlen(key);
	const char *p = body;

	/* URL 的 query 以 '?' 开头（http_server 的 GET 分支会把 "?ssid=..." 整段
	 * 当 data 交过来），请求体则直接是 "ssid=..."。这里两种都吃。 */
	if (p && *p == '?') {
		p++;
	}

	while (p && *p) {
		if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
			const char *val = p + klen + 1;
			const char *end = strchr(val, '&');
			size_t len = end ? (size_t)(end - val) : strlen(val);

			if (len >= dst_len) {
				return false;
			}
			memcpy(dst, val, len);
			dst[len] = '\0';
			url_decode(dst);
			if (dst_len_out) {
				*dst_len_out = strlen(dst);
			}
			return true;
		}
		p = strchr(p, '&');
		if (p) {
			p++;
		}
	}

	return false;
}

static int connect_handler(struct http_client_ctx *client, enum http_transaction_status status,
			   const struct http_request_ctx *request_ctx,
			   struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(user_data);

	AUTH_GUARD();

	if (status == HTTP_SERVER_TRANSACTION_ABORTED) {
		body_len = 0;
		return 0;
	}

	/* 只有 POST 才需要把分片 body 攒起来。GET 的 query（"?ssid=..."）会一次性
	 * 出现在 request_ctx->data 里，直接解析即可。
	 *
	 * 这里有个真实的坑：body_buf 是所有 client 共享的静态缓冲，HTTP_SERVER_MAX_CLIENTS
	 * = 4 时多个连接并发回调会互相串味。实测连续两次 forget 解析出
	 *   "AlphaTest?ssid=BetaTest2"
	 * ——上一个请求的数据被拼到了当前请求上，于是 cred_remove 找不到记录，
	 * 用户在页面上看到的就是"点忘记没反应" / "连谁都连不上"。 */
	if (client->method != HTTP_GET && request_ctx && request_ctx->data_len > 0) {
		size_t room = sizeof(body_buf) - body_len - 1;
		size_t add = MIN(request_ctx->data_len, room);

		memcpy(body_buf + body_len, request_ctx->data, add);
		body_len += add;
		body_buf[body_len] = '\0';
	}

	if (status == HTTP_SERVER_REQUEST_DATA_MORE) {
		/* 还没收完，先别回复 */
		response_ctx->final_chunk = false;
		return 0;
	}

	/* 只有 FINAL 这一轮才真正执行动作。POST 的 body 可能分片回调多次，
	 * 之前没判 status，同一个连接请求会被执行两遍（日志里两条 connect request）。 */
	if (status != HTTP_SERVER_REQUEST_DATA_FINAL) {
		return 0;
	}

	char ssid[WIFI_SSID_MAX_LEN + 1] = {0};
	char psk[65] = {0};
	size_t ssid_len = 0, psk_len = 0;
	const char *url = (const char *)client->url_buffer;
	const char *src = NULL;

	if (client->method == HTTP_GET) {
		/* GET 只认当前请求的 data，绝不碰共享缓冲 */
		src = (request_ctx && request_ctx->data_len > 0) ?
		      (const char *)request_ctx->data : NULL;
	} else {
		src = (const char *)body_buf;
	}

	bool ok = src ? parse_form_field(src, "ssid", ssid, sizeof(ssid), &ssid_len) : false;

	if (ok) {
		parse_form_field(src, "psk", psk, sizeof(psk), &psk_len);

		/* 只给了 SSID（网页里点"已保存"的网络）→ 从凭据表补齐密码，
		 * 用户不用把密码再敲一遍。开放网络在表里 psk_len 也是 0，行为一致。 */
		if (psk_len == 0) {
			int idx = cred_index(ssid, ssid_len);
			const struct cred_item *it = cred_at(idx);

			if (it != NULL && it->psk_len > 0) {
				psk_len = it->psk_len;
				memcpy(psk, it->psk, psk_len);
				psk[psk_len] = '\0';
			}
		}
	}

	LOG_INF("connect rx: method=%d status=%d body=%u content_len=%zu url=\"%s\" ssid=\"%s\"",
		(int)client->method, (int)status, (unsigned)body_len, client->content_len,
		url, ok ? ssid : "");

	body_len = 0;

	LOG_INF("connect request: \"%s\" psk_len=%u (from table: %s)", ssid,
		(unsigned)psk_len, cred_index(ssid, ssid_len) >= 0 ? "yes" : "no");

	if (!ok || ssid_len == 0) {
		snprintf(g_conn_json, sizeof(g_conn_json), "{\"ok\":false,\"err\":\"missing ssid\"}");
	} else if (link_is_connected() && link_target_is(ssid, ssid_len)) {
		/* 已经连着这个网络。再发一次 CONNECT 驱动会回 -EALREADY(120)，
		 * 而我们原来的失败分支会把状态打成 idle、把灯打红 —— 状态被误报。
		 * 用户的本意就是"连上它"，此刻已经是目标状态，直接认成功。 */
		LOG_INF("connect request: already on \"%s\", nothing to do", ssid);
		snprintf(g_conn_json, sizeof(g_conn_json),
			 "{\"ok\":true,\"msg\":\"already connected\"}");
	} else {
		/* 一行调用代替原来这 12 行对 6 个全局的写：
		 * 目标 SSID/PSK、清 last_err、停自动重连、对齐轮换下标、提交 work ——
		 * 全是 wifi_link 的内部约定，L1 不该知道它们。 */
		link_request_connect(ssid, ssid_len, psk, psk_len);
		snprintf(g_conn_json, sizeof(g_conn_json), "{\"ok\":true}");
	}

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_conn_json;
	response_ctx->body_len = strlen(g_conn_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* -------------------------------------- /api/saved、/api/forget、/api/disconnect
 * 对应网页上"已保存网络"那张卡片。
 * 只回 SSID 和"有没有存密码"，密码本身不出网关——页面没必要拿到明文。
 */
static char g_saved_json[512];
static char g_forget_json[128];
static char g_disc_json[48];

static int saved_handler(struct http_client_ctx *client, enum http_transaction_status status,
			 const struct http_request_ctx *request_ctx,
			 struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	size_t n = 0;

	n += snprintf(g_saved_json + n, sizeof(g_saved_json) - n, "{\"saved\":[");

	for (int i = 0; i < cred_count() && n < sizeof(g_saved_json) - 90; i++) {
		char esc[WIFI_SSID_MAX_LEN + 8];
		const struct cred_item *it = cred_at(i);
		/* "当前"标记跟着实际链路走，不跟目标 SSID 走：
		 * 否则点一个连不上的网络时，那条会被错误地标成"当前"。 */
		bool current = (link_state() == LINK_CONNECTED) &&
			       strlen(link_link_ssid()) == it->ssid_len &&
			       memcmp(link_link_ssid(), it->ssid, it->ssid_len) == 0;

		portal_json_escape(esc, sizeof(esc), it->ssid);
		n += snprintf(g_saved_json + n, sizeof(g_saved_json) - n,
			      "%s{\"ssid\":\"%s\",\"sec\":%s,\"current\":%s}",
			      i ? "," : "", esc,
			      it->psk_len > 0 ? "true" : "false",
			      current ? "true" : "false");
	}

	snprintf(g_saved_json + n, sizeof(g_saved_json) - n, "]}");

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_saved_json;
	response_ctx->body_len = strlen(g_saved_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* POST /api/forget  body: ssid=xxx
 * 单独用一份 buffer，避免和 /api/connect 的 body_buf 在并发请求时互相覆盖 */
static uint8_t forget_buf[128];
static size_t forget_len;

static int forget_handler(struct http_client_ctx *client, enum http_transaction_status status,
			  const struct http_request_ctx *request_ctx,
			  struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(user_data);

	AUTH_GUARD();

	if (status == HTTP_SERVER_TRANSACTION_ABORTED) {
		forget_len = 0;
		return 0;
	}

	/* 与 connect_handler 同理：forget_buf 是所有 client 共享的静态缓冲，
	 * 并发/连续请求会串味成 "AlphaTest?ssid=BetaTest2"，cred_remove 自然找不到 →
	 * 前端显示"没有找到…"，用户看到的就是"点忘记没反应"。GET 只认当前 data。 */
	if (client->method != HTTP_GET && request_ctx && request_ctx->data_len > 0) {
		size_t room = sizeof(forget_buf) - forget_len - 1;
		size_t add = MIN(request_ctx->data_len, room);

		memcpy(forget_buf + forget_len, request_ctx->data, add);
		forget_len += add;
		forget_buf[forget_len] = '\0';
	}

	if (status == HTTP_SERVER_REQUEST_DATA_MORE) {
		response_ctx->final_chunk = false;
		return 0;
	}

	/* 只在 FINAL 执行：第二次回调时凭据已经删掉了，会误报 ok:false */
	if (status != HTTP_SERVER_REQUEST_DATA_FINAL) {
		return 0;
	}

	char ssid[WIFI_SSID_MAX_LEN + 1] = {0};
	size_t ssid_len = 0;
	const char *src = NULL;

	if (client->method == HTTP_GET) {
		src = (request_ctx && request_ctx->data_len > 0) ?
		      (const char *)request_ctx->data : NULL;
	} else {
		src = (const char *)forget_buf;
	}

	bool ok = src ? parse_form_field(src, "ssid", ssid, sizeof(ssid), &ssid_len) : false;

	bool removed = ok && cred_remove(ssid, ssid_len);

	LOG_INF("forget rx: body=%u url=\"%s\" ssid=\"%s\" removed=%d", (unsigned)forget_len,
		(const char *)client->url_buffer, ok ? ssid : "", (int)removed);

	forget_len = 0;

	/* 回显解析到的 ssid：排查"参数串味"时不用抓串口，看响应就知道
	 * 服务端到底认成了哪个网络（曾经出现过 "501A?ssid=6803" 这种）。 */
	snprintf(g_forget_json, sizeof(g_forget_json), "{\"ok\":%s,\"ssid\":\"%.*s\"}",
		 removed ? "true" : "false", (int)ssid_len, ssid);

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_forget_json;
	response_ctx->body_len = strlen(g_forget_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* POST /api/disconnect：主动断开 STA，且不会被运行期重连自动连回去 */
static int disconnect_handler(struct http_client_ctx *client, enum http_transaction_status status,
			      const struct http_request_ctx *request_ctx,
			      struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	if (status == HTTP_SERVER_TRANSACTION_ABORTED) {
		return 0;
	}

	/* 客户端通常先发 header 再发（可能为空的）body：等请求体收完再动手 */
	if (status == HTTP_SERVER_REQUEST_DATA_MORE) {
		response_ctx->final_chunk = false;
		return 0;
	}

	if (status != HTTP_SERVER_REQUEST_DATA_FINAL) {
		return 0;
	}

	int ret = portal_disconnect();

	snprintf(g_disc_json, sizeof(g_disc_json), "{\"ok\":%s,\"err\":%d}",
		 ret == 0 ? "true" : "false", ret);

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_disc_json;
	response_ctx->body_len = strlen(g_disc_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* ------------------------------------------ /api/apclients、/api/rssi_hist
 * 这两张卡片都是"看得见的信号"：谁连着本机 AP、信号是不是在变差。 */

/* 这一版 Zephyr 没有查询 AP 客户端的 net_mgmt 请求，数据靠
 * NET_EVENT_WIFI_AP_STA_CONNECTED / _DISCONNECTED 自己维护。
 * struct wifi_ap_sta_info 只带 MAC（没有 RSSI、没有 IP），所以只能给
 * MAC + 在线时长——不假装有拿不到的数据。 */
static char g_apclients_json[512];

static int apclients_handler(struct http_client_ctx *client, enum http_transaction_status status,
			     const struct http_request_ctx *request_ctx,
			     struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	DEV_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	size_t n = 0;
	int count = ap_client_count();

	n += snprintf(g_apclients_json + n, sizeof(g_apclients_json) - n,
		      "{\"count\":%d,\"max\":%d,\"clients\":[", count, AP_CLIENT_MAX);

	int sep = 0;

	for (int i = 0; i < AP_CLIENT_MAX && n < sizeof(g_apclients_json) - 90; i++) {
		const struct ap_client *c = ap_client_at(i);

		if (c == NULL) {
			continue;
		}

		uint32_t now = k_uptime_get_32();
		uint32_t sec = (now > c->joined_at) ? (now - c->joined_at) / 1000U : 0;

		n += snprintf(g_apclients_json + n, sizeof(g_apclients_json) - n,
			      "%s{\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\",\"sec\":%u}",
			      sep ? "," : "",
			      c->mac[0], c->mac[1], c->mac[2], c->mac[3], c->mac[4],
			      c->mac[5], (unsigned)sec);
		sep = 1;
	}

	snprintf(g_apclients_json + n, sizeof(g_apclients_json) - n, "]}");

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_apclients_json;
	response_ctx->body_len = strlen(g_apclients_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* 按"最老 → 最新"输出。环形缓冲装满后从 head 开始绕一圈，
 * 前端拿到的就是一条可以直接画折线的时间序列。 */
static char g_rssi_json[512];

static int rssi_hist_handler(struct http_client_ctx *client, enum http_transaction_status status,
			     const struct http_request_ctx *request_ctx,
			     struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	AUTH_GUARD();
	DEV_GUARD();
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	/* 环形缓冲的起点/长度/填充规则是 wifi_link 的内部约定：
	 * 这里只要一段能直接画折线的 JSON，不要自己绕一圈。 */
	link_rssi_hist_json(g_rssi_json, sizeof(g_rssi_json));

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_rssi_json;
	response_ctx->body_len = strlen(g_rssi_json);
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic saved_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_HEAD),
	},
	.cb = saved_handler,
};

struct http_resource_detail_dynamic forget_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_POST) | BIT(HTTP_GET),
	},
	.cb = forget_handler,
};

struct http_resource_detail_dynamic disconnect_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_POST) | BIT(HTTP_GET),
	},
	.cb = disconnect_handler,
};

struct http_resource_detail_dynamic apclients_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_HEAD),
	},
	.cb = apclients_handler,
};

struct http_resource_detail_dynamic rssi_hist_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_HEAD),
	},
	.cb = rssi_hist_handler,
};

/* 这五个 detail 必须和 handler 同文件：cb 指向的是本文件的 static 函数 */
struct http_resource_detail_dynamic scan_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = scan_handler,
};

struct http_resource_detail_dynamic scan_start_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = scan_start_handler,
};

struct http_resource_detail_dynamic status_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = status_handler,
};

/* AP 侧用户页的状态接口，两个侧都允许访问（局域网侧要 k）。 */
struct http_resource_detail_dynamic wifi_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = wifi_handler,
};

struct http_resource_detail_dynamic connect_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		/* GET 也必须支持：页面把 ssid/psk 放在 URL query 里（GET 的 query
		 * 会被 http_server 直接当成 request data 交给 handler），
		 * 这样就不依赖 POST body 能不能完整传到板子。 */
		.bitmask_of_supported_http_methods = BIT(HTTP_POST) | BIT(HTTP_GET),
	},
	.cb = connect_handler,
};

