/*
 * Korvo-1 系统层示例：只读 / 无副作用 的系统信息观测
 *
 *   sys info    复位原因（只读寄存器）/ 器件 ID / 运行时长
 *   sys temp    片上温度传感器（coretemp）
 *   sys rand    硬件 RNG（trng0）
 *   sys dev     列出 wdt0 / trng0 / coretemp 是否就绪
 *
 * 说明：看门狗的「安装超时」「故意卡死看它复位」「软件复位」三项
 * 会真的让板子重启，按本机执行策略不做，只在教程里给出代码与原理。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/version.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/random/random.h>
#include <zephyr/crypto/crypto.h>
#include <zephyr/crypto/hash.h>
#include <zephyr/crypto/cipher.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(ksys, LOG_LEVEL_INF);

#define TEMP_NODE DT_NODELABEL(coretemp)
#define WDT_NODE  DT_ALIAS(watchdog0)
#define SHA_NODE  DT_NODELABEL(sha)
#define AES_NODE  DT_NODELABEL(aes)

static const struct device *const temp_dev = DEVICE_DT_GET(TEMP_NODE);
static const struct device *const wdt_dev = DEVICE_DT_GET(WDT_NODE);
static const struct device *const sha_dev = DEVICE_DT_GET(SHA_NODE);
static const struct device *const aes_dev = DEVICE_DT_GET(AES_NODE);

/* 这棵树没有 hwinfo_reset_cause_to_str()，自己把位图翻成人话 */
static const char *reset_cause_str(uint32_t cause)
{
	static char buf[96];
	int n = 0;

	buf[0] = '\0';
	for (int bit = 0; bit < 32; bit++) {
		const char *name = NULL;

		if (!(cause & BIT(bit))) {
			continue;
		}

		switch (BIT(bit)) {
		case RESET_PIN:            name = "PIN"; break;
		case RESET_SOFTWARE:       name = "SOFTWARE"; break;
		case RESET_BROWNOUT:       name = "BROWNOUT"; break;
		case RESET_POR:            name = "POWER_ON"; break;
		case RESET_WATCHDOG:       name = "WATCHDOG"; break;
		case RESET_DEBUG:          name = "DEBUG"; break;
		case RESET_SECURITY:       name = "SECURITY"; break;
		case RESET_LOW_POWER_WAKE: name = "LOW_POWER_WAKE"; break;
		case RESET_CPU_LOCKUP:     name = "CPU_LOCKUP"; break;
		case RESET_PARITY:         name = "PARITY"; break;
		case RESET_PLL:            name = "PLL"; break;
		case RESET_CLOCK:          name = "CLOCK"; break;
		case RESET_HARDWARE:       name = "HARDWARE"; break;
		case RESET_USER:           name = "USER"; break;
		case RESET_TEMPERATURE:    name = "TEMPERATURE"; break;
		case RESET_BOOTLOADER:     name = "BOOTLOADER"; break;
		case RESET_FLASH:          name = "FLASH"; break;
		default:                   name = NULL; break;
		}

		if (name) {
			n += snprintf(buf + n, sizeof(buf) - n, "%s%s", n ? " | " : "", name);
			if (n >= (int)sizeof(buf) - 1) {
				break;
			}
		}
	}

	return n ? buf : "UNKNOWN";
}

/* ------------------------------------------------------------ sys info */
static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	uint32_t cause = 0;
	uint8_t id[16];
	ssize_t len;

	if (hwinfo_get_reset_cause(&cause) == 0) {
		shell_print(sh, "[info] 复位原因位图 = 0x%08x", cause);
		shell_print(sh, "[info] 复位原因 = %s", reset_cause_str(cause));
	}

	len = hwinfo_get_device_id(id, sizeof(id));
	if (len > 0) {
		shell_print(sh, "[info] 器件 ID = %02x:%02x:%02x:%02x:%02x:%02x (len=%d)",
			    id[0], id[1], id[2], id[3], id[4], id[5], (int)len);
	}

	shell_print(sh, "[info] 运行时长 = %u ms", k_uptime_get_32());
	shell_print(sh, "[info] Zephyr 版本 = %s", KERNEL_VERSION_STRING);
	shell_print(sh, "[info] 板 = %s", CONFIG_BOARD_TARGET);
	return 0;
}

