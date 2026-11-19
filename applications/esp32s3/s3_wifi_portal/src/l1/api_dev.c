/*
 * api_dev —— 局域网侧（开发者）HTTP 接口：事件账本 / 崩溃 / OTA 槽位 / 远程 shell
 *
 * L1 接口层。和 api_wifi 是两套受众、两套鉴权（这一侧要口令），所以是两个文件。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/http/server.h>
#include <zephyr/net/http/service.h>
#include <zephyr/net/http/status.h>
#include <zephyr/shell/shell.h>
#if defined(CONFIG_SHELL_BACKEND_DUMMY)
#include <zephyr/shell/shell_dummy.h>
#endif
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/storage/flash_map.h>

#include "portal_cfg.h"
#include "http_core.h"
#include "cred.h"
#include "wifi_link.h"
#include "ops.h"
#include "ota.h"

LOG_MODULE_REGISTER(portal_api_dev, LOG_LEVEL_INF);

static char g_wlog_json[2176];

static int wlog_handler(struct http_client_ctx *client, enum http_transaction_status status,
			const struct http_request_ctx *request_ctx,
			struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	ops_ev_json(g_wlog_json, sizeof(g_wlog_json));

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_wlog_json;
	response_ctx->body_len = strlen(g_wlog_json);
	response_ctx->final_chunk = true;

	return 0;
}

/* --------------------------- GET /api/drops：断连汇总（重启后仍可查） ---- */
static char g_drops_json[520];

static int drops_handler(struct http_client_ctx *client, enum http_transaction_status status,
			 const struct http_request_ctx *request_ctx,
			 struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	/* 事件存在 NVS 里的 wlog/blk，掉线当下就写入，所以这条查询
	 * "下次上电联网后再问"也是对的，不依赖串口在不在场。 */
	ops_drop_json(g_drops_json, sizeof(g_drops_json));

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_drops_json;
	response_ctx->body_len = strlen(g_drops_json);
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic drops_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_HEAD),
	},
	.cb = drops_handler,
};

/* ------------------------------- GET /api/boot：启动计数 + 线程栈水位 ----- */
static char g_bootjson_buf[640];

static int boot_handler(struct http_client_ctx *client, enum http_transaction_status status,
			const struct http_request_ctx *request_ctx,
			struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	char bo[128];
	char stk[448];

	ops_boot_json(bo, sizeof(bo));
	/* 只报最紧的 6 个线程：卡死时想知道的永远是"谁快没了"，
	 * 把 20 多个线程全铺开反而看不出重点。 */
	ops_stack_json(stk, sizeof(stk), 6);

	snprintf(g_bootjson_buf, sizeof(g_bootjson_buf), "{\"boot\":%s,\"stack\":%s}", bo, stk);

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_bootjson_buf;
	response_ctx->body_len = strlen(g_bootjson_buf);
	response_ctx->final_chunk = true;

	return 0;
}

/* ------------------- GET /api/ota/slots：两槽里躺着哪个版本（只读） -------
 * 回滚方案评估的第一步：确认 swap 之后旧镜像是不是真的还留在 slot1。
 * 只读、不写任何状态；读失败就如实回 rc，不做任何猜测。 */
static char g_slots_json[720];

