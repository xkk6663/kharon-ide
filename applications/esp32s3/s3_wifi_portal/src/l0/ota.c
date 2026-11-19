/*
 * OTA 接收端（独立 TCP :8080）
 *
 * 为什么不用 http_server：它的 body 是交给 handler 逐块回调的，几百 KB 固件
 * 挤在自己的缓冲里不合适；而且这里要按字节精确打点、要能中途拒收、要在切槽
 * 前做 flash 回读比对，独立 socket 的可控性高得多。
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/dfu/flash_img.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/settings/settings.h>
/* 续传要做"回读已写区间复算段摘要"，必须直接读 flash_area */
#include <zephyr/storage/flash_map.h>

/* 这份树里的 mbedTLS 是 4.x（CONFIG_MBEDTLS_VERSION_4_x），legacy 的
 * <mbedtls/sha256.h> 已经被删掉了，只能用 PSA 的哈希接口。 */
#include <psa/crypto.h>

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ota.h"
#include "ota_engine.h"
/* 主动重启前要把"本次运行"标记为干净退出，见 ops_boot_mark_clean() */
#include "ops.h"

LOG_MODULE_REGISTER(ota, CONFIG_LOG_DEFAULT_LEVEL);

/* 头最多读这么多字节。正常请求 < 600 B，留余量给各种浏览器自定义头。 */
#define OTA_HDR_MAX 1536
/* 一次 recv 的块大小。2 KB 兼顾 MTU 对齐与遍历开销。 */
#define OTA_CHUNK 2048

/* AP 侧网段的代表地址，只用来判断"这条连接是不是手机从 S3-Portal 连进来的"。
 * 值必须与 main.c 的 AP_IP 一致（同一个 AP 静态地址）；改 AP 网段时两边一起改。 */
#define OTA_AP_SIDE_IP "192.168.4.1"

enum ota_st {
	OTA_IDLE = 0,
	OTA_RECV,
	OTA_VERIFY,
	OTA_READY,
	OTA_FAIL,
};

static struct {
	volatile int st;
	volatile uint32_t written;   /* 已写入 slot1 的字节数 */
	volatile uint32_t total;     /* 本次声明的整包长度 */
	volatile uint32_t ver;       /* 本次上传声明的版本号 */
	volatile uint32_t t_dl;      /* 收包耗时 ms */
	volatile uint32_t t_verify;  /* 回读校验耗时 ms */
	volatile uint32_t t_total;   /* 总耗时 ms */
	volatile int32_t last_err;
	char err[64];                /* 失败原因，给人看 */
	char stage[16];              /* idle/recv/verify/ready/fail */
} g_ota;

/* ---------------- S5 自证窗口的状态（实现见文件后半段 ota_confirm_if_healthy） ----------------
 * 放在这里是因为 ota_snapshot_json() 也要读它们给 /api/status 用。 */
static uint32_t g_cand_ver;    /* 候选版本，0 = 没有候选 */
static uint32_t g_cand_crash;  /* 签名那一刻的崩溃计数基线 */
static bool g_cand_valid;
static bool g_win_started;
static uint32_t g_win_ticks;
/* 调用方的周期是 10 s（main.c 的 rssi work），下文按这个换算窗口长度。 */
#define OTA_WIN_TICK_MS 10000

/* 故障注入开关：置 1 后窗口一律判"不健康"，用来在真板上验证
 * "窗口超时 → ota_revert_to_secondary() → 下次启动回到旧槽"这条自动回退路径。
 * 为什么需要它：正常固件的链路是通的，不注入就永远走不到失败分支；
 * 断网、换错 SSID 这类"自然注入"会把验证完的板子留在离线状态，反而更难收场。
 * ★ 正常构建必须为 0，否则每一版正常固件都会在 60 s 后被自己回退掉。
 * 实测记录（2026-11-15）：置 1 的 v121 推上去后 60 s 打出
 *   <err> ota: self-test failed: link down after 60 s
 *   <wrn> ota: revert(link down in window): pending on slot1 (swap=3), rebooting into previous image
 * 重启后 local_ver 回到 120、clean=true、crash 仍 6（回退没被记成崩溃）。 */
#define OTA_SELFTEST_FORCE_FAIL 0


static struct flash_img_context g_fctx;
static uint32_t g_nonce_last;          /* 上一次成功升级用的 nonce，持久化 */

/* ---------------- S3 密钥 ----------------
 * 密钥来源按 skill 的要求写死一个顺序：NVS 里的 s3ota/psk → 编译期默认值。
 * 编译期默认值只为"开发板上开箱能用"，量产必须写独立 PSK；
 * 未开 Flash Encryption 时这串密钥是能从 Flash 里读出来的，这条边界写进文档，
 * 不能把它当成"已加密"。
 */
#define OTA_PSK_DEV_DEFAULT "s3ota-psk-dev-20261115"
static char g_psk[48] = OTA_PSK_DEV_DEFAULT;
static bool g_psk_from_nvs;

/* ---------------- 断点续传（skill 里的可选档，本工程选做） ----------------
 * 先把 skill 的硬约束抄在这里，免得后来的人"想当然"写坏：
 *   1) 哈希是流式的，跨重启**无法从中途累积**。所以续传只有一条正确路径：
 *      「先把已写区间回读一遍重算摘要，再从断点继续流式累积」。
 *   2) 断点记录必须自带 magic / 总长 / 期望摘要 / 已确认偏移 / 段摘要，
 *      加载时做 stale 校验；任一项对不上就丢弃记录、退化成全量。
 *   3) 断点只落在**段边界**（256 KB）。段内断掉就把偏移回退到上一个边界重写，
 *      这样"槽里内容"与"记录"永远自洽，代价是最多丢 256 KB。
 *   4) 只有"切槽成功"或"开始一次全新上传"才清记录；失败/断开一律保留。
 */
#define RB_MAGIC  0x52415442u          /* "RATB" */
#define RB_SEG    (256u * 1024u)       /* 段大小 */
#define RB_MAXSEG 6u                   /* 1280 KB 槽最多 5 段，留 1 段余量 */

struct rb_rec {
	uint32_t magic;
	uint32_t total;      /* 整包长度 */
	uint32_t ver;        /* 发起端声明版本 */
	uint32_t rw;         /* 已确认写入且计入摘要的偏移（段边界） */
	uint32_t seg_done;   /* 已完成段数 */
	uint8_t  sha[32];    /* 期望整包摘要（二进制） */
	uint8_t  seg[RB_MAXSEG][32];
};

static struct rb_rec g_rb;
static bool g_rb_valid;
/* 段哈希与整包哈希必须并行推进：整包 hop 属于 ota_do_upgrade 的局部，
 * 段这边的上下文放模块级，跨"收包循环"复用。 */
static psa_hash_operation_t g_hseg;
static bool g_hseg_live;
static uint32_t g_seg_fill;

static void rb_clear(void)
{
	if (settings_delete("s3ota/rb")) {
		/* 删不掉不算错（本来就没有），但要留下可查的痕迹 */
		LOG_DBG("settings_delete(s3ota/rb) failed");
	}
	memset(&g_rb, 0, sizeof(g_rb));
	g_rb_valid = false;
}

