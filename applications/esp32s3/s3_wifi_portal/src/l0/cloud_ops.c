/*
 * cloud_ops —— 公网命令白名单执行器（实现）
 *
 * 只做"翻译"：把云端下发的 {cmd,args} 变成一次本地动作，并给人话结果。
 * 不持有任何网络状态，也不直接复位（复位由 ota_cloud 在回执之后触发）。
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cloud_ops.h"
#include "ops.h"
#include "ota.h"
#include "ota_cloud.h"

LOG_MODULE_REGISTER(cloud_ops, LOG_LEVEL_INF);

static struct cloud_ops_hooks h;   /* main.c 注册；未注册时相应命令返回 -ENODEV */

void cloud_ops_bind(const struct cloud_ops_hooks *hooks)
{
	if (hooks != NULL) {
		h = *hooks;
	}
}

/* 影子里的环境字段。字符串做最小转义（只挡引号与控制字符），
 * 这里不接受任何外部输入，SSID 来自用户配置，防御性处理即可。 */
static void json_put_str(char *buf, size_t n, const char *key, const char *val)
{
	size_t used = strlen(buf);
	size_t i = 0;

	/* 前导逗号由本函数负责：调用方只管按顺序往同一个对象里塞字段，
	 * 少写一个逗号就是一份非法 JSON（曾经把整份影子打成 400）。 */
	if (used > 0 && used + 1 < n) {
		buf[used++] = ',';
		buf[used] = '\0';
	}
	if (n <= used + 4) {
		return;
	}
	int w = snprintf(buf + used, n - used, "\"%s\":\"", key);

	if (w <= 0) {
		return;
	}
	used += (size_t)w;
	while (val != NULL && val[i] != '\0' && used + 8 < n) {
		char c = val[i++];

		if (c == '"' || c == '\\') {
			buf[used++] = '\\';
			buf[used++] = c;
		} else if ((unsigned char)c < 0x20) {
			buf[used++] = ' ';
		} else {
			buf[used++] = c;
		}
	}
	if (used + 3 < n) {
		buf[used++] = '"';
		buf[used] = '\0';
	}
}

void cloud_ops_env_json(char *buf, size_t n)
{
	char ssid_buf[40];

	if (buf == NULL || n < 8) {
		return;
	}
	buf[0] = '\0';

	snprintf(buf, n, ",\"rssi\":%d", h.rssi != NULL ? h.rssi() : 0);

	/* ssid/ip 都从 l2 取；未绑定时给空串，别让影子里出现 null */
	snprintf(ssid_buf, sizeof(ssid_buf), "%s", (h.ssid != NULL && h.ssid() != NULL) ?
		 h.ssid() : "");
	size_t used = strlen(buf);

	if (used + 12 < n) {
		json_put_str(buf, n, "ssid", ssid_buf);
	}
	snprintf(ssid_buf, sizeof(ssid_buf), "%s", (h.ip != NULL && h.ip() != NULL) ?
		 h.ip() : "");
	used = strlen(buf);
	if (used + 12 < n) {
		json_put_str(buf, n, "ip", ssid_buf);
	}
	used = strlen(buf);
	if (used + 40 < n) {
		snprintf(buf + used, n - used, ",\"net\":{\"dns_rc\":%d,\"http\":%d}",
			 h.net_dns_rc != NULL ? h.net_dns_rc() : 0,
			 h.net_http != NULL ? h.net_http() : 0);
	}
}

/* ---- 白名单 ------------------------------------------------------------- */
static int cmd_reboot(const char *args, char *out, size_t n, struct cloud_ops_ret *ret)
{
	bool cold = (args == NULL || strcmp(args, "cold") == 0 ||
		     strcmp(args, "warm") != 0);

	if (args != NULL && strcmp(args, "warm") == 0) {
		cold = false;
	}
	ret->want_reboot = 1;
	snprintf(out, n, "reboot scheduled (%s)", cold ? "cold" : "warm");
	return 0;
}