static void slot_one_json(char *out, size_t len, const char *key, uint8_t area_id)
{
	struct mcuboot_img_header h;
	int rc = boot_read_bank_header(area_id, &h, sizeof(h));
	/* 再按 raw 解一次镜像头（mcuboot image_header v1 的真实布局）：
	 *   +0  magic(u32) / +4 load_addr(u32) / +8 hdr_size(u16) + pad(u16)
	 *   +12 img_size(u32) / +16 flags(u32)
	 *   +20 ver(major u8, minor u8, revision u16) / +24 build_num(u32)
	 * 这样能区分"槽里没镜像"和"有镜像但不在 helper 假设的偏移上"。 */
	char p0[72] = "-";
	char p4k[72] = "-";
	const struct flash_area *fa;

	if (flash_area_open(area_id, &fa) == 0) {
		uint32_t w[8];
		uint32_t offs[2] = {0, 0x1000};

		for (int i = 0; i < 2; i++) {
			char *dst = i == 0 ? p0 : p4k;

			memset(w, 0, sizeof(w));
			if (flash_area_read(fa, offs[i], w, sizeof(w)) != 0) {
				snprintf(dst, 72, "read-err");
				continue;
			}
			snprintf(dst, 72, "m=0x%08x sz=%u v=%u.%u.%u",
				 w[0], w[3],
				 (unsigned)(w[5] >> 24), (unsigned)((w[5] >> 16) & 0xff),
				 (unsigned)(w[5] & 0xffff));
		}
		flash_area_close(fa);
	}

	if (rc != 0) {
		snprintf(out, len, "\"%s\":{\"rc\":%d,\"p0\":\"%s\",\"p4k\":\"%s\"}",
			 key, rc, p0, p4k);
		return;
	}

	snprintf(out, len,
		 "\"%s\":{\"rc\":0,\"ver\":\"%u.%u.%u\",\"size\":%u,\"p0\":\"%s\",\"p4k\":\"%s\"}",
		 key, h.h.v1.sem_ver.major, h.h.v1.sem_ver.minor,
		 h.h.v1.sem_ver.revision, (unsigned int)h.h.v1.image_size, p0, p4k);
}

static int slots_handler(struct http_client_ctx *client, enum http_transaction_status status,
			 const struct http_request_ctx *request_ctx,
			 struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	char s0[200];
	char s1[200];

	slot_one_json(s0, sizeof(s0), "slot0",
		      FIXED_PARTITION_ID(slot0_partition));
	slot_one_json(s1, sizeof(s1), "slot1",
		      FIXED_PARTITION_ID(slot1_partition));

	snprintf(g_slots_json, sizeof(g_slots_json),
		 "{%s,%s,\"confirmed\":%s,\"local_ver\":%d,\"swap\":%d}",
		 s0, s1,
		 boot_is_img_confirmed() ? "true" : "false",
		 OTA_LOCAL_VERSION, (int)mcuboot_swap_type());

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_slots_json;
	response_ctx->body_len = strlen(g_slots_json);
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic slots_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = slots_handler,
};

/* ------------------- GET /api/ota/revert：把备用槽标成待启动 -----------------
 *
 * 这是「应用侧回滚」的唯一原语：boot_request_upgrade() 写的是**非运行槽**的
 * trailer（OTA 每天在做、且实测成功的那个操作），把 slot1 里那份镜像标成
 * 「下次启动换过去」。重启后 mcuboot 真的搬不搬，得看 swap 类型和 local_ver。
 *
 * permanent=1 → 换过去后直接转正（不回弹）；permanent=0 → 只跑一次（test）。
 * 故意不做成自动调用：它是「决定回退」这一步，必须由人/策略显式触发。
 */
static char g_revert_json[160];

static int revert_handler(struct http_client_ctx *client, enum http_transaction_status status,
			  const struct http_request_ctx *request_ctx,
			  struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	int rc = boot_request_upgrade(BOOT_UPGRADE_PERMANENT);

	LOG_INF("ota revert: request pending on slot1 rc=%d swap=%d local_ver=%d",
		rc, (int)mcuboot_swap_type(), OTA_LOCAL_VERSION);

	snprintf(g_revert_json, sizeof(g_revert_json),
		 "{\"rc\":%d,\"local_ver\":%d,\"swap\":%d,\"ok\":%s}",
		 rc, OTA_LOCAL_VERSION, (int)mcuboot_swap_type(),
		 rc == 0 ? "true" : "false");

	response_ctx->status = rc == 0 ? HTTP_200_OK : HTTP_500_INTERNAL_SERVER_ERROR;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_revert_json;
	response_ctx->body_len = strlen(g_revert_json);
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic revert_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_POST),
	},
	.cb = revert_handler,
};

/* --------------------------- GET/POST /api/pw?pw=xxx：改配网页口令 ------- */
static char g_pwjson[48];