/* ------------------------------------------------------------ sys temp */
static int cmd_temp(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 3;

	if (n <= 0 || n > 50) {
		n = 3;
	}

	if (!device_is_ready(temp_dev)) {
		shell_error(sh, "coretemp 未就绪");
		return -ENODEV;
	}

	shell_print(sh, "[temp] 采样 %d 次", n);
	for (int i = 0; i < n; i++) {
		struct sensor_value v;

		if (sensor_sample_fetch(temp_dev) != 0) {
			shell_error(sh, "fetch 失败");
			return -EIO;
		}
		/* esp32_temp 驱动只实现 SENSOR_CHAN_DIE_TEMP（芯片结温），
		 * 传 AMBIENT_TEMP 会直接返回 -ENOTSUP */
		if (sensor_channel_get(temp_dev, SENSOR_CHAN_DIE_TEMP, &v) != 0) {
			shell_error(sh, "channel_get 失败");
			return -EIO;
		}
		shell_print(sh, "[temp] %d.%02d °C",
			    v.val1, (v.val2 < 0 ? -v.val2 : v.val2) / 10000);
		k_msleep(200);
	}
	return 0;
}

/* ------------------------------------------------------------ sys rand */
static int cmd_rand(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 4;

	if (n <= 0 || n > 32) {
		n = 4;
	}

	shell_print(sh, "[rand] 从硬件熵源取 %d 组", n);
	for (int i = 0; i < n; i++) {
		uint32_t a = 0, b = 0;

		if (sys_csrand_get(&a, sizeof(a)) != 0 ||
		    sys_csrand_get(&b, sizeof(b)) != 0) {
			shell_error(sh, "熵源读取失败");
			return -EIO;
		}
		shell_print(sh, "[rand] 0x%08x 0x%08x", a, b);
	}
	return 0;
}

/* ------------------------------------------------------------- sys dev */
static int cmd_dev(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[dev] coretemp  = %s (ready=%d)", temp_dev->name,
		    device_is_ready(temp_dev));
	shell_print(sh, "[dev] watchdog0 = %s (ready=%d)", wdt_dev->name,
		    device_is_ready(wdt_dev));
	shell_print(sh, "[dev] 说明：只读设备状态，不安装超时（安装后停止喂狗会复位板子）");
	return 0;
}

/* ------------------------------------------------------ 硬件加密：工具函数 */
static void hex_dump(const struct shell *sh, const char *tag,
		     const uint8_t *p, size_t n)
{
	char line[128];
	int k = 0;

	k += snprintf(line + k, sizeof(line) - k, "%s", tag);
	for (size_t i = 0; i < n && k < (int)sizeof(line) - 3; i++) {
		k += snprintf(line + k, sizeof(line) - k, "%02x", p[i]);
	}
	shell_print(sh, "%s", line);
}

/* ------------------------------------------------------------- sys sha */
/*
 * 硬件 SHA 的"已知答案测试"（KAT）：
 * 不拿它跟自己的软件实现比，而是跟**标准里写死的答案**比。
 * 只要有一个 bit 不对，就说明这条链路（会话/驱动/寄存器）有问题。
 */
static const uint8_t kat_abc[32] = {
	0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea,
	0x41, 0x41, 0x40, 0xde, 0x5d, 0xae, 0x22, 0x23,
	0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c,
	0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad,
};

static const uint8_t kat_empty[32] = {
	0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14,
	0x9a, 0xfb, 0xf4, 0xc8, 0x99, 0x6f, 0xb9, 0x24,
	0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b, 0x93, 0x4c,
	0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55,
};

static uint8_t sha_buf[16384];      /* 吞吐测试用（16 KB，避免吃太多 RAM） */
static uint8_t sha_out[32];

/*
 * 一次哈希：begin_session → compute → free_session。
 * SHA 驱动只声明了 CAP_SEPARATE_IO_BUFS | CAP_SYNC_OPS（没有 INPLACE），
 * 所以输入输出必须是两个独立缓冲，flags 也只能这么填，否则 assert 直接挂。
 */
static int sha256_once(const uint8_t *in, size_t len, uint8_t *out)
{
	struct hash_ctx ctx = {
		.flags = CAP_SEPARATE_IO_BUFS | CAP_SYNC_OPS,
	};
	struct hash_pkt pkt = {
		.in_buf = in,
		.in_len = len,
		.out_buf = out,
	};
	int ret;

	ret = hash_begin_session(sha_dev, &ctx, CRYPTO_HASH_ALGO_SHA256);
	if (ret != 0) {
		return ret;
	}
	ret = hash_compute(&ctx, &pkt);
	(void)hash_free_session(sha_dev, &ctx);

	return ret;
}

