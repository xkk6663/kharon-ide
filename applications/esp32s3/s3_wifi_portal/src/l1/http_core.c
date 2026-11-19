/*
 * http_core —— L1 接口层的公共底座：鉴权、来源判定、JSON 转义、HTTP 服务定义
 *
 * 所有 api_* / page 都 include 这个头；反过来它不认识任何业务模块，
 * 只认识 wifi_link（判断"是不是连着"）。
 */
#include <string.h>
#include <stdlib.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/http/server.h>
#include <zephyr/net/http/service.h>
#include <zephyr/net/http/status.h>

#include "portal_cfg.h"
#include "http_core.h"
#include "wifi_link.h"
#include "ops.h"

LOG_MODULE_REGISTER(portal_http, LOG_LEVEL_INF);

const struct http_header json_header[] = {
	{.name = "Content-Type", .value = "application/json"},
	{.name = "Cache-Control", .value = "no-store"},
};

/* 把 SSID 里的 " 和 \ 转义掉，否则 JSON 会被撑坏。
 * 名字带 portal_ 前缀：CONFIG_JSON_LIBRARY 打开后 zephyr/lib/utils/json.c
 * 也导出 json_escape()，全局同名会在链接期 multiple definition。 */
size_t portal_json_escape(char *dst, size_t dst_len, const char *src)
{
	size_t n = 0;

	for (; *src && n + 2 < dst_len; src++) {
		if (*src == '"' || *src == '\\') {
			dst[n++] = '\\';
		}
		if ((unsigned char)*src < 0x20) {
			continue;
		}
		dst[n++] = *src;
	}
	dst[n] = '\0';

	return n;
}

/* HTTP server 允许 4 个客户端并发，每个 handler 各用一块缓冲，
 * 避免 /api/scan（大 JSON）和 /api/status（每秒被手机轮询）互相踩。
 */
/* ------------------------------------------------------- 请求来源：AP 侧 / 局域网侧
 *
 * 产品上这是两个受众完全不同的界面：
 *   AP 侧（手机直连 S3-Portal）  = 面向用户的配网页，只做"连哪个 WiFi"，不要口令、不要命令；
 *   局域网侧（板子已连上路由器） = 面向开发者的调试台，OTA / 远程 shell / 槽位 / 复位原因都在这里。
 *
 * 这两者共用一个 :80，所以必须在请求层面区分来源，否则连上热点的任何人都能看到 OTA 和 shell
 * （实测就是这样：手机连 AP 后整块"固件升级/远程命令"都在）。
 * 判据用已 accept 的 socket 对端地址：AP 侧客户端的地址一定落在 192.168.4.0/24。
 * 拿不到对端地址时**按 AP 侧处理**（从严）：宁可让开发者少一个接口，也不把 shell 暴露给未知来源。 */
bool client_side_ap(struct http_client_ctx *client)
{
	struct sockaddr_in peer;
	socklen_t plen = sizeof(peer);
	static struct in_addr ap_addr;

	if (client == NULL || client->fd < 0) {
		return true;
	}

	if (zsock_getpeername(client->fd, (struct sockaddr *)&peer, &plen) < 0 ||
	    peer.sin_family != AF_INET) {
		return true;
	}

	if (ap_addr.s_addr == 0) {
		net_addr_pton(AF_INET, AP_IP, &ap_addr);
	}

	uint32_t peer_host = ntohl(peer.sin_addr.s_addr);
	uint32_t ap_host = ntohl(ap_addr.s_addr);

	return (peer_host & 0xFFFFFF00u) == (ap_host & 0xFFFFFF00u);
}

/* 口令校验走查询/请求体里的 k= 参数，不用 HTTP Basic。
 *
 * 为什么不用 Basic：这版 Zephyr（4.5.99）的请求头捕获有缺陷 ——
 * check_user_request_headers()/populate_user_request_header() 成功后**从不**把
 * header_capture_ctx.status 置成 HTTP_HEADER_STATUS_OK（全树只有 NONE 和 DROPPED），
 * 而 populate_request_ctx() 只要 status==NONE 就把 headers 置 NULL。
 * 结果是 request_ctx->headers 恒为 NULL，任何基于头的鉴权都恒放行。
 * 实测坐实过：不带 Authorization 也能拿到 200。
 *
 * 改成 k= 之后鉴权完全由应用层决定，代价是口令会出现在 URL 和浏览器历史里，
 * 属于"局域网配网门户"可接受的一档；真要更强的，得给 Zephyr 打补丁或上 TLS。
 *
 * 口令只管**局域网侧**的开发者接口。AP 侧是用户配网页：进 AP 本身要 WPA2 密码，
 * 再叠一层 k 只会让"连上就能配网"变麻烦，与产品要求相反。 */
bool auth_pass(struct http_client_ctx *client, const struct http_request_ctx *req,
		      struct http_response_ctx *resp)
{
	if (client_side_ap(client)) {
		return true;
	}