static int pw_handler(struct http_client_ctx *client, enum http_transaction_status status,
		      const struct http_request_ctx *request_ctx,
		      struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	int rc = -EINVAL;

	if (request_ctx && request_ctx->data_len > 0) {
		char buf[96];
		size_t n = MIN(request_ctx->data_len, sizeof(buf) - 1);

		memcpy(buf, request_ctx->data, n);
		buf[n] = '\0';
		url_decode(buf);

		char *p = strstr(buf, "pw=");

		if (p) {
			char *amp;

			p += 3;
			amp = strchr(p, '&');
			if (amp) {
				*amp = '\0';
			}
			rc = ops_auth_set_pw(p);
			if (rc == 0) {
				LOG_INF("auth password updated (%u chars)", (unsigned)strlen(p));
			} else {
				LOG_WRN("auth password rejected (%d)", rc);
			}
		}
	}

	snprintf(g_pwjson, sizeof(g_pwjson), "{\"ok\":%s}", rc == 0 ? "true" : "false");

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)g_pwjson;
	response_ctx->body_len = strlen(g_pwjson);
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic wlog_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = wlog_handler,
};

struct http_resource_detail_dynamic boot_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = boot_handler,
};

struct http_resource_detail_dynamic pw_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		/* GET 也支持：页面把新口令放 query 里，和 connect 一样的写法 */
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_POST),
	},
	.cb = pw_handler,
};

/* ------------------ GET|POST /api/shell?cmd=xxx：网页内远程命令 ------------
 * 为什么不在 HTTP 回调里直接执行：shell 要解析命令、跑 printf、部分命令还会
 * 递归调用，压在 HTTP server 那 6 KB 栈上风险太高（栈一越界就不是 401 而是
 * "整块板子 halt"）。这里把命令递交给独立线程（6 KB），回调只等结果。
 *
 * 为什么用 shell_dummy：串口后端的输出是流式写出去的，拿不回来；
 * dummy 后端把输出收进内存缓冲，shell_backend_dummy_get_output() 能整段取回。
 * （Telnet 后端已于 2026-11 关闭：它是 AP 侧唯一能拿到交互式 shell 的漏口。）
 *
 * 一次只允许一条命令在跑：并发会让输出缓冲互相覆盖，shell 命令执行本身
 * 也不是可重入的。
 */
#if defined(CONFIG_SHELL_BACKEND_DUMMY)
#define SH_CMD_MAX 160
#define SH_OUT_MAX 2048

static char g_sh_cmd[SH_CMD_MAX];
static char g_sh_out[SH_OUT_MAX];
static char g_sh_json[SH_OUT_MAX + 96];
static K_SEM_DEFINE(sh_req, 0, 1);
static K_SEM_DEFINE(sh_done, 0, 1);
static volatile bool g_sh_busy;
/* 等待方超时后置 false：线程跑完就别再 give，否则下一次请求会立刻拿到
 * 上一次的 done，读到的是过期输出 */
static volatile bool g_sh_waiting;

/* JSON 字符串转义。终端输出里 CR/LF 很多，CR 直接丢，LF 转成 \n */
static void sh_json_escape(const char *src, char *dst, size_t cap)
{
	size_t o = 0;

	for (size_t i = 0; src[i] != '\0' && o + 7 < cap; i++) {
		char c = src[i];

		if (c == '"' || c == '\\') {
			dst[o++] = '\\';
			dst[o++] = c;
		} else if (c == '\n') {
			dst[o++] = '\\';
			dst[o++] = 'n';
		} else if (c == '\t') {
			dst[o++] = ' ';
		} else if (c == '\r') {
			continue;
		} else if ((unsigned char)c < 0x20U) {
			continue;
		} else {
			dst[o++] = c;
		}
	}
	dst[o] = '\0';
}