static int cmd_sha(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t t0, t1;
	int loops = (argc > 1) ? atoi(argv[1]) : 8;
	int ret;

	if (loops <= 0 || loops > 64) {
		loops = 8;
	}

	if (!device_is_ready(sha_dev)) {
		shell_error(sh, "sha 设备未就绪");
		return -ENODEV;
	}
	shell_print(sh, "[sha] 设备=%s 驱动 hwcaps=0x%x", sha_dev->name,
		    crypto_query_hwcaps(sha_dev));

	/* KAT 1：SHA-256("abc") */
	ret = sha256_once((const uint8_t *)"abc", 3, sha_out);
	if (ret != 0) {
		shell_error(sh, "begin/compute 失败 ret=%d", ret);
		return ret;
	}
	hex_dump(sh, "[sha] ab    = ", sha_out, 32);
	shell_print(sh, "[sha] KAT(\"abc\")   %s",
		    memcmp(sha_out, kat_abc, 32) == 0 ? "PASS" : "FAIL");

	/* KAT 2：SHA-256("")，这条专治"padding 写错" */
	ret = sha256_once((const uint8_t *)"", 0, sha_out);
	if (ret != 0) {
		return ret;
	}
	hex_dump(sh, "[sha] empty = ", sha_out, 32);
	shell_print(sh, "[sha] KAT(\"\")      %s",
		    memcmp(sha_out, kat_empty, 32) == 0 ? "PASS" : "FAIL");

	/* 吞吐：16 KB × loops 次，用硬件周期算速率 */
	memset(sha_buf, 0x5a, sizeof(sha_buf));
	t0 = k_cycle_get_32();
	for (int i = 0; i < loops; i++) {
		if (sha256_once(sha_buf, sizeof(sha_buf), sha_out) != 0) {
			shell_error(sh, "吞吐测试中断");
			return -EIO;
		}
	}
	t1 = k_cycle_get_32();

	uint32_t cyc = t1 - t0;
	uint32_t hz = sys_clock_hw_cycles_per_sec();
	uint64_t bytes = (uint64_t)sizeof(sha_buf) * loops;
	uint64_t bps = (uint64_t)bytes * hz / (cyc ? cyc : 1);

	shell_print(sh, "[sha] 吞吐 %llu B / %u cyc = %llu B/s（约 %llu KB/s，%d 次 × %u B）",
		    (unsigned long long)bytes, cyc,
		    (unsigned long long)bps, (unsigned long long)(bps / 1024), loops,
		    (unsigned)sizeof(sha_buf));
	return 0;
}


/* ------------------------------------------------------------- sys aes */
/*
 * AES-128 ECB 的 NIST 官方测试向量（FIPS-197 附录 B）：
 *   key = 000102...0f
 *   pt  = 00112233445566778899aabbccddeeff
 *   ct  = 69c4e0d86a7b0430d8cdb78070b4c55a
 * 加密算一遍、解密再算回来，两次都对才叫"通了"。
 */
static const uint8_t aes_key[16] = {
	0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
	0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
};
static const uint8_t aes_pt[16] = {
	0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
	0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff,
};
static const uint8_t aes_ct_kat[16] = {
	0x69, 0xc4, 0xe0, 0xd8, 0x6a, 0x7b, 0x04, 0x30,
	0xd8, 0xcd, 0xb7, 0x80, 0x70, 0xb4, 0xc5, 0x5a,
};

static uint8_t aes_in[16];
static uint8_t aes_out[16];

/* 一次 ECB 块运算；op 决定加密还是解密 */
static int aes_ecb_block(enum cipher_op op, const uint8_t *in, uint8_t *out)
{
	struct cipher_ctx ctx = {
		.keylen = 16,
		.key = { .bit_stream = aes_key },
		/* esp32 aes 驱动声明：CAP_RAW_KEY | INPLACE | SEPARATE | SYNC | NO_IV_PREFIX */
		.flags = CAP_RAW_KEY | CAP_SEPARATE_IO_BUFS | CAP_SYNC_OPS | CAP_NO_IV_PREFIX,
	};
	struct cipher_pkt pkt = {
		.in_buf = (uint8_t *)in,
		.in_len = 16,
		.out_buf = out,
		.out_buf_max = 16,
	};
	int ret;

	ret = cipher_begin_session(aes_dev, &ctx, CRYPTO_CIPHER_ALGO_AES,
				   CRYPTO_CIPHER_MODE_ECB, op);
	if (ret != 0) {
		return ret;
	}
	ret = cipher_block_op(&ctx, &pkt);
	(void)cipher_free_session(aes_dev, &ctx);

	return ret;
}

