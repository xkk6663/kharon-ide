/*
 * korvo_nvs —— 持久化实验台
 *
 * 目的：证明"数据真的落到了 flash 上"，不是留在 RAM 里。
 * 验证方式：写一次 → 断电/复位 → 再读，值还在，且开机计数 +1。
 *
 * 分层：
 *   src/store.c   芯片无关（NVS + storage_partition）
 *   src/main.c    本工程的 shell 界面（这一层换板子才需要动）
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/logging/log.h>

#include "store.h"

LOG_MODULE_REGISTER(knvs, LOG_LEVEL_INF);

/* ---------------------------------------------------------------- nvs info */
static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	struct store_info info;
	uint32_t boot = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (store_get_info(&info) != 0) {
		shell_error(sh, "NVS 未挂载");
		return -ENODEV;
	}

	shell_print(sh, "[nvs] storage 分区 offset  = 0x%08x", info.offset);
	shell_print(sh, "[nvs] 扇区 = %u B × %u 个 = %u B",
		    info.sector_size, info.sector_count,
		    info.sector_size * info.sector_count);
	shell_print(sh, "[nvs] 可用容量估算 = %u B（留 1 个扇区做搬移）", info.capacity);

	if (store_get_u32(STORE_ID_BOOT_CNT, &boot) == 0) {
		shell_print(sh, "[nvs] 开机计数 = %u（本次是第 %u 次）", boot, boot);
	} else {
		shell_print(sh, "[nvs] 开机计数 = 未写入");
	}
	return 0;
}

/* ---------------------------------------------------------------- nvs get */
static int cmd_get(const struct shell *sh, size_t argc, char **argv)
{
	uint16_t id;
	uint32_t v;

	if (argc < 2) {
		shell_error(sh, "用法: nvs get <id>");
		return -EINVAL;
	}
	id = (uint16_t)atoi(argv[1]);

	if (store_get_u32(id, &v) == 0) {
		shell_print(sh, "[nvs] id=%u → u32 %u (0x%08x)", id, v, v);
		return 0;
	}

	/* 不是 u32，试试字符串 */
	char buf[64];
	size_t len = 0;

	if (store_get_str(id, buf, sizeof(buf), &len) == 0) {
		shell_print(sh, "[nvs] id=%u → str \"%s\" (%u B)", id, buf, (unsigned)len);
		return 0;
	}

	/* 还不是，试 blob */
	uint8_t raw[32];
	size_t n = 0;

	if (store_get_blob(id, raw, sizeof(raw), &n) == 0) {
		char hex[3 * 32 + 1];
		int p = 0;

		for (size_t i = 0; i < n && i < 32; i++) {
			p += snprintf(hex + p, sizeof(hex) - p, "%02x ", raw[i]);
		}
		shell_print(sh, "[nvs] id=%u → blob %u B: %s", id, (unsigned)n, hex);
		return 0;
	}

	shell_print(sh, "[nvs] id=%u 不存在", id);
	return -ENOENT;
}

/* ---------------------------------------------------------------- nvs set */
static int cmd_set(const struct shell *sh, size_t argc, char **argv)
{
	uint16_t id;
	uint32_t v;
	int rc;

	if (argc < 3) {
		shell_error(sh, "用法: nvs set <id> <u32>");
		return -EINVAL;
	}
	id = (uint16_t)atoi(argv[1]);
	v = (uint32_t)strtoul(argv[2], NULL, 0);

	rc = store_set_u32(id, v);
	if (rc < 0) {
		shell_error(sh, "写入失败 rc=%d", rc);
		return rc;
	}

	shell_print(sh, "[nvs] id=%u ← u32 %u 已写入（%d B）", id, v, rc);
	return 0;
}

/* ---------------------------------------------------------------- nvs del */
static int cmd_del(const struct shell *sh, size_t argc, char **argv)
{
	uint16_t id;
	int rc;

	if (argc < 2) {
		shell_error(sh, "用法: nvs del <id>");
		return -EINVAL;
	}
	id = (uint16_t)atoi(argv[1]);

	rc = store_del(id);
	if (rc < 0) {
		shell_error(sh, "删除失败 rc=%d", rc);
		return rc;
	}
	shell_print(sh, "[nvs] id=%u 已删除", id);
	return 0;
}