static void sh_cmd_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	const struct shell *sh = shell_backend_dummy_get_ptr();

	while (1) {
		size_t len = 0;
		const char *out;

		k_sem_take(&sh_req, K_FOREVER);

		shell_backend_dummy_clear_output(sh);
		shell_backend_dummy_clear_input(sh);
		shell_execute_cmd(sh, g_sh_cmd);
		out = shell_backend_dummy_get_output(sh, &len);

		if (out != NULL && len > 0) {
			size_t n = MIN(len, sizeof(g_sh_out) - 1);

			memcpy(g_sh_out, out, n);
			g_sh_out[n] = '\0';
		} else {
			g_sh_out[0] = '\0';
		}

		g_sh_busy = false;
		if (g_sh_waiting) {
			k_sem_give(&sh_done);
		}
	}
}

K_THREAD_DEFINE(sh_cmd_tid, 6144, sh_cmd_thread, NULL, NULL, NULL, 7, 0, 0);

static void sh_reply(struct http_response_ctx *response_ctx, const char *json)
{
	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = json_header;
	response_ctx->header_count = ARRAY_SIZE(json_header);
	response_ctx->body = (const uint8_t *)json;
	response_ctx->body_len = strlen(json);
	response_ctx->final_chunk = true;
}

static int shell_handler(struct http_client_ctx *client, enum http_transaction_status status,
			 const struct http_request_ctx *request_ctx,
			 struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	ARG_UNUSED(status);
	ARG_UNUSED(user_data);

	AUTH_GUARD();
	DEV_GUARD();

	/* 先判忙：命令还在跑时拒绝，避免输出缓冲被两条命令同时写 */
	if (g_sh_busy) {
		snprintf(g_sh_json, sizeof(g_sh_json), "{\"ok\":false,\"err\":\"busy\"}");
		sh_reply(response_ctx, g_sh_json);
		return 0;
	}

	if (request_ctx == NULL || request_ctx->data == NULL || request_ctx->data_len == 0) {
		snprintf(g_sh_json, sizeof(g_sh_json), "{\"ok\":false,\"err\":\"missing cmd\"}");
		sh_reply(response_ctx, g_sh_json);
		return 0;
	}

	char buf[192];
	size_t n = MIN(request_ctx->data_len, sizeof(buf) - 1);

	memcpy(buf, request_ctx->data, n);
	buf[n] = '\0';
	url_decode(buf);

	char *p = strstr(buf, "cmd=");

	if (p == NULL) {
		snprintf(g_sh_json, sizeof(g_sh_json), "{\"ok\":false,\"err\":\"missing cmd\"}");
		sh_reply(response_ctx, g_sh_json);
		return 0;
	}
	p += 4;

	char *amp = strchr(p, '&');

	if (amp != NULL) {
		*amp = '\0';
	}
	if (*p == '\0') {
		snprintf(g_sh_json, sizeof(g_sh_json), "{\"ok\":false,\"err\":\"empty cmd\"}");
		sh_reply(response_ctx, g_sh_json);
		return 0;
	}

	snprintf(g_sh_cmd, sizeof(g_sh_cmd), "%s", p);

	g_sh_busy = true;
	g_sh_waiting = true;
	k_sem_give(&sh_req);

	if (k_sem_take(&sh_done, K_MSEC(2500)) != 0) {
		/* 命令没在 2.5 s 内结束（例如 shell sleep）。不惜把 HTTP 线程卡更久，
		 * 直接告诉调用方超时，输出以后台日志为准。 */
		g_sh_waiting = false;
		snprintf(g_sh_json, sizeof(g_sh_json), "{\"ok\":false,\"err\":\"timeout\"}");
		sh_reply(response_ctx, g_sh_json);
		return 0;
	}

	{
		size_t head = (size_t)snprintf(g_sh_json, sizeof(g_sh_json),
					       "{\"ok\":true,\"out\":\"");

		sh_json_escape(g_sh_out, g_sh_json + head, sizeof(g_sh_json) - head - 8);
		strcat(g_sh_json, "\"}");
	}

	sh_reply(response_ctx, g_sh_json);

	return 0;
}

struct http_resource_detail_dynamic shell_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		/* GET 也放开：浏览器地址栏直接敲 /api/shell?cmd=kernel+uptime&k=...
		 * 就能试，不用先写页面 */
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_POST),
	},
	.cb = shell_handler,
};
#endif /* CONFIG_SHELL_BACKEND_DUMMY */