static void rb_save(void)
{
	int rc = settings_save_one("s3ota/rb", &g_rb, sizeof(g_rb));

	if (rc) {
		LOG_WRN("resume record save failed: %d", rc);
	}
}

/* 十六进制 → 32 字节摘要；长度不对返回 false */
static bool sha_hex_to_bin(const char *hex, uint8_t out[32])
{
	for (size_t i = 0; i < 32; i++) {
		uint8_t v = 0;
		for (size_t k = 0; k < 2; k++) {
			char c = hex[i * 2 + k];
			uint8_t d;
			if (c >= '0' && c <= '9') {
				d = (uint8_t)(c - '0');
			} else if (c >= 'a' && c <= 'f') {
				d = (uint8_t)(c - 'a' + 10);
			} else {
				return false;
			}
			v = (uint8_t)((v << 4) | d);
		}
		out[i] = v;
	}
	return true;
}

/* 段哈希喂数据。段满就落一次记录。
 * 注意 rw 用段号反推（seg_done * RB_SEG），不要用"chunk 结束偏移"，
 * 否则一段在 chunk 中间完成时记录会指到段中间，续传重写起点就错了。 */
static int seg_feed(const uint8_t *p, size_t n)
{
	if (!g_hseg_live) {
		return -EIO;
	}
	while (n > 0) {
		uint32_t room = RB_SEG - g_seg_fill;
		size_t take = n < room ? n : (size_t)room;

		if (psa_hash_update(&g_hseg, p, take) != PSA_SUCCESS) {
			return -EIO;
		}
		g_seg_fill += (uint32_t)take;
		p += take;
		n -= take;

		if (g_seg_fill == RB_SEG) {
			uint8_t dig[32];
			size_t dl = 0;

			if (g_rb.seg_done >= RB_MAXSEG) {
				return -ENOSPC;
			}
			if (psa_hash_finish(&g_hseg, dig, sizeof(dig), &dl) != PSA_SUCCESS ||
			    dl != sizeof(dig)) {
				g_hseg_live = false;
				return -EIO;
			}
			memcpy(g_rb.seg[g_rb.seg_done], dig, sizeof(dig));
			g_rb.seg_done++;
			g_rb.rw = g_rb.seg_done * RB_SEG;
			g_rb_valid = true;
			rb_save();
			LOG_INF("resume checkpoint: seg=%u off=%u", g_rb.seg_done, g_rb.rw);
			g_seg_fill = 0;
			if (psa_hash_setup(&g_hseg, PSA_ALG_SHA_256) != PSA_SUCCESS) {
				g_hseg_live = false;
				return -EIO;
			}
		}
	}
	return 0;
}

/* 续传前的"回读重算"：把 [0, rw) 从 Flash 读回来，
 * 逐段复算段摘要与记录比对，同时把这些字节喂进整包 hop（这样末尾 finish
 * 得到的仍是整包摘要，不需要重传）。
 *
 * 返回 0 表示可以续传；负数表示记录已 stale，调用方丢弃记录走全量。 */
static int rb_readback_verify(psa_hash_operation_t *hop, uint32_t rw)
{
	const struct flash_area *fa = NULL;
	uint8_t buf[4096];
	uint32_t img_start;
	int rc = 0;

	if (rw == 0) {
		return 0;
	}
	if (rw % 4096u != 0) {
		/* 段边界一定是 4 KB 对齐；不满足说明记录坏了 */
		return -EINVAL;
	}

	rc = flash_area_open(flash_img_get_upload_slot(), &fa);
	if (rc) {
		return rc;
	}

	/* OFFSET 模式下 flash_img_init() 把写入起点放在槽的**第二个扇区**
	 * （mcuboot 要求镜像从第二扇区开始），所以回读也要用同一个基址。
	 * 这里从 init 之后的 stream.offset 反推，避免把 4 KB 写死。 */
	img_start = (uint32_t)(g_fctx.stream.offset - g_fctx.flash_area->fa_off);

	uint32_t done = 0;

	while (done < rw && rc == 0) {
		uint32_t seg = done / RB_SEG;
		uint32_t seg_end = (seg + 1) * RB_SEG;
		uint8_t dig[32];
		size_t dl = 0;
		psa_hash_operation_t hs = PSA_HASH_OPERATION_INIT;

		if (seg_end > rw) {
			seg_end = rw;
		}
		if (psa_hash_setup(&hs, PSA_ALG_SHA_256) != PSA_SUCCESS) {
			rc = -EIO;
			break;
		}

		for (uint32_t off = done; off < seg_end; off += sizeof(buf)) {
			uint32_t n = seg_end - off;

			if (n > sizeof(buf)) {
				n = sizeof(buf);
			}
			rc = flash_area_read(fa, img_start + off, buf, n);
			if (rc) {
				break;
			}
			if (psa_hash_update(&hs, buf, n) != PSA_SUCCESS ||
			    psa_hash_update(hop, buf, n) != PSA_SUCCESS) {
				rc = -EIO;
				break;
			}
		}
		if (rc) {
			psa_hash_abort(&hs);
			break;
		}
		if (psa_hash_finish(&hs, dig, sizeof(dig), &dl) != PSA_SUCCESS ||
		    dl != sizeof(dig)) {
			rc = -EIO;
			break;
		}

		if (seg < g_rb.seg_done) {
			/* 完整段：必须与记录逐段一致 */
			if (memcmp(dig, g_rb.seg[seg], sizeof(dig)) != 0) {
				LOG_WRN("resume stale: seg %u digest differs", seg);
				rc = -EBADMSG;
				break;
			}
		}
		done = seg_end;
	}

	flash_area_close(fa);
	return rc;
}

/* 记录有效性（stale 校验），任何一项不符都算无效 */
static bool rb_check(uint32_t total, uint32_t ver, const char *want_sha_hex)
{
	uint8_t want[32];

	if (!g_rb_valid) {
		return false;
	}
	if (g_rb.magic != RB_MAGIC) {
		return false;
	}
	if (g_rb.total != total || g_rb.ver != ver) {
		LOG_INF("resume stale: total/ver changed (%u/%u vs %u/%u)", g_rb.total,
			g_rb.ver, total, ver);
		return false;
	}
	if (!sha_hex_to_bin(want_sha_hex, want) || memcmp(want, g_rb.sha, sizeof(want)) != 0) {
		LOG_INF("resume stale: sha256 changed");
		return false;
	}
	if (g_rb.rw == 0 || g_rb.rw > total) {
		return false;
	}
	if (g_rb.rw % RB_SEG != 0 && g_rb.rw != total) {
		return false;
	}
	if (g_rb.seg_done > RB_MAXSEG) {
		return false;
	}
	return true;
}

/* ---------------- nonce 持久化（防重放） ----------------
 * 用 settings 而不是另开一个 NVS 实例：本工程已经开着 SETTINGS_NVS，
 * 同一条 NVS 再起第二个实例会抢扇区。 */