/* ---------------------------------------------------------------- nvs tag */
static int cmd_tag(const struct shell *sh, size_t argc, char **argv)
{
	int rc;

	if (argc < 2) {
		shell_error(sh, "用法: nvs tag <text>");
		return -EINVAL;
	}

	rc = store_set_str(STORE_ID_TAG, argv[1]);
	if (rc < 0) {
		shell_error(sh, "写入失败 rc=%d", rc);
		return rc;
	}
	shell_print(sh, "[nvs] 标签 \"%s\" 已写入（%d B）", argv[1], rc);
	return 0;
}

/* ---------------------------------------------------------------- nvs rgb */
static int cmd_rgb(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t rgb[3];
	int rc;

	if (argc < 4) {
		shell_error(sh, "用法: nvs rgb <r> <g> <b>  (0-255)");
		return -EINVAL;
	}
	for (int i = 0; i < 3; i++) {
		int v = atoi(argv[i + 1]);

		if (v < 0 || v > 255) {
			shell_error(sh, "每个分量必须在 0-255");
			return -EINVAL;
		}
		rgb[i] = (uint8_t)v;
	}

	rc = store_set_blob(STORE_ID_RGB, rgb, sizeof(rgb));
	if (rc < 0) {
		shell_error(sh, "写入失败 rc=%d", rc);
		return rc;
	}
	shell_print(sh, "[nvs] RGB = %02x %02x %02x 已写入（%d B）",
		    rgb[0], rgb[1], rgb[2], rc);
	return 0;
}

/* ------------------------------------------------------------- nvs bright */
static int cmd_bright(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t v;
	int rc;

	if (argc < 2) {
		shell_error(sh, "用法: nvs bright <0-100>");
		return -EINVAL;
	}
	v = (uint32_t)atoi(argv[1]);
	if (v > 100) {
		shell_error(sh, "亮度上限 100");
		return -EINVAL;
	}

	rc = store_set_u32(STORE_ID_BRIGHT, v);
	if (rc < 0) {
		shell_error(sh, "写入失败 rc=%d", rc);
		return rc;
	}
	shell_print(sh, "[nvs] 亮度 = %u%% 已写入（%d B）", v, rc);
	return 0;
}

/* ---------------------------------------------------------------- nvs dump */
static int cmd_dump(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[nvs] ---- 已知键 ----------");

	uint32_t v;
	char str[64];
	uint8_t rgb[8];
	size_t len = 0;

	if (store_get_u32(STORE_ID_BOOT_CNT, &v) == 0) {
		shell_print(sh, "  1 boot_cnt = %u", v);
	}
	if (store_get_str(STORE_ID_TAG, str, sizeof(str), &len) == 0) {
		shell_print(sh, "  2 tag      = \"%s\"", str);
	}
	if (store_get_blob(STORE_ID_RGB, rgb, sizeof(rgb), &len) == 0 && len == 3) {
		shell_print(sh, "  3 rgb      = %02x %02x %02x", rgb[0], rgb[1], rgb[2]);
	}
	if (store_get_u32(STORE_ID_BRIGHT, &v) == 0) {
		shell_print(sh, "  4 bright   = %u%%", v);
	}
	shell_print(sh, "[nvs] ---------------------");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_nvs,
	SHELL_CMD(info,   NULL, "分区几何 + 开机计数", cmd_info),
	SHELL_CMD(get,    NULL, "读一个键（自动识别 u32/str/blob）", cmd_get),
	SHELL_CMD(set,    NULL, "写 u32: nvs set <id> <value>", cmd_set),
	SHELL_CMD(del,    NULL, "删一个键: nvs del <id>", cmd_del),
	SHELL_CMD(tag,    NULL, "写字符串到 id=2", cmd_tag),
	SHELL_CMD(rgb,    NULL, "写 RGB 三字节到 id=3", cmd_rgb),
	SHELL_CMD(bright, NULL, "写亮度到 id=4", cmd_bright),
	SHELL_CMD(dump,   NULL, "列出所有已知键", cmd_dump),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(nvs, &sub_nvs, "持久化实验台（NVS）", NULL);

int main(void)
{
	uint32_t boot;

	LOG_INF("board=%s", CONFIG_BOARD_TARGET);

	if (store_init() != 0) {
		printk("\nNVS 挂载失败，后续命令都不可用\n");
		return 0;
	}

	boot = store_boot_count_bump();

	printk("\n=== Korvo-1 NVS ===\n");
	printk("本次是第 %u 次开机（计数已落盘）\n", boot);
	printk("nvs info | nvs dump | nvs set 10 123 | nvs get 10\n");
	printk("nvs tag korvo-1 | nvs rgb 20 30 40 | nvs bright 60 | nvs del 10\n");
	return 0;
}
