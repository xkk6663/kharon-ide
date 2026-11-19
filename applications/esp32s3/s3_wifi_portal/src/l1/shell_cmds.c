/*
 * shell_cmds —— 串口 `portal` 命令组（status / drop / saved / conn / led）
 *
 * L1 接口层（表达层）：它只是把 L2 的快照打印成人能读的文字，
 * 和网页 /api/* 用的是同一份数据。
 */
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/shell/shell.h>

#include "portal_cfg.h"
#include "cred.h"
#include "portal_ap.h"
#include "wifi_link.h"
#include "led_ctrl.h"
#include "ota_cloud.h"
#include "ota.h"

LOG_MODULE_REGISTER(portal_shell, LOG_LEVEL_INF);

/* ------------------------------------------------------------ 串口调试命令
 * 串口上敲 "portal status" 看状态，"portal drop" 主动断开 STA。
 * drop 的主要用途：验证运行期掉线重连，不用真的去拔路由器。
 */
static int cmd_portal_status(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	struct link_view v;

	link_view_get(&v);
	shell_print(sh, "state=%d ssid=\"%s\" ip=%s rssi=%d reconnects=%u uptime=%us",
		    v.state, v.link_ssid, v.ip, v.rssi, (unsigned)v.reconnects,
		    (unsigned)(k_uptime_get_32() / 1000U));

	return 0;
}

static int cmd_portal_drop(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	int ret = portal_disconnect();

	if (ret == -ENODEV) {
		shell_error(sh, "no STA interface");
	}

	shell_print(sh, "disconnect request -> %d (runtime reconnect suppressed)", ret);

	return ret;
}

/* 串口连网：`portal conn` 用 NVS 表头那条；`portal conn <ssid> [psk]` 直接指定。
 *
 * 为什么要加参数：换到新环境（比如热点叫 Power 而不是家里的 501A）时，
 * 板子上电只会反复重连 NVS 里那唯一一条旧凭据，日志就是
 *   connect failed, status 4 / sc=4
 * 而网页要能打开又得先有网 —— 死锁。串口不受 STA 掉线影响，
 * 给它一条"直接说连谁"的命令，就能把板子从任何离线状态拉回来。
 *
 * 指定了 SSID 就顺手 upsert 进 NVS（并挪到表头）：上电自动重连跟着换，
 * 否则重启后又会去追旧 SSID。 */
static int cmd_portal_conn(const struct shell *sh, size_t argc, char **argv)
{
	const char *want_ssid = NULL;
	size_t want_ssid_len = 0;
	const char *want_psk = NULL;
	size_t want_psk_len = 0;

	if (argc >= 2) {
		want_ssid = argv[1];
		want_ssid_len = strlen(want_ssid);
		if (want_ssid_len == 0 || want_ssid_len > WIFI_SSID_MAX_LEN) {
			shell_error(sh, "bad ssid length %u", (unsigned)want_ssid_len);
			return -EINVAL;
		}
		if (argc >= 3) {
			want_psk = argv[2];
			want_psk_len = strlen(want_psk);
			if (want_psk_len > 64) {
				shell_error(sh, "psk too long (%u)", (unsigned)want_psk_len);
				return -EINVAL;
			}
		} else {
			/* 只给 SSID：优先用 NVS 里同名那条的密码，省得在串口里敲密码 */
			const struct cred_item *it = cred_at(cred_index(want_ssid, want_ssid_len));

			if (it != NULL) {
				want_psk = it->psk;
				want_psk_len = it->psk_len;
			}
		}
	}

	if (want_ssid != NULL) {
		int rc = cred_upsert(want_ssid, want_ssid_len, want_psk ? want_psk : "",
				     want_psk_len);

		if (rc) {
			shell_error(sh, "save credential failed (%d)", rc);
			return rc;
		}
		shell_print(sh, "credential saved, now boot target");
		link_request_connect(want_ssid, want_ssid_len,
				     want_psk ? want_psk : "", want_psk_len);
	} else {
		if (cred_count() == 0) {
			shell_error(sh, "no saved network in NVS");
			return -ENOENT;
		}
		/* SSID 也照表头来，保证两边一致 */
		const struct cred_item *first = cred_at(0);

		link_request_connect(first->ssid, first->ssid_len, first->psk, first->psk_len);
	}

	shell_print(sh, "connecting to \"%s\"", link_target_ssid());

	return 0;
}