static int ota_nonce_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const char *next;

	/* 断点续传记录：s3ota/rb（二进制结构，见 struct rb_rec） */
	if (settings_name_steq(name, "rb", &next) && !next) {
		if (len != sizeof(g_rb)) {
			LOG_WRN("resume record size mismatch (%u != %u), ignored",
				(unsigned)len, (unsigned)sizeof(g_rb));
			return -EINVAL;
		}
		ssize_t n = read_cb(cb_arg, &g_rb, len);

		if (n < 0) {
			return (int)n;
		}
		g_rb_valid = true;
		LOG_INF("resume record restored: off=%u seg=%u ver=%u total=%u", g_rb.rw,
			g_rb.seg_done, g_rb.ver, g_rb.total);
		return 0;
	}

	/* 同一个 "s3ota" 子树下还放密钥：s3ota/psk（字符串，最多 47 B） */
	if (settings_name_steq(name, "psk", &next) && !next) {
		if (len == 0 || len >= sizeof(g_psk)) {
			LOG_WRN("psk too long (%u B), keep default", (unsigned)len);
			return -EINVAL;
		}
		ssize_t n = read_cb(cb_arg, g_psk, len);
		if (n < 0) {
			return (int)n;
		}
		g_psk[len] = '\0';
		g_psk_from_nvs = true;
		LOG_INF("psk loaded from NVS (%u B)", (unsigned)len);
		return 0;
	}

	/* 自证窗口的候选标记：s3ota/hv（候选版本）、s3ota/hc（签名那一刻的崩溃计数）。
	 * 断电重启后这两个键还在，重启后的固件才知道"我现在是待验证状态"。 */
	if (settings_name_steq(name, "hv", &next) && !next) {
		ssize_t n = read_cb(cb_arg, &g_cand_ver, len);

		if (n < 0) {
			return (int)n;
		}
		g_cand_valid = true;
		LOG_INF("self-test marker restored: candidate v%u", g_cand_ver);
		return 0;
	}

	if (settings_name_steq(name, "hc", &next) && !next) {
		ssize_t n = read_cb(cb_arg, &g_cand_crash, len);

		return n < 0 ? (int)n : 0;
	}

	if (!settings_name_steq(name, "nonce", &next) || next) {
		return -ENOENT;
	}
	if (len != sizeof(g_nonce_last)) {
		return -EINVAL;
	}
	ssize_t rc = read_cb(cb_arg, &g_nonce_last, sizeof(g_nonce_last));
	if (rc < 0) {
		return (int)rc;
	}

	/* 历史坑：页面曾经把 Date.now() 的**毫秒**当 nonce 发过来，
	 * 1.7e12 在 32 位 strtoul 上直接饱和成 4294967295。一旦存成这个值，
	 * 之后任何"正常"的 nonce 都 `<= last`，远程升级从此永久返 403 replay ——
	 * 板上没有串口就再也升不动了（实测踩到，代价是这一版只能靠 USB 救）。
	 * 判据：饱和值只可能来自"不是秒级时间戳"的输入，直接判脏并归零。 */
	if (g_nonce_last == UINT32_MAX) {
		LOG_WRN("nonce stored as UINT32_MAX (poisoned by ms timestamps), reset to 0");
		g_nonce_last = 0;
		return 0;
	}

	LOG_INF("nonce restored: %u", g_nonce_last);
	return 0;
}

/* 6 个参数：name / subtree / get / set / export / apply（少一个都编不过） */
SETTINGS_STATIC_HANDLER_DEFINE(ota_nonce, "s3ota", NULL, ota_nonce_set, NULL, NULL);

/* 量产换 PSK：先落 NVS 再切内存。顺序不能反过来 —— 写盘失败时内存里保持旧值，
 * 才不会出现"这次能用、重启又变回开发默认"的半套状态。 */
int ota_psk_set(const char *psk)
{
	size_t len;

	if (psk == NULL) {
		return -EINVAL;
	}
	len = strlen(psk);
	if (len == 0U || len >= sizeof(g_psk)) {
		return -EINVAL;
	}

	int rc = settings_save_one("s3ota/psk", psk, len);

	if (rc) {
		return rc;
	}

	memcpy(g_psk, psk, len);
	g_psk[len] = '\0';
	g_psk_from_nvs = true;
	return 0;
}

static void ota_nonce_bump(uint32_t used)
{
	if (used <= g_nonce_last) {
		return;
	}
	g_nonce_last = used;
	int rc = settings_save_one("s3ota/nonce", &g_nonce_last, sizeof(g_nonce_last));
	if (rc) {
		LOG_WRN("nonce persist failed: %d", rc);
	}
}

/* ---------------- 小工具 ---------------- */

static void set_stage(const char *s)
{
	strncpy(g_ota.stage, s, sizeof(g_ota.stage) - 1);
	g_ota.stage[sizeof(g_ota.stage) - 1] = '\0';
}

/* 公网通道的进度打点：只碰快照字段（LED/状态页），不动 g_ota.st，
 * 免得走过这条路径后 fail() 的 "ready 不许被抹" 判断被绕过。 */
void ota_progress_mark(const char *stage, uint32_t written, uint32_t total,
		       uint32_t ver)
{
	set_stage(stage);
	g_ota.written = written;
	g_ota.total = total;
	if (ver > 0) {
		g_ota.ver = ver;
	}
}

void ota_progress_bytes(uint32_t written)
{
	g_ota.written = written;
}

static void fail(int32_t code, const char *why)
{
	/* 重要：门禁类拒绝（没带口令、nonce 重放、版本不够）常常发生在
	 * "上一版镜像已经 ready、只等重启"之后。如果这里无条件把 stage 改成 fail，
	 * 同网段随便一个人发个空请求就能把 ready 抹掉，然后你点重启收到 409。
	 * 所以只有"正在收/正在校验"这种真实进行中的状态才允许翻成 fail；
	 * 其余情况只记录 last_err/err 供页面显示，不动 stage。 */
	int prev = g_ota.st;

	g_ota.last_err = code;
	strncpy(g_ota.err, why, sizeof(g_ota.err) - 1);
	g_ota.err[sizeof(g_ota.err) - 1] = '\0';

	if (prev == OTA_RECV || prev == OTA_VERIFY) {
		g_ota.st = OTA_FAIL;
		set_stage("fail");
	}
	LOG_ERR("ota fail: %s (errno %d)", why, code);
}

/* 头部解析 / 十六进制比较 / 十六进制编码 / 分段算术 全部在 ota_engine 组件里：
 * applications\common\src\ota_engine.c，平台无关，换芯片时原样复用。 */

/* HMAC-SHA256(PSK, msg) → 64 位小写十六进制 */
static int ota_hmac_hex(const char *msg, char out[65])
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_key_id_t key = 0;
	uint8_t mac[32];
	size_t mac_len = 0;

	psa_set_key_type(&attr, PSA_KEY_TYPE_HMAC);
	psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_SIGN_MESSAGE);
	psa_set_key_algorithm(&attr, PSA_ALG_HMAC(PSA_ALG_SHA_256));
	psa_set_key_bits(&attr, strlen(g_psk) * 8);

	psa_status_t ps = psa_import_key(&attr, (const uint8_t *)g_psk, strlen(g_psk), &key);
	psa_reset_key_attributes(&attr);
	if (ps != PSA_SUCCESS) {
		return -EIO;
	}

	ps = psa_mac_compute(key, PSA_ALG_HMAC(PSA_ALG_SHA_256), (const uint8_t *)msg,
			     strlen(msg), mac, sizeof(mac), &mac_len);
	(void)psa_destroy_key(key);

	if (ps != PSA_SUCCESS || mac_len != sizeof(mac)) {
		return -EIO;
	}
	ota_hex_encode(mac, mac_len, out);
	return 0;
}