	if (req != NULL && req->data != NULL && req->data_len > 0) {
		/* 191 而不是 127：/api/connect 的查询串是 ssid + psk + k 三个字段，
		 * 长 SSID（32 字节）+ 长密码（64 字节）再加 k，128 会正好把 k 截掉。
		 * 截掉了也不许"猜测"——找不到 k 就当没给，401 是明确可查的。 */
		char buf[192];
		size_t n = MIN(req->data_len, sizeof(buf) - 1);

		memcpy(buf, req->data, n);
		buf[n] = '\0';
		url_decode(buf);

		/* 只认"参数边界上的 k="：前一个字符必须是串首、'?' 或 '&'。
		 * 裸搜 "k=" 会命中 "psk=..." 尾巴上的那个，于是口令被判成密码不对
		 * → 所有带 psk 的 /api/connect 全 401（真浏览器点一次才暴露）。
		 * 另外 GET 的 req->data 带前导 '?'，所以"串首"要和 '?' 一起认。 */
		const char *p = NULL;
		const char *q = buf;

		while ((q = strstr(q, "k=")) != NULL) {
			if (q == buf || q[-1] == '?' || q[-1] == '&') {
				p = q;
				break;
			}
			q += 2;
		}

		if (p != NULL) {
			size_t len = strcspn(p + 2, "&");
			const char *pw = ops_auth_pw();

			if (len > 0 && len == strlen(pw) && strncmp(p + 2, pw, len) == 0) {
				return true;
			}
		}
	}

	/* ★ 必须回 JSON。原来这里是纯文本 "need key: ..."，而页面对所有 /api
	 * 都直接 r.json()：401 时 JSON.parse("need key…") 会先按 null 试解析，
	 * 在第 1 个字符 'e' 处炸掉，用户看到的是
	 *   SyntaxError: Unexpected token e in JSON at position 1
	 * 完全掩盖了"缺口令"这个真实原因（实测踩过，页面报的就是这句）。 */
	static const char msg[] = "{\"ok\":false,\"err\":\"need key\","
				  "\"help\":\"\\u5728\\u7f51\\u5740\\u540e\\u52a0 ?k=<\\u53e3\\u4ee4>\"}";

	resp->status = HTTP_401_UNAUTHORIZED;
	resp->headers = json_header;
	resp->header_count = ARRAY_SIZE(json_header);
	resp->body = (const uint8_t *)msg;
	resp->body_len = sizeof(msg) - 1;
	resp->final_chunk = true;

	return false;
}


uint16_t http_port = 80;

HTTP_SERVICE_DEFINE(portal_service, NULL, &http_port, 4, 10, NULL, NULL, NULL);

HTTP_RESOURCE_DEFINE(portal_index, portal_service, "/", &index_detail);
/* /index.html 也指向同一个页面：手输地址、旧书签、以及部分浏览器/系统探测
 * 会带这个路径。不注册的话它会命中下面的通配资源并被 302 出去，
 * 只要 Location 也指回 /index.html 就成了无限跳转环。 */
HTTP_RESOURCE_DEFINE(portal_index_html, portal_service, "/index.html", &index_detail);
HTTP_RESOURCE_DEFINE(portal_scan, portal_service, "/api/scan", &scan_detail);
HTTP_RESOURCE_DEFINE(portal_scan_start, portal_service, "/api/scan/start", &scan_start_detail);
HTTP_RESOURCE_DEFINE(portal_status, portal_service, "/api/status", &status_detail);
HTTP_RESOURCE_DEFINE(portal_wifi, portal_service, "/api/wifi", &wifi_detail);
HTTP_RESOURCE_DEFINE(portal_connect, portal_service, "/api/connect", &connect_detail);
HTTP_RESOURCE_DEFINE(portal_saved, portal_service, "/api/saved", &saved_detail);
HTTP_RESOURCE_DEFINE(portal_forget, portal_service, "/api/forget", &forget_detail);
HTTP_RESOURCE_DEFINE(portal_api_disconnect, portal_service, "/api/disconnect", &disconnect_detail);
HTTP_RESOURCE_DEFINE(portal_apclients, portal_service, "/api/apclients", &apclients_detail);
HTTP_RESOURCE_DEFINE(portal_rssi_hist, portal_service, "/api/rssi_hist", &rssi_hist_detail);
HTTP_RESOURCE_DEFINE(portal_wlog, portal_service, "/api/wlog", &wlog_detail);
HTTP_RESOURCE_DEFINE(portal_drops, portal_service, "/api/drops", &drops_detail);
HTTP_RESOURCE_DEFINE(portal_boot, portal_service, "/api/boot", &boot_detail);
HTTP_RESOURCE_DEFINE(portal_ota_slots, portal_service, "/api/ota/slots", &slots_detail);
HTTP_RESOURCE_DEFINE(portal_ota_revert, portal_service, "/api/ota/revert", &revert_detail);
HTTP_RESOURCE_DEFINE(portal_pw, portal_service, "/api/pw", &pw_detail);
#if defined(CONFIG_SHELL_BACKEND_DUMMY)
HTTP_RESOURCE_DEFINE(portal_shell, portal_service, "/api/shell", &shell_detail);
#endif
/* 通配资源排在精确匹配之后（exact 先命中），所以不会抢 /api 的接口；
 * 所有别的路径（热点探测、任意域名）都 302 到配网页，逼手机弹 Captive Portal */
HTTP_RESOURCE_DEFINE(portal_captive, portal_service, "/*", &captive_detail);

/* -------------------------------------------------------------- 呼吸灯 */