/* 列出 NVS 里保存过的网络；只显示密码长度，不打印明文 */
static int cmd_portal_saved(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "%u saved network(s), first is used on boot:", cred_count());

	for (int i = 0; i < cred_count(); i++) {
		const struct cred_item *it = cred_at(i);

		shell_print(sh, "  [%d] \"%s\" psk_len=%u%s", i, it->ssid, it->psk_len,
			    i == 0 ? "  <- boot target" : "");
	}

	return 0;
}

static int cmd_portal_led(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);

	if (!strcmp(argv[1], "off")) {
		led_ctrl_set(false);
		shell_print(sh, "led off");
		return 0;
	}
	if (!strcmp(argv[1], "on")) {
		led_ctrl_set(true);
		shell_print(sh, "led on");
		return 0;
	}
	if (!strcmp(argv[1], "toggle")) {
		led_ctrl_toggle();
		shell_print(sh, "led %s", led_ctrl_is_on() ? "on" : "off");
		return 0;
	}

	shell_error(sh, "usage: portal led on|off|toggle");
	return -EINVAL;
}

/* 诊断用：把 SoftAP 钉在一个指定信道（0 = 恢复 auto）。
 *
 * 单射频下 AP 与 STA 必须同信道，这个命令就是用来人工制造"AP 被钉在错信道"
 * 的现场，好验证开机重连时 connect_failed_now() 的对齐阶梯会不会自己把 AP
 * 搬回来（status 4 那条路）。它不走 Settings，重启即失效。 */
static int cmd_portal_apch(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t ch = (uint8_t)atoi(argv[1]);

	shell_print(sh, "ap channel pin -> %u (0 = auto); now ap=%u sta=%d pinned=%u",
		    (unsigned)ch, (unsigned)ap_current_channel(), link_sta_channel(),
		    (unsigned)ap_pinned_channel());
	ap_retarget(ch);

	return 0;
}


/* ---- 公网 OTA（ota_cloud）----
 * otoken：一次性把设备 token 写进 NVS（s3cloud/token）。token 不回显。
 * opsk：一次性把量产 PSK 写进 NVS（s3ota/psk），不写就吃编译期开发默认值。
 * ocheck：手动触发一次公网 check（绕过 15 分钟节流）。
 * ostat：公网通道快照（阶段/进度/上次失败原因）。 */
static int cmd_portal_otoken(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	int rc = ota_cloud_token_set(argv[1]);

	shell_print(sh, rc ? "otoken save failed (%d)" : "otoken saved to NVS (not shown)", rc);
	return 0;
}

static int cmd_portal_opsk(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	int rc = ota_psk_set(argv[1]);

	shell_print(sh, rc ? "opsk save failed (%d)" : "opsk saved to NVS s3ota/psk (not shown)",
		    rc);
	return 0;
}

static int cmd_portal_ocheck(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	ota_cloud_check_now();
	shell_print(sh, "cloud check queued (busy=%d)", ota_cloud_busy());
	return 0;
}

static int cmd_portal_ostat(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "%s", ota_cloud_snapshot_json());
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(portal_cmds,
	SHELL_CMD_ARG(status, NULL, "print portal link status", cmd_portal_status, 1, 0),
	SHELL_CMD_ARG(drop, NULL, "force STA disconnect (tests runtime reconnect)",
		      cmd_portal_drop, 1, 0),
	SHELL_CMD_ARG(saved, NULL, "list networks remembered in NVS", cmd_portal_saved, 1, 0),
	SHELL_CMD_ARG(conn, NULL, "connect: no args = first saved; or <ssid> [psk] "
		      "(saved to NVS and becomes boot target)", cmd_portal_conn, 1, 2),
	SHELL_CMD_ARG(led, NULL, "rgb led: on|off|toggle (KEY press does the same)",
		      cmd_portal_led, 2, 0),
	SHELL_CMD_ARG(apch, NULL, "pin SoftAP channel: <n> (0 = auto) — 诊断单射频对齐用",
		      cmd_portal_apch, 2, 0),
	SHELL_CMD_ARG(otoken, NULL, "store cloud device token to NVS: <hex> (never echoed)",
		      cmd_portal_otoken, 2, 0),
	SHELL_CMD_ARG(opsk, NULL, "store production OTA PSK to NVS: <psk> (never echoed)",
		      cmd_portal_opsk, 2, 0),
	SHELL_CMD_ARG(ocheck, NULL, "trigger one cloud OTA check now (bypass throttle)",
		      cmd_portal_ocheck, 1, 0),
	SHELL_CMD_ARG(ostat, NULL, "print cloud OTA channel snapshot",
		      cmd_portal_ostat, 1, 0),
	SHELL_SUBCMD_SET_END
);
SHELL_CMD_REGISTER(portal, &portal_cmds, "Wi-Fi portal debug commands", NULL);