/* S3 鉴权：规范化串由调用方拼好，这里只比 MAC。
 *
 * 规范化串两端必须逐字一致（skill 里最容易翻车的地方），本工程定为：
 *   upgrade：ver|size|sha256|nonce|force
 *   reboot ：reboot|<nonce>
 * 字段顺序固定、`|` 分隔、十进制无符号、force 归一成 0/1、字符串不 trim。
 *
 * 两种口令入口：
 *   X-Auth：HMAC-SHA256(PSK, 规范化串)，64 位小写 hex —— 脚本走这条
 *   X-Key ：配网页口令原样 —— 页面走这条（http 下 crypto.subtle 不可用，
 *           页面没法现算 HMAC；这条弱一档，写进文档，不假装它等于 HMAC）
 */
bool ota_auth_ok(void *ctx, const char *hdr, const char *canonical)
{
	ARG_UNUSED(ctx);
	char want[65];
	char got[65];

	if (ota_hmac_hex(canonical, want) == 0) {
		if (ota_hdr_get_hex(hdr, "X-Auth", got, sizeof(got)) == 64 && ota_hex_eq(got, want, 64)) {
			return true;
		}
	}

	const char *k = ota_hdr_get(hdr, "X-Key");

	if (k) {
		char tmp[64];
		size_t n = 0;

		while (*k && *k != ' ' && *k != '\r' && *k != '\n' && n + 1 < sizeof(tmp)) {
			tmp[n++] = *k++;
		}
		tmp[n] = '\0';
		const char *pw = ops_auth_pw();

		if (n > 0 && strlen(pw) == n) {
			uint8_t diff = 0;

			for (size_t i = 0; i < n; i++) {
				diff |= (uint8_t)(tmp[i] ^ pw[i]);
			}
			if (diff == 0) {
				return true;
			}
		}
	}

	return false;
}

static void send_all(int sock, const char *s)
{
	size_t len = strlen(s);
	while (len > 0) {
		ssize_t n = zsock_send(sock, s, len, 0);
		if (n <= 0) {
			return;
		}
		s += n;
		len -= (size_t)n;
	}
}

static void reply(int sock, int code, const char *body)
{
	char head[256];

	snprintf(head, sizeof(head),
		 "HTTP/1.1 %d %s\r\n"
		 "Content-Type: application/json\r\n"
		 "Content-Length: %u\r\n"
		 "Connection: close\r\n"
		 "Access-Control-Allow-Origin: *\r\n"
		 "Access-Control-Allow-Headers: *\r\n"
		 "Access-Control-Allow-Methods: POST, OPTIONS\r\n"
		 "\r\n",
		 code, code == 200 ? "OK" : "ERR", (unsigned)strlen(body));
	send_all(sock, head);
	send_all(sock, body);
}

/* 续传握手用的"流式响应"：这时连接不能关，所以不能照抄 reply() 里的
 * Connection: close；额外头用来把 X-Resume-Offset 带回去。 */
static void reply_go(int sock, int code, const char *body, const char *extra)
{
	char head[320];

	snprintf(head, sizeof(head),
		 "HTTP/1.1 %d %s\r\n"
		 "Content-Type: application/json\r\n"
		 "Content-Length: %u\r\n"
		 "%s"
		 "Connection: keep-alive\r\n"
		 "\r\n",
		 code, code == 206 ? "Partial Content" : "OK", (unsigned)strlen(body),
		 extra ? extra : "");
	send_all(sock, head);
	send_all(sock, body);
}

/* ---------------- 升级主流程 ---------------- */

/* ==================== ota_backend：本工程的平台后端 ======================
 *
 * 协议层（applications\common\src\ota_engine.c）决定"什么时候写、写多少、什么时候算完"；
 * 这四个回调回答"写到哪、怎么写、怎么算摘要、怎么判通过"。
 *
 * 换芯片时只需要重写这一块：flash 换成 SPI-NOR/文件、PSA 换成硬件 SHA、
 * mcuboot 换成自家 bootloader —— 而 ota_do_upgrade 的骨架不用动。
 */

/* 整包摘要上下文：旧实现是 ota_do_upgrade 的局部变量，拆成回调后
 * 必须跨 begin/write/finish 三次调用存活，只能提到文件作用域。 */
static psa_hash_operation_t g_hop;
static bool g_hop_live;

struct ota_be_ctx {
	int32_t off;      /* begin 判出来的续传偏移：0 = 全量，>0 = 断点 */
	uint32_t rb_ms;   /* 续传回读校验耗时，最后并进 verify 统计 */
};

static struct ota_be_ctx g_be_ctx;

static struct ota_backend g_be;

/* ---- begin：初始化存储与摘要上下文，并决定"能否续传、从哪续" ---- */
static int32_t be_begin(void *c, const struct ota_req *req)
{
	struct ota_be_ctx *b = c;
	int rc;

	b->off = 0;
	b->rb_ms = 0;
	g_be.err_msg[0] = '\0';
	g_be.err_token = NULL;

	rc = flash_img_init(&g_fctx);
	if (rc) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "flash_img_init failed");
		g_be.err_token = "flash init";
		return rc;
	}

	if (psa_hash_setup(&g_hop, PSA_ALG_SHA_256) != PSA_SUCCESS) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "psa_hash_setup failed");
		g_be.err_token = "crypto init";
		return -EIO;
	}
	g_hop_live = true;

	g_seg_fill = 0;
	g_hseg_live = (psa_hash_setup(&g_hseg, PSA_ALG_SHA_256) == PSA_SUCCESS);
	if (!g_hseg_live) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "segment hash setup failed");
		g_be.err_token = "crypto init";
		g_hop_live = false;
		psa_hash_abort(&g_hop);
		return -EIO;
	}

	if (req->resume) {
		uint32_t off = g_rb.rw;

		if (rb_check(req->total, req->ver, req->sha)) {
			uint64_t tr = k_uptime_get();
			int vrc = rb_readback_verify(&g_hop, off);

			b->rb_ms = (uint32_t)(k_uptime_get() - tr);
			if (vrc == 0) {
				/* 把 stream 指针挪到断点。erased_up_to 必须同步设成同一值：
				 * 它相对 stream.offset，归零会导致下一次写入重新擦除已写好的开头。 */
				g_fctx.stream.bytes_written = off;
				g_fctx.stream.erased_up_to = off;
				g_fctx.stream.buf_bytes = 0;
				g_rb.seg_done = off / RB_SEG;
				g_seg_fill = 0;
				b->off = (int32_t)off;
				LOG_INF("resume accepted: off=%u seg=%u (readback verified, %u ms)",
					off, g_rb.seg_done, b->rb_ms);
			} else {
				LOG_WRN("resume rejected: readback verify rc=%d, fallback to full",
					vrc);
				rb_clear();
			}
		} else {
			LOG_INF("resume requested but record unusable, fallback to full");
			rb_clear();
		}
	} else {
		/* 全新上传：上一版留下的记录与本次无关 */
		rb_clear();
	}

	/* 判据是"最终从 0 开始"（b->off == 0），不是"没要求续传"：
	 * 续传握手失败退回全量时，同样要重建记录 —— 否则会留下一条 0 字节的
	 * 旧记录，下次断线重连仍被判成"记录可用"而去回读校验。 */
	if (b->off == 0) {
		g_rb.magic = RB_MAGIC;
		g_rb.total = req->total;
		g_rb.ver = req->ver;
		g_rb.rw = 0;
		g_rb.seg_done = 0;
		(void)sha_hex_to_bin(req->sha, g_rb.sha);
		/* 关键：记录现在就在 RAM 里生效。g_rb_valid 原本只在 settings 加载时置真，
		 * 若这里不置，"本次运行内断线 → 立刻重连续传"会被判成"无记录"，
		 * 白白退化成全量（实测踩过：握手回 200 resume:0）。 */
		g_rb_valid = true;
	}

	return b->off;
}

