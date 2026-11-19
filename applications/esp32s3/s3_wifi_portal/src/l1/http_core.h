/*
 * http_core.h —— L1 公共底座：鉴权 / 来源判定 / 转义 / HTTP 服务定义
 *
 * 两个 GUARD 宏依赖**形参名**（client / request_ctx / response_ctx），这是隐性契约：
 * 改名不会报链接错误，只会静默用错对象。所以宏只在这里定义一次，并且注释写死形参名。
 */
#ifndef HTTP_CORE_H_
#define HTTP_CORE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/net/http/server.h>
#include <zephyr/net/http/service.h>
#include <zephyr/net/http/status.h>

extern const struct http_header json_header[2];
extern uint16_t http_port;

bool   client_side_ap(struct http_client_ctx *client);
bool   auth_pass(struct http_client_ctx *client, const struct http_request_ctx *req,
                 struct http_response_ctx *resp);
size_t portal_json_escape(char *dst, size_t dst_len, const char *src);
void   url_decode(char *s);
bool   parse_form_field(const char *body, const char *key, char *dst, size_t dst_len,
                        size_t *dst_len_out);

/* 形参名必须是 client / request_ctx / response_ctx */
#define AUTH_GUARD()                                                        \
	do {                                                                \
		if (!auth_pass(client, request_ctx, response_ctx)) {        \
			return 0;                                           \
		}                                                           \
	} while (0)

/* 开发者专用接口：AP 侧一律当作不存在（404），避免"连上热点就能刷机/敲命令"。 */
#define DEV_GUARD()                                                            \
	do {                                                                   \
		if (client_side_ap(client)) {                                  \
			static const char m[] =                                \
				"{\"ok\":false,\"err\":\"developer console is on the LAN side only\"}"; \
			response_ctx->status = HTTP_404_NOT_FOUND;             \
			response_ctx->headers = json_header;                   \
			response_ctx->header_count = ARRAY_SIZE(json_header);  \
			response_ctx->body = (const uint8_t *)m;               \
			response_ctx->body_len = sizeof(m) - 1;                \
			response_ctx->final_chunk = true;                      \
			return 0;                                              \
		}                                                              \
	} while (0)


/* 资源表（HTTP_RESOURCE_DEFINE 的最后一个参数）集中声明：
 * 表在 http_core.c，detail 对象随各自的 handler 留在 L1 各文件里。 */
extern struct http_resource_detail_dynamic saved_detail;
extern struct http_resource_detail_dynamic forget_detail;
extern struct http_resource_detail_dynamic disconnect_detail;
extern struct http_resource_detail_dynamic apclients_detail;
extern struct http_resource_detail_dynamic rssi_hist_detail;
extern struct http_resource_detail_dynamic drops_detail;
extern struct http_resource_detail_dynamic slots_detail;
extern struct http_resource_detail_dynamic revert_detail;
extern struct http_resource_detail_dynamic wlog_detail;
extern struct http_resource_detail_dynamic boot_detail;
extern struct http_resource_detail_dynamic pw_detail;
extern struct http_resource_detail_dynamic shell_detail;
extern struct http_resource_detail_dynamic index_detail;
extern struct http_resource_detail_dynamic scan_detail;
extern struct http_resource_detail_dynamic scan_start_detail;
extern struct http_resource_detail_dynamic status_detail;
extern struct http_resource_detail_dynamic wifi_detail;
extern struct http_resource_detail_dynamic connect_detail;
extern struct http_resource_detail_dynamic captive_detail;

#endif /* HTTP_CORE_H_ */