static int cmd_netcheck(char *out, size_t n)
{
	if (h.net_http == NULL && h.net_dns_rc == NULL) {
		snprintf(out, n, "netcheck not bound");
		return -ENODEV;
	}
	snprintf(out, n, "dns_rc=%d http=%d ip=%s",
		 h.net_dns_rc != NULL ? h.net_dns_rc() : -1,
		 h.net_http != NULL ? h.net_http() : -1,
		 (h.ip != NULL && h.ip() != NULL) ? h.ip() : "-");
	return 0;
}

static int cmd_led(const char *args, char *out, size_t n, struct cloud_ops_ret *ret)
{
	if (h.led_test == NULL) {
		snprintf(out, n, "led_test not bound");
		return -ENODEV;
	}
	const char *color = (args != NULL && args[0] != '\0') ? args : "purple";
	int rc = h.led_test(color, 5000);

	if (rc != 0) {
		snprintf(out, n, "led_test rejected (%d)", rc);
		return -EINVAL;
	}
	ret->fast_rounds = 1;
	snprintf(out, n, "led %s for 5s", color);
	return 0;
}

static int cmd_log(char *out, size_t n)
{
	static char tmp[512];

	ops_ev_json(tmp, sizeof(tmp));
	/* out 只有 512 B 量级，截断时明确标注，避免下游以为拿到了完整数组 */
	if (strlen(tmp) >= n) {
		snprintf(out, n, "%.*s", (int)(n - 20), tmp);
		size_t used = strlen(out);

		if (used + 16 < n) {
			snprintf(out + used, n - used, "…(truncated)");
		}
	} else {
		snprintf(out, n, "%s", tmp);
	}
	return 0;
}

static int cmd_cfg(const char *args, char *out, size_t n, struct cloud_ops_ret *ret)
{
	int sec = 0;

	if (args == NULL || sscanf(args, "hb=%d", &sec) != 1) {
		snprintf(out, n, "usage: hb=<sec> (60..3600)");
		return -EINVAL;
	}
	if (sec < 60 || sec > 3600) {
		snprintf(out, n, "hb out of range (%d)", sec);
		return -EINVAL;
	}
	ota_cloud_set_period((uint32_t)sec);
	ret->fast_rounds = 1;
	snprintf(out, n, "heartbeat period = %ds", sec);
	return 0;
}

static int cmd_info(char *out, size_t n)
{
	snprintf(out, n, "ver=%d proto=%d boot=%u crash=%u up=%us",
		 OTA_LOCAL_VERSION, OTA_PROTO_VER, ops_boot_count(),
		 ops_boot_crash_count(), (unsigned)(k_uptime_get() / 1000));
	return 0;
}

int cloud_ops_run(const char *name, const char *args, char *out, size_t out_n,
		  struct cloud_ops_ret *ret)
{
	memset(ret, 0, sizeof(*ret));
	if (out == NULL || out_n < 16) {
		return -EINVAL;
	}
	out[0] = '\0';
	if (name == NULL || name[0] == '\0') {
		snprintf(out, out_n, "empty cmd");
		ret->rc = -EINVAL;
		return ret->rc;
	}

	int rc;

	if (strcmp(name, "reboot") == 0) {
		rc = cmd_reboot(args, out, out_n, ret);
	} else if (strcmp(name, "ocheck") == 0) {
		ret->fast_rounds = 3;
		snprintf(out, out_n, "check scheduled");
		rc = 0;
	} else if (strcmp(name, "netcheck") == 0) {
		rc = cmd_netcheck(out, out_n);
	} else if (strcmp(name, "led_test") == 0) {
		rc = cmd_led(args, out, out_n, ret);
	} else if (strcmp(name, "log_dump") == 0) {
		rc = cmd_log(out, out_n);
	} else if (strcmp(name, "cfg_set") == 0) {
		rc = cmd_cfg(args, out, out_n, ret);
	} else if (strcmp(name, "info") == 0) {
		rc = cmd_info(out, out_n);
	} else {
		snprintf(out, out_n, "unknown cmd \"%.24s\"", name);
		rc = -ENOENT;
	}

	ret->rc = rc;
	LOG_INF("cloud cmd %s(%s) rc=%d: %s", name, args != NULL ? args : "-", rc, out);
	return rc;
}