/* ---- write：写存储 + 累积整包摘要 + 累积分段摘要 ---- */
static int be_write(void *c, const uint8_t *data, size_t len)
{
	ARG_UNUSED(c);

	int rc = flash_img_buffered_write(&g_fctx, data, len, false);

	if (rc) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "flash write failed");
		g_be.err_token = "flash write";
		return rc;
	}

	if (psa_hash_update(&g_hop, data, len) != PSA_SUCCESS) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "whole hash update failed");
		g_be.err_token = "hash";
		return -EIO;
	}

	rc = seg_feed(data, len);
	if (rc) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "segment hash failed");
		g_be.err_token = "hash";
		return rc;
	}

	g_ota.written += (uint32_t)len;
	return 0;
}

/* ---- finish：刷缓存 → 结束整包摘要 → 比期望摘要 → 回读比对 ---- */
static int be_finish(void *c, const struct ota_req *req)
{
	ARG_UNUSED(c);

	int rc = flash_img_buffered_write(&g_fctx, NULL, 0, true);

	if (rc) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "flash flush failed");
		g_be.err_token = "flush";
		return rc;
	}

	uint8_t digest[32];
	size_t digest_len = 0;

	if (psa_hash_finish(&g_hop, digest, sizeof(digest), &digest_len) != PSA_SUCCESS ||
	    digest_len != sizeof(digest)) {
		g_hop_live = false;
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "psa_hash_finish failed");
		g_be.err_token = "hash";
		return -EIO;
	}
	g_hop_live = false;

	/* S2 闭环：先跟"发起端声明的期望摘要"比，再跟 Flash 回读比。
	 * 顺序是有意的 —— 便宜的先做，而且这一步能拦住"包被换掉"这种
	 * Flash 回读永远发现不了的情况。 */
	char got_sha[65];

	ota_hex_encode(digest, digest_len, got_sha);

	if (!ota_hex_eq(got_sha, req->sha, 64)) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg),
			 "sha256 MISMATCH vs expected X-Sha256");
		g_be.err_token = "sha mismatch";
		return -EBADMSG;
	}
	LOG_INF("expected-sha256 MATCH (%s)", got_sha);

	/* S2：切槽前把整包摘要跟 flash 里实际落下的内容比一遍 */
	struct flash_img_check fic = { .match = digest, .clen = req->total };

	rc = flash_img_check(&g_fctx, &fic, flash_img_get_upload_slot());
	if (rc) {
		snprintf(g_be.err_msg, sizeof(g_be.err_msg), "%s",
			 rc == -EBADMSG ? "whole-image sha256 MISMATCH" : "flash check failed");
		g_be.err_token = "sha mismatch";
		return rc;
	}
	LOG_INF("whole-sha256 MATCH (%u bytes)", req->total);

	return 0;
}

/* ---- abort：释放摘要上下文（断链/失败/放弃时都要调） ---- */
static void be_abort(void *c)
{
	ARG_UNUSED(c);

	if (g_hop_live) {
		psa_hash_abort(&g_hop);
		g_hop_live = false;
	}
	if (g_hseg_live) {
		psa_hash_abort(&g_hseg);
		g_hseg_live = false;
	}
}

/* 后端表：四个回调 + 一块上下文。这里故意用普通 struct 而不是 const，
 * 因为失败时后端要往 err_msg / err_token 里写字。 */
static struct ota_backend g_be = {
	.begin = be_begin,
	.write = be_write,
	.finish = be_finish,
	.abort = be_abort,
	.ctx = &g_be_ctx,
};

/* ---- 收数据：把 socket 读成"引擎要的一份" ---- */
static int sock_recv_cb(void *ctx, uint8_t *buf, size_t want, size_t *got)
{
	int fd = *(int *)ctx;
	ssize_t n = zsock_recv(fd, buf, want, 0);

	if (n < 0) {
		return -errno;
	}
	*got = (size_t)n;
	return 0;
}

/* 门禁结果 → errno（日志里要看到"为什么拒"，但不能只有一个 -EINVAL） */
static int32_t check_errno(enum ota_check c)
{
	switch (c) {
	case OTA_CHECK_AUTH:
	case OTA_CHECK_REPLAY:
		return -EACCES;
	case OTA_CHECK_TOO_BIG:
		return -EFBIG;
	case OTA_CHECK_OLD_VER:
		return -EPERM;
	default:
		return -EINVAL;
	}
}

/* 失败应答：HTTP 码与 JSON token 都由协议层给，这里只负责拼一句话 */
static void reply_fail(int sock, const struct ota_sess *s)
{
	char body[96];

	snprintf(body, sizeof(body), "{\"ok\":false,\"err\":\"%s\"}",
		 s->token ? s->token : "fail");
	reply(sock, s->http_code ? s->http_code : 500, body);
}

/* ============================ 升级主流程 ==============================
 *
 * 这个函数现在只剩三段：门禁 → 收 body → 收尾切槽。
 * 每一段的具体算法都在引擎里；它自己不再解析头部、不再管块大小、
 * 不再算"还剩多少字节"，只处理"谁在这个平台上落地"。 */