static int cmd_aes(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t t0, t1;
	int loops = (argc > 1) ? atoi(argv[1]) : 1000;
	int ret;

	if (loops <= 0 || loops > 100000) {
		loops = 1000;
	}
	if (!device_is_ready(aes_dev)) {
		shell_error(sh, "aes 设备未就绪");
		return -ENODEV;
	}
	shell_print(sh, "[aes] 设备=%s 驱动 hwcaps=0x%x", aes_dev->name,
		    crypto_query_hwcaps(aes_dev));

	/* 加密：对 NIST 向量 */
	ret = aes_ecb_block(CRYPTO_CIPHER_OP_ENCRYPT, aes_pt, aes_out);
	if (ret != 0) {
		shell_error(sh, "加密失败 ret=%d", ret);
		return ret;
	}
	hex_dump(sh, "[aes] 明文  = ", aes_pt, 16);
	hex_dump(sh, "[aes] 密文  = ", aes_out, 16);
	hex_dump(sh, "[aes] 期望  = ", aes_ct_kat, 16);
	shell_print(sh, "[aes] KAT(encrypt) %s",
		    memcmp(aes_out, aes_ct_kat, 16) == 0 ? "PASS" : "FAIL");

	/* 解密：把密文还原回明文，验证 decrypt 路径也通 */
	ret = aes_ecb_block(CRYPTO_CIPHER_OP_DECRYPT, aes_ct_kat, aes_in);
	if (ret != 0) {
		shell_error(sh, "解密失败 ret=%d", ret);
		return ret;
	}
	shell_print(sh, "[aes] KAT(decrypt→明文) %s",
		    memcmp(aes_in, aes_pt, 16) == 0 ? "PASS" : "FAIL");

	/* 吞吐：这次**复用同一个会话**，只换输入块。
	 * 对比着看就知道 begin/free 的开销有多大（下面先测"每块开关一次"）。 */
	t0 = k_cycle_get_32();
	for (int i = 0; i < loops; i++) {
		if (aes_ecb_block(CRYPTO_CIPHER_OP_ENCRYPT, aes_pt, aes_out) != 0) {
			shell_error(sh, "吞吐测试中断");
			return -EIO;
		}
	}
	t1 = k_cycle_get_32();

	uint32_t cyc = t1 - t0;
	uint32_t hz = sys_clock_hw_cycles_per_sec();
	uint64_t bytes = (uint64_t)16 * loops;
	uint64_t bps = (uint64_t)bytes * hz / (cyc ? cyc : 1);

	shell_print(sh, "[aes] 每块开关会话：%d 块 / %u cyc = %llu B/s（约 %llu KB/s）",
		    loops, cyc, (unsigned long long)bps, (unsigned long long)(bps / 1024));

	/* 复用会话：begin 一次 → N 次 block_op → free 一次 */
	struct cipher_ctx ctx = {
		.keylen = 16,
		.key = { .bit_stream = aes_key },
		.flags = CAP_RAW_KEY | CAP_SEPARATE_IO_BUFS | CAP_SYNC_OPS | CAP_NO_IV_PREFIX,
	};
	struct cipher_pkt pkt = {
		.in_buf = (uint8_t *)aes_pt,
		.in_len = 16,
		.out_buf = aes_out,
		.out_buf_max = 16,
	};

	ret = cipher_begin_session(aes_dev, &ctx, CRYPTO_CIPHER_ALGO_AES,
				   CRYPTO_CIPHER_MODE_ECB, CRYPTO_CIPHER_OP_ENCRYPT);
	if (ret != 0) {
		shell_error(sh, "复用会话 begin 失败 ret=%d", ret);
		return ret;
	}

	t0 = k_cycle_get_32();
	for (int i = 0; i < loops; i++) {
		if (cipher_block_op(&ctx, &pkt) != 0) {
			(void)cipher_free_session(aes_dev, &ctx);
			shell_error(sh, "块运算失败");
			return -EIO;
		}
	}
	t1 = k_cycle_get_32();
	(void)cipher_free_session(aes_dev, &ctx);

	cyc = t1 - t0;
	bps = (uint64_t)bytes * hz / (cyc ? cyc : 1);

	shell_print(sh, "[aes] 复用会话：    %d 块 / %u cyc = %llu B/s（约 %llu KB/s）",
		    loops, cyc, (unsigned long long)bps, (unsigned long long)(bps / 1024));
	shell_print(sh, "[aes] 结论：会话开关本身比一次块运算还贵，高频小块要用长会话");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_sys,
	SHELL_CMD(info, NULL, "复位原因 / 器件 ID / 运行时长", cmd_info),
	SHELL_CMD(temp, NULL, "片上温度 [次数]", cmd_temp),
	SHELL_CMD(rand, NULL, "硬件随机数 [组数]", cmd_rand),
	SHELL_CMD(dev, NULL, "wdt0 / trng0 / coretemp 就绪状态", cmd_dev),
	SHELL_CMD(sha, NULL, "硬件 SHA-256：KAT + 吞吐 [循环次数]", cmd_sha),
	SHELL_CMD(aes, NULL, "硬件 AES-128 ECB：KAT + 吞吐 [块数]", cmd_aes),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(sys, &sub_sys, "Korvo-1 系统层示例", NULL);

int main(void)
{
	LOG_INF("board=%s", CONFIG_BOARD_TARGET);
	LOG_INF("coretemp ready=%d", device_is_ready(temp_dev));

	printk("\n=== Korvo-1 sys ===\n");
	printk("sys info | sys temp 3 | sys rand 4 | sys dev\n");
	printk("sys sha 8 | sys aes 1000（硬件加密，已知答案自检）\n");
	return 0;
}