static void ota_do_upgrade(int sock, const char *hdr, const uint8_t *pre, size_t pre_len)
{
	struct ota_req req;
	char why[96];

	/* 门禁全在协议层：头齐全 → 带期望摘要 → 鉴权 → 体积 → 防重放 → 版本 */
	enum ota_check chk = ota_req_check(hdr, OTA_LOCAL_VERSION, OTA_SLOT_MAX_BYTES,
					   g_nonce_last, ota_auth_ok, NULL, &req, why,
					   sizeof(why));

	if (chk != OTA_CHECK_OK) {
		char body[96];

		fail(check_errno(chk), why);
		snprintf(body, sizeof(body), "{\"ok\":false,\"err\":\"%s\"}",
			 ota_check_token(chk));
		reply(sock, ota_check_http(chk), body);
		return;
	}

	g_ota.st = OTA_RECV;
	g_ota.total = req.total;
	g_ota.written = 0;
	g_ota.ver = req.ver;
	g_ota.last_err = 0;
	g_ota.err[0] = '\0';
	set_stage("recv");

	/* 后端开包：初始化 flash / 摘要 / 断点记录，并决定"能不能续传、从哪续" */
	int32_t prep = g_be.begin(g_be.ctx, &req);

	if (prep < 0) {
		fail(prep, g_be.err_msg);
		reply(sock, 500, "{\"ok\":false,\"err\":\"flash init\"}");
		return;
	}

	struct ota_sess sess;

	ota_sess_init(&sess, &req);
	if (prep > 0) {
		sess.written = (uint32_t)prep;
		sess.start_off = (uint32_t)prep;
		g_ota.written = (uint32_t)prep;
	}

	/* 续传握手：发起端在等这个答复，回了它才开始发 body（或从 0 发全量） */
	if (req.resume) {
		if (prep > 0) {
			char extra[48];

			snprintf(extra, sizeof(extra), "X-Resume-Offset: %d\r\n", prep);
			reply_go(sock, 206, "{\"ok\":true,\"resume\":1}", extra);
		} else {
			reply_go(sock, 200, "{\"ok\":true,\"resume\":0}", NULL);
		}
	}

	LOG_INF("ota start: ver=%u size=%u nonce=%u force=%d resumed=%d off=%u", req.ver,
		req.total, req.nonce, req.force, prep > 0 ? 1 : 0,
		(uint32_t)(prep > 0 ? prep : 0));

	uint64_t t0 = k_uptime_get();
	/* skill 要求：续传时吞吐的分母用"本次实际传输的增量字节数" */
	int rc = ota_sess_run(&sess, &g_be, sock_recv_cb, &sock, pre, pre_len);

	if (rc) {
		fail(sess.err ? sess.err : rc, sess.err_msg);
		reply_fail(sock, &sess);
		goto out;
	}

	g_ota.t_dl = (uint32_t)(k_uptime_get() - t0);
	g_ota.written = sess.written;

	/* ---- S0 打点：必须在切槽之前输出 ---- */
	{
		uint32_t thr = 0;
		uint32_t nb = req.total - sess.start_off;

		if (g_ota.t_dl > 0) {
			thr = (uint32_t)(((uint64_t)nb * 1000) / (g_ota.t_dl * 1024));
		}
		LOG_INF("timing dl=%u verify=- total=- throughput=%uKB/s size=%u resumed=%d off=%u",
			g_ota.t_dl, thr, req.total, prep > 0 ? 1 : 0, sess.start_off);
	}

	g_ota.st = OTA_VERIFY;
	set_stage("verify");

	uint64_t tv = k_uptime_get();
	rc = ota_sess_finish(&sess, &g_be, &req);
	/* 续传的回读重算也属于校验开销，必须计进 verify，否则会低估成本 */
	g_ota.t_verify = (uint32_t)(k_uptime_get() - tv + g_be_ctx.rb_ms);

	if (rc) {
		fail(sess.err ? sess.err : rc, sess.err_msg);
		reply_fail(sock, &sess);
		goto out;
	}

	g_ota.t_total = (uint32_t)(k_uptime_get() - t0 + g_be_ctx.rb_ms);
	{
		uint32_t thr = 0;
		uint32_t nb = req.total - sess.start_off;

		if (g_ota.t_total > 0) {
			thr = (uint32_t)(((uint64_t)nb * 1000) / (g_ota.t_total * 1024));
		}
		LOG_INF("timing dl=%u verify=%u total=%u throughput=%uKB/s size=%u resumed=%d off=%u",
			g_ota.t_dl, g_ota.t_verify, g_ota.t_total, thr, req.total,
			prep > 0 ? 1 : 0, sess.start_off);
	}

	/* ---- S5：请求切槽 ----
	 *
	 * 这里只能用 PERMANENT，不能用 TEST + 自证。
	 * 原因不是偷懒，是 ESP32 的硬约束：
	 *   - mcuboot 的"待确认"状态靠往 **当前运行槽末尾** 的 trailer 写 image_ok 位;
	 *   - ESP32 是 XIP 执行，app 就在 slot0 里跑，写 slot0 自己的 trailer 会返回 -EIO
	 *     （实测 boot_write_img_confirmed() 稳定返回 -5）;
	 *   - 所以 TEST 模式的镜像**永远转不了正**，每次重启都会回滚，等于刷了个一次性的固件。
	 *
	 * 代价要说清：没有"新固件崩了自动回退"这条能力了。回退只能靠"再 OTA 一版旧固件"。
	 * 这条限制写进了文档 18-OTA与远程运维 的「未实现项」，不假装有回滚。
	 */
	rc = boot_request_upgrade(BOOT_UPGRADE_PERMANENT);
	if (rc) {
		fail(rc, "boot_request_upgrade failed");
		reply(sock, 500, "{\"ok\":false,\"err\":\"request upgrade\"}");
		goto out;
	}

	ota_nonce_bump(req.nonce);
	/* 升级标记已置、镜像已校验：断点记录到此为止，再留着会污染下一次判断 */
	rb_clear();

	/* 关键：在"已经要求 mcuboot 切槽"之后立刻记下候选版本与崩溃基线。
	 * 重启后新固件读到这个标记才知道自己处于自证期；
	 * 若窗口内崩了 / 链路起不来，就用 boot_request_upgrade 把 slot1 的旧镜像搬回来。 */
	ota_arm_candidate((uint32_t)g_ota.ver, ops_boot_crash_count());

	g_ota.st = OTA_READY;
	set_stage("ready");
	reply(sock, 200, "{\"ok\":true,\"reboot\":false}");
	LOG_INF("ota ready: slot1 written + verified, upgrade flag set");
	return;

out:
	ota_sess_abort(&sess, &g_be);
}

/* ---------------- HTTP 解析入口 ---------------- */

static void handle_one(int sock)
{
	uint8_t hdr[OTA_HDR_MAX];
	size_t hl = 0;
	int end = -1;

	/* 收包超时：这个服务是单线程的，客户端掉线/挂起时不能无限阻塞。
	 * 超时按"传输中断"处理 —— 段边界上的断点记录已经落盘，下次可续传。 */
	struct timeval tv = { .tv_sec = 15, .tv_usec = 0 };

	zsock_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

	while (hl < sizeof(hdr) - 1) {
		ssize_t n = zsock_recv(sock, hdr + hl, sizeof(hdr) - 1 - hl, 0);
		if (n <= 0) {
			return;
		}
		hl += (size_t)n;
		end = ota_find_hdr_end(hdr, hl);
		if (end >= 0) {
			break;
		}
	}
	if (end < 0) {
		reply(sock, 400, "{\"ok\":false,\"err\":\"bad request\"}");
		return;
	}

	/* 转成 C 字符串，方便用 strstr；头后面那截 body 单独保留 */
	size_t pre_len = hl - (size_t)end - 4;
	uint8_t pre[OTA_HDR_MAX];
	memcpy(pre, hdr + end + 4, pre_len);

	char h[OTA_HDR_MAX];
	size_t copy_len = (size_t)end < sizeof(h) - 1 ? (size_t)end : sizeof(h) - 1;
	memcpy(h, hdr, copy_len);
	h[copy_len] = '\0';

	/* 浏览器跨端口 + 自定义头一定会先发预检 */
	if (strncmp(h, "OPTIONS", 7) == 0) {
		reply(sock, 200, "{\"ok\":true}");
		return;
	}

	/* 重启生效入口：镜像已落盘且升级标记已置，才允许重启。
	 * 刻意把它做成显式的第二个请求，而不是上传完自动重启。 */
	if (strncmp(h, "POST ", 5) == 0 && strstr(h, " /api/ota/reboot ") != NULL) {
		/* 重启也要口令：否则同网段任何人随时能把设备重启掉（DoS 也算攻击面）。
		 * 规范化串用 reboot|<nonce>；这里不判 nonce 递增——重放一个重启请求
		 * 最多让设备多重启一次，没有升级语义，不值得为它消耗 nonce 配额。 */
		char canonical[64];
		uint32_t rn = ota_hdr_get_u32(h, "X-Nonce", 0);

		if (rn == 0) {
			fail(-EINVAL, "reboot: missing X-Nonce");
			reply(sock, 401, "{\"ok\":false,\"err\":\"noauth\"}");
			return;
		}
		snprintf(canonical, sizeof(canonical), "reboot|%u", rn);
		if (!ota_auth_ok(NULL, h, canonical)) {
			fail(-EACCES, "reboot auth failed");
			reply(sock, 401, "{\"ok\":false,\"err\":\"badauth\"}");
			return;
		}

		if (g_ota.st != OTA_READY) {
			reply(sock, 409, "{\"ok\":false,\"err\":\"no image staged\"}");
			return;
		}
		reply(sock, 200, "{\"ok\":true}");
		LOG_INF("reboot requested via /api/ota/reboot");
		/* 主动重启算"干净退出"：不清这个标志，下次启动就会被记成一次崩溃，
		 * 崩溃计数立刻变成噪音，再也没法用它判断"是不是真崩过"。 */
		ops_boot_mark_clean();
		k_sleep(K_MSEC(1200));
		sys_reboot(SYS_REBOOT_COLD);
		return;
	}

	if (strncmp(h, "POST ", 5) != 0 || strstr(h, " /ota ") == NULL) {
		reply(sock, 404, "{\"ok\":false,\"err\":\"not found\"}");
		return;
	}

	ota_do_upgrade(sock, h, pre, pre_len);
}

/* ---------------- 线程 ---------------- */

/* 这个栈必须给足。实测踩过：handle_one() 里 hdr/pre/h 三个 OTA_HDR_MAX(1536) 缓冲
 * 就吃掉 4.6 KB，再叠上 ota_do_upgrade() 的 2 KB 收块缓冲和 PSA 哈希上下文，
 * 8 KB 栈在"传输完 → 回读校验"这一步直接溢出，现象是整块板子失去响应。
 * S3 有 512 KB SRAM，24 KB 这点代价换确定性，值。 */
K_THREAD_STACK_DEFINE(ota_stack, 24576);
static struct k_thread ota_tcb;

static void ota_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	int srv = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (srv < 0) {
		LOG_ERR("ota socket: %d", errno);
		return;
	}
	int one = 1;
	zsock_setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_port = htons(OTA_PORT),
		.sin_addr.s_addr = INADDR_ANY,
	};
	if (zsock_bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		LOG_ERR("ota bind :%d failed: %d", OTA_PORT, errno);
		zsock_close(srv);
		return;
	}
	if (zsock_listen(srv, 2) < 0) {
		LOG_ERR("ota listen failed: %d", errno);
		zsock_close(srv);
		return;
	}
	LOG_INF("ota server listening on :%d (proto v%d, local ver %d)", OTA_PORT,
		OTA_PROTO_VER, OTA_LOCAL_VERSION);

	while (1) {
		struct sockaddr_in peer;
		socklen_t plen = sizeof(peer);

		int cli = zsock_accept(srv, (struct sockaddr *)&peer, &plen);
		if (cli < 0) {
			k_sleep(K_MSEC(200));
			continue;
		}
		/* OTA 属于开发者能力，只在局域网侧开放。
		 * 判据与 :80 一致：源地址落在 AP 网段 192.168.4.0/24 就是"连在热点上的手机"，
		 * 直接关连接 —— 它连"这个端口存在"都不该知道。
		 * 注意这里拒绝的是连接，不是请求，所以不会有半截固件写进备用槽。 */
		if (peer.sin_family == AF_INET) {
			struct in_addr ap;
			char pb[NET_IPV4_ADDR_LEN];

			net_addr_pton(AF_INET, OTA_AP_SIDE_IP, &ap);
			net_addr_ntop(AF_INET, &peer.sin_addr, pb, sizeof(pb));

			if ((ntohl(peer.sin_addr.s_addr) & 0xFFFFFF00u) ==
			    (ntohl(ap.s_addr) & 0xFFFFFF00u)) {
				LOG_WRN("ota: reject %s (AP side, developer only)", pb);
				zsock_close(cli);
				continue;
			}
		}
		handle_one(cli);
		zsock_close(cli);
	}
}

int ota_srv_start(void)
{
	g_ota.st = OTA_IDLE;
	strcpy(g_ota.stage, "idle");

	/* PSA 要先 init 才能开哈希/开 MAC 会话；这个调用是幂等的 */
	(void)psa_crypto_init();

	/* 口令来源要显式打出来：跑的是编译期默认值时必须能一眼看见，
	 * 否则"开发默认能开箱用"会被误当成"量产也这样就行"。 */
	LOG_INF("ota auth: psk source=%s, portal-key fallback enabled",
		g_psk_from_nvs ? "nvs(s3ota/psk)" : "BUILTIN DEV DEFAULT");

	/* 打印一次镜像状态，便于确认"这一版是从 slot0 还是 slot1 起来的" */
	LOG_INF("image_ok=%d (PERMANENT upgrade mode, no self-confirm window)",
		boot_is_img_confirmed() ? 1 : 0);

	k_thread_create(&ota_tcb, ota_stack, K_THREAD_STACK_SIZEOF(ota_stack), ota_thread,
			NULL, NULL, NULL, K_PRIO_PREEMPT(7), 0, K_NO_WAIT);
	k_thread_name_set(&ota_tcb, "ota_srv");
	return 0;
}

const char *ota_snapshot_json(void)
{
	static char buf[256];
	uint32_t pct = 0;

	if (g_ota.total > 0) {
		pct = (uint32_t)(((uint64_t)g_ota.written * 100) / g_ota.total);
	}

	snprintf(buf, sizeof(buf),
		 "\"ota\":{\"stage\":\"%s\",\"pct\":%u,\"written\":%u,\"total\":%u,"
		 "\"ver\":%u,\"local_ver\":%d,\"pending\":%s,\"err\":\"%s\","
		 "\"cand\":%u,\"win\":%s,\"win_s\":%u}",
		 g_ota.stage, pct, g_ota.written, g_ota.total, g_ota.ver, OTA_LOCAL_VERSION,
		 ota_pending_verify() ? "true" : "false", g_ota.err,
		 ota_candidate_ver(), g_win_started ? "true" : "false",
		 g_win_started ? (g_win_ticks * OTA_WIN_TICK_MS) / 1000u : 0u);
	return buf;
}

bool ota_pending_verify(void)
{
	/* 恒 false：本板走 PERMANENT 语义，不存在"待确认"这个状态。
	 * 直接透传 boot_is_img_confirmed() 会一直返回 false（trailer 写不进），
	 * 页面就会永远显示"待确认"，反而误导人。 */
	return false;
}

/* ------------------ S5：自证窗口 + 应用侧回退（MOVE 模式才成立） ------------------
 *
 * 为什么现在能做了：切到 SWAP_USING_MOVE 之后，swap 来回都是"镜像在槽首"的同一布局，
 * slot1 里那份旧镜像是**合法可读**的升级镜像（实测 rc=0、ver=上一版），
 * 对它 boot_request_upgrade() 打 pending 会被 bootloader 真的搬回去（实测 local_ver 119→118）。
 * OFFSET 模式下做不到：旧镜像落在 +0 而升级槽要求 +0x1000，打 pending 会被判无效并整槽擦除。
 *
 * 边界（不要吹成"防砖"）：这是应用侧回退。候选固件要是连启动都起不来，
 * 窗口代码根本没机会跑 —— 那种情况仍然只能 USB 重刷。 */

void ota_arm_candidate(uint32_t ver, uint32_t crash_now)
{
	g_cand_ver = ver;
	g_cand_crash = crash_now;
	g_cand_valid = true;
	g_win_started = false;
	g_win_ticks = 0;
	settings_save_one("s3ota/hv", &g_cand_ver, sizeof(g_cand_ver));
	settings_save_one("s3ota/hc", &g_cand_crash, sizeof(g_cand_crash));
	LOG_INF("self-test armed: candidate v%u (crash baseline %u)", ver, crash_now);
}

uint32_t ota_candidate_ver(void)
{
	return g_cand_valid ? g_cand_ver : 0u;
}

uint32_t ota_candidate_crash(void)
{
	return g_cand_crash;
}

void ota_commit_candidate(void)
{
	if (!g_cand_valid) {
		return;
	}
	g_cand_valid = false;
	/* 窗口状态一起清掉：转正之后 /api/status 不该再显示 win=true（实测踩过，
	 * cand 归 0 了但 win 一直是 true，看着像窗口还在跑）。 */
	g_win_started = false;
	g_win_ticks = 0;
	settings_delete("s3ota/hv");
	settings_delete("s3ota/hc");
}

int ota_revert_to_secondary(const char *why)
{
	int rc = boot_request_upgrade(BOOT_UPGRADE_PERMANENT);

	if (rc) {
		LOG_ERR("revert(%s): boot_request_upgrade rc=%d", why, rc);
		return -EIO;
	}

	LOG_WRN("revert(%s): pending on slot1 (swap=%d), rebooting into previous image",
		why, (int)mcuboot_swap_type());

	/* 标记作废 + 主动重启算干净退出：否则下次启动会把这次重启记成一次崩溃，
	 * 崩溃计数被自己的回退动作污染，以后就没法拿它判"是不是真崩过"。 */
	g_cand_valid = false;
	settings_delete("s3ota/hv");
	settings_delete("s3ota/hc");
	ops_boot_mark_clean();

	k_sleep(K_MSEC(800));
	sys_reboot(SYS_REBOOT_COLD);
	return 0;
}

void ota_confirm_if_healthy(bool link_ok)
{
	if (!g_cand_valid) {
		return;
	}

#if OTA_SELFTEST_FORCE_FAIL
	/* 故障注入：假装链路一直没起来（见文件头部 OTA_SELFTEST_FORCE_FAIL 的说明） */
	link_ok = false;
#endif

	if (g_cand_ver != (uint32_t)OTA_LOCAL_VERSION) {
		/* 正在跑的不是候选版本：说明已经回退过（或被人换了镜像），标记作废 */
		LOG_INF("self-test: running v%d != candidate v%u, drop marker",
			(int)OTA_LOCAL_VERSION, g_cand_ver);
		ota_commit_candidate();
		return;
	}

	uint32_t crashes = ops_boot_crash_count();

	if (crashes > g_cand_crash) {
		LOG_ERR("self-test: candidate crashed (crash %u > baseline %u)", crashes, g_cand_crash);
		(void)ota_revert_to_secondary("candidate crashed");
		return;
	}

	/* 窗口内被**拔电**：崩溃计数不会涨（poweron 不算异常复位），
	 * 而窗口起点又只存在 RAM 里 —— 掉电重启会把窗口从头再走一遍，链路一通就转正，
	 * 等于"掉电"这件事永远不会触发回退。这条判据把它补上：
	 * 候选已 armed（说明上次是带着"待自证"重启的），但本次启动上来就是 dirty，
	 * 说明上一轮没走任何正常退出路径 —— 只有拔电/硬复位会这样。
	 * 只在 poweron / brownout / pwr_glitch 上判，软重启不算（那是干净的）。 */
	if (!ops_boot_last_clean()) {
		const char *rst = ops_boot_rst_reason();

		if (strcmp(rst, "poweron") == 0 || strcmp(rst, "brownout") == 0 ||
		    strcmp(rst, "pwr_glitch") == 0) {
			LOG_ERR("self-test: power cut in window (rst=%s) -> revert", rst);
			(void)ota_revert_to_secondary("power cut in window");
			return;
		}
	}

	if (!g_win_started) {
		g_win_started = true;
		g_win_ticks = 0;
		LOG_INF("self-test: window %u s, candidate v%u", (unsigned)(OTA_SELFTEST_MS / 1000),
			g_cand_ver);
	}

	g_win_ticks++;

	if (g_win_ticks * OTA_WIN_TICK_MS < OTA_SELFTEST_MS) {
		return; /* 窗口没走完，继续观察 */
	}

	if (link_ok) {
		ota_commit_candidate();
		LOG_INF("self-test OK: v%d confirmed", (int)OTA_LOCAL_VERSION);
	} else {
		LOG_ERR("self-test failed: link down after %u s", (unsigned)(OTA_SELFTEST_MS / 1000));
		(void)ota_revert_to_secondary("link down in window");
	}
}


/* ---------------- 公网通道（ota_cloud）复用的导出原语 ----------------
 *
 * 设计：公网 HTTPS 拉取与局域网 HTTP 上传共享同一套写槽后端与门禁，
 * 不许任何一条通道绕过鉴权/防重放/版本门禁另写一份。 */

const struct ota_backend *ota_backend_get(void)
{
	return &g_be;
}

uint32_t ota_nonce_get(void)
{
	return g_nonce_last;
}

int ota_arm_upgrade(uint32_t ver, uint32_t nonce)
{
	int rc = boot_request_upgrade(BOOT_UPGRADE_PERMANENT);

	if (rc) {
		LOG_ERR("arm_upgrade: boot_request_upgrade failed (%d)", rc);
		return rc;
	}

	ota_nonce_bump(nonce);
	/* 升级标记已置、镜像已校验：断点记录到此为止，再留着会污染下一次判断 */
	rb_clear();
	/* 在"已经要求 mcuboot 切槽"之后立刻记下候选版本与崩溃基线（自证窗口） */
	ota_arm_candidate(ver, ops_boot_crash_count());
	LOG_INF("arm_upgrade: slot1 pending, ver=%u nonce=%u (PERMANENT)", ver, nonce);
	return 0;
}

bool ota_lan_idle(void)
{
	return g_ota.st == OTA_IDLE || g_ota.st == OTA_FAIL || g_ota.st == OTA_READY;
}

void ota_resume_invalidate(void)
{
	rb_clear();
}
