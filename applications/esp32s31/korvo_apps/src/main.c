/*
 * Korvo-1 应用层：板载 RGB（WS2812 @ GPIO37）动画引擎
 *
 * 分层：
 *   应用层（本文件）  模式状态机 / 帧计算 / 亮度 / 功放联动 / 帧率统计
 *   驱动层            led_strip_update_rgb()
 *   外设层            pulse_io + RMT 硬件时序
 *
 * shell 命令：app rgb / app bright / app pa / app stats
 */

#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/led_strip.h>
#include <zephyr/logging/log.h>

#include "store.h"
#include "breath_sync.h"

LOG_MODULE_REGISTER(app, LOG_LEVEL_INF);

/* NVS 键：模式 / 基色 / 亮度 / 功放，一次性记住上次的状态。
 * 用 20-23 这段独立 id，避免和 korvo_nvs 演示用的 10 号键撞车——
 * 同一块板的不同工程共用同一个 storage 分区，键空间必须自己规划。 */
#define K_MODE    20u
#define K_RGB     21u
#define K_BRIGHT  22u
#define K_PA      23u

#define STRIP_NODE DT_ALIAS(led_strip)
#define PA_NODE    DT_ALIAS(pa_ctrl)
#define KEY_NODE   DT_ALIAS(sw0)

/*
 * 同一份应用代码要能在两种板上编译：
 *   Korvo-1         有 WS2812（GPIO37）和 PA_CTRL（GPIO7），四键走 IO42 的 ADC 分压梯
 *   ESP32-S3 最小板  有 WS2812（GPIO48）和 KEY（GPIO45，普通 GPIO 按键），没有功放使能脚
 * 用 dts 别名做"能力探测"：板级 overlay 给了就有，没给就整段自动裁掉。
 * 这是跨芯片迁移的核心手法——能力由硬件描述决定，而不是靠 #ifdef CONFIG_xxx 猜。
 */
#define HAS_STRIP DT_HAS_ALIAS(led_strip)
#define HAS_PA    DT_HAS_ALIAS(pa_ctrl)

/*
 * 按键这里有个坑：板级 dts 把 `sw0` 别名指到了**子节点 button0**（那是"某个键"，
 * 不是一个驱动实例）。gpio-keys 的驱动实例是它的**父节点**（/buttons）。
 * 所以判断"有没有按键设备"要用 compatible 找节点，而不是看 sw0 别名：
 *   DT_HAS_COMPAT_STATUS_OKAY(gpio_keys)          —— 有没有一个 enabled 的 gpio-keys
 *   DT_COMPAT_GET_ANY_STATUS_OKAY(gpio_keys)      —— 拿到那个节点
 * 引脚规格仍然从 sw0 别名取（别名的作用本来就是"帮我找到这个键"）。
 */
#define HAS_KEY   DT_HAS_COMPAT_STATUS_OKAY(gpio_keys)

#if HAS_STRIP
static const struct device *const strip = DEVICE_DT_GET(STRIP_NODE);
#endif

#if HAS_PA
static const struct gpio_dt_spec pa = GPIO_DT_SPEC_GET(PA_NODE, gpios);
#endif

#if HAS_KEY
#include <zephyr/input/input.h>
#include <zephyr/dt-bindings/input/input-event-codes.h>   /* INPUT_KEY_0 等键码 */

#define KEY_DEV_NODE DT_COMPAT_GET_ANY_STATUS_OKAY(gpio_keys)

static const struct device *const key_dev = DEVICE_DT_GET(KEY_DEV_NODE);
static const struct gpio_dt_spec key_gpio = GPIO_DT_SPEC_GET(KEY_NODE, gpios);
#endif

/* ------------------------------------------------------------ 应用状态 */
enum rgb_mode {
	RGB_OFF,
	RGB_SOLID,
	RGB_BREATH,
	RGB_RAINBOW,
	RGB_STATUS,
};

static const char *mode_name[] = {"off", "solid", "breath", "rainbow", "status"};

static volatile enum rgb_mode g_mode = RGB_STATUS;
static volatile struct led_rgb g_base = {.r = 0x00, .g = 0x20, .b = 0x40};
static volatile int g_bright = 60;          /* 0..100 */
static volatile int g_pa;                   /* 功放当前状态 */
static volatile uint32_t g_frames;          /* 已经刷出去的帧数 */
static volatile uint32_t g_last_ms;         /* 上一帧的耗时 */
static volatile uint8_t g_last_rgb[3];

/*
 * 规范 v1.0 的 UTC 估计器（docs\参考\同步呼吸灯规则.md）。
 *
 * 呼吸的亮度从此只由「绝对时间」推导，和“本机开机多久”“这是第几帧”都无关：
 *   相位 φ = (UTC - epoch 0) mod 4 s
 *   亮度   = LUT[φ×256/4s]                     ← 256 项余弦表 + gamma 2.0
 * 所以两台设备只要时间对得上，灯就同相，不需要互相通信。
 *
 * 本板没有网络、也没有 RTC，所以只能自由运行：上电时刻当 epoch 0，
 * 此时 synced=0、一致性等级 0，亮度会被半亮守卫压一半（“没对上表”要看得出来）。
 * 用 `app sync <epoch秒>` 人工对一次表，就并到全局时间上并恢复满亮。
 */
static struct breath_clock g_clk;

/* --------------------------------------------------- 持久化：存 / 取 / 清 */
static void cfg_save(void)
{
	uint8_t rgb[3] = {g_base.r, g_base.g, g_base.b};

	store_set_u32(K_MODE, (uint32_t)g_mode);
	store_set_blob(K_RGB, rgb, sizeof(rgb));
	store_set_u32(K_BRIGHT, (uint32_t)g_bright);
	store_set_u32(K_PA, (uint32_t)g_pa);
}

static void cfg_load(void)
{
	uint32_t v;
	uint8_t rgb[3];
	size_t n = 0;

	if (store_get_u32(K_MODE, &v) == 0 && v <= RGB_STATUS) {
		g_mode = (enum rgb_mode)v;
	}
	if (store_get_blob(K_RGB, rgb, sizeof(rgb), &n) == 0 && n == 3) {
		g_base.r = rgb[0];
		g_base.g = rgb[1];
		g_base.b = rgb[2];
	}
	if (store_get_u32(K_BRIGHT, &v) == 0 && v <= 100) {
		g_bright = (int)v;
	}
	if (store_get_u32(K_PA, &v) == 0) {
		g_pa = v ? 1 : 0;
	}
}

static void cfg_clear(void)
{
	store_del(K_MODE);
	store_del(K_RGB);
	store_del(K_BRIGHT);
	store_del(K_PA);
}

/* ------------------------------------------------------------ 颜色换算 */
/* HSV -> RGB，整数实现，h: 0..360，s/v: 0..100 */
static void hsv2rgb(int h, int s, int v, struct led_rgb *out)
{
	int c = (v * s) / 100;
	int x = c * (60 - abs((h % 120) - 60)) / 60;
	int m = v - c;

	switch (h / 60) {
	case 0:  out->r = c + m; out->g = x + m; out->b = m;      break;
	case 1:  out->r = x + m; out->g = c + m; out->b = m;      break;
	case 2:  out->r = m;     out->g = c + m; out->b = x + m;  break;
	case 3:  out->r = m;     out->g = x + m; out->b = c + m;  break;
	case 4:  out->r = x + m; out->g = m;     out->b = c + m;  break;
	default: out->r = c + m; out->g = m;     out->b = x + m;  break;
	}
}

/* 按百分比压亮度（0..100） */
static struct led_rgb scale(const struct led_rgb *c, int pct)
{
	struct led_rgb out = {
		.r = (uint8_t)((c->r * pct) / 100),
		.g = (uint8_t)((c->g * pct) / 100),
		.b = (uint8_t)((c->b * pct) / 100),
	};

	return out;
}

/* -------------------------------------------------------------- 帧计算 */
/*
 * 时间参数有两个，分工不同：
 *   utc_us  ——「世界现在几点」，一切相位/颜色轮转都由它决定（多台设备一致）
 *   mono_us ——「本机开机多久」，只用来算“这口时间放了多久”的误差预算，不进相位
 *
 * 原来这里只有一个 now_ms = k_uptime_get_32()，那是本机时长：
 * 两台设备上电时刻不同就永远差一截，重启还会归零（规范第 1 节）。
 */
static struct led_rgb compute_frame(int64_t utc_us, int64_t mono_us)
{
	struct led_rgb px = {0};
	int env = 100;          /* 包络：心跳用（呼吸直接走 LUT，不经过这一层） */
	uint64_t now = (utc_us > 0) ? (uint64_t)utc_us : 0ull;

	switch (g_mode) {
	case RGB_OFF:
		return px;

	case RGB_SOLID:
		px = g_base;
		break;

	case RGB_BREATH: {
		/* 呼吸走规范：余弦 LUT + gamma 2.0，4 s 一周期，纯 UTC 驱动。
		 * 返回的已经是 0..255 的通道值，所以这里不能再叠一次 scale()，
		 * 否则会做两次取整，和别的设备差 1 个码。 */
		struct breath_rgb base = {.r = g_base.r, .g = g_base.g, .b = g_base.b};
		struct breath_rgb out;
		uint8_t lvl = breath_clock_level(&g_clk, mono_us);
		uint8_t gain = breath_gain_guard((uint8_t)g_bright, lvl);

		breath_frame(now, &base, gain, &out);

		px.r = out.r;
		px.g = out.g;
		px.b = out.b;
		return px;
	}

	case RGB_RAINBOW: {
		/* 3.6 s 转一圈：色相本质也是「时间的函数」，换成 UTC 后多台设备连彩虹同步 */
		int64_t s = (int64_t)(now / 1000000ull);
		int hue = (int)(((s % 3600) + 3600) % 3600 * 360 / 3600);

		hsv2rgb(hue, 100, 100, &px);
		break;
	}

	case RGB_STATUS: {
		/* 心跳：亮 200 ms、灭 200 ms、亮 200 ms、然后长灭 1400 ms。
		 * 这里同样用 UTC 而不是 uptime：多台设备的心跳会落在同一拍上，
		 * 白拿一个“凭心跳对表”的观察手段。 */
		int64_t ms = (int64_t)(now / 1000ull);
		int64_t p = ((ms % 2000) + 2000) % 2000;

		env = ((p < 200) || (p >= 400 && p < 600)) ? 100 : 0;
		px.r = 0x00;
		px.g = 0x10;
		px.b = 0x60;
		break;
	}
	}

	/* 包络 × 全局亮度 */
	return scale(&px, env * g_bright / 100);
}

/* ------------------------------------------------------------ 动画线程 */
#define RGB_STACK_SIZE   2048
#define RGB_PRIORITY     7

K_THREAD_STACK_DEFINE(rgb_stack, RGB_STACK_SIZE);
static struct k_thread rgb_thread_data;

static void rgb_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	const uint32_t period = 40;      /* 25 fps */

#if !HAS_STRIP
	/* 本板没有 led_strip 别名：线程保留但不碰硬件，app stats 照样能看帧节奏 */
	while (1) {
		k_msleep(period);
	}
#else
	while (1) {
		/* 每帧重算，不留任何累加状态：
		 * 掉帧、卡顿、重启之后，下一帧自动回到正确相位（规范 §8.3） */
		int64_t mono = (int64_t)k_uptime_get() * 1000;       /* us */
		int64_t utc = breath_clock_now_us(&g_clk, mono);
		uint32_t t0 = k_uptime_get_32();
		struct led_rgb px = compute_frame(utc, mono);
		int ret = led_strip_update_rgb(strip, &px, 1);

		if (ret == 0) {
			g_frames++;
			g_last_ms = k_uptime_get_32() - t0;
			g_last_rgb[0] = px.r;
			g_last_rgb[1] = px.g;
			g_last_rgb[2] = px.b;
		}
		k_msleep(period);
	}
#endif
}

/* ------------------------------------------------------------ 按键（GPIO） */
#if HAS_KEY
/*
 * 硬件：KEY 接 IO45，按下接地（GPIO_ACTIVE_LOW + 内部上拉），松开为高。
 * 软件：走 Zephyr 的 input 子系统（gpio-keys 驱动 + INPUT_CALLBACK_DEFINE）。
 *       驱动在中断里只做"延时复查"（debounce-interval-ms），
 *       真正的 input_report 发生在系统工作队列里，所以我们这里不能阻塞。
 *
 * 分工：回调只负责"入队"；按键线程负责业务（切模式 / 切亮度 / 写 NVS）。
 * 这样做的好处：NVS 写可能耗时几十 ms，不会卡住系统工作队列。
 */
struct btn_evt {
	uint32_t ms;
	bool pressed;
};

K_MSGQ_DEFINE(btn_q, sizeof(struct btn_evt), 16, 4);

K_THREAD_STACK_DEFINE(btn_stack, 1024);
static struct k_thread btn_thread_data;

static volatile uint32_t g_btn_presses;
static volatile uint32_t g_btn_short;
static volatile uint32_t g_btn_long;

static void btn_cb(struct input_event *evt, void *user_data)
{
	ARG_UNUSED(user_data);

	if (evt->type != INPUT_EV_KEY) {
		return;
	}

	struct btn_evt e = {
		.ms = k_uptime_get_32(),
		.pressed = evt->value != 0,
	};

	k_msgq_put(&btn_q, &e, K_NO_WAIT);
}

INPUT_CALLBACK_DEFINE(DEVICE_DT_GET(KEY_DEV_NODE), btn_cb, NULL);

static void btn_thread(void *a, void *b, void *c)
{
	static const int levels[] = {15, 30, 60, 100};
	struct btn_evt e;
	uint32_t press_ms = 0;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (1) {
		if (k_msgq_get(&btn_q, &e, K_FOREVER) != 0) {
			continue;
		}

		if (e.pressed) {
			press_ms = e.ms;
			g_btn_presses++;
			continue;      /* 按下先记账，等松开再判断长短 */
		}

		uint32_t dur = e.ms - press_ms;

		if (dur < 80) {
			continue;      /* 电气抖动，丢掉 */
		}

		if (dur < 800) {
			/* 短按：模式轮转 */
			g_btn_short++;
			g_mode = (enum rgb_mode)((g_mode + 1) % (RGB_STATUS + 1));
			printk("[btn] 短按 %u ms -> 模式=%s\n", dur, mode_name[g_mode]);
		} else {
			/* 长按：亮度档 15 -> 30 -> 60 -> 100 -> 15 */
			g_btn_long++;
			int idx = -1;

			for (size_t i = 0; i < ARRAY_SIZE(levels); i++) {
				if (g_bright == levels[i]) {
					idx = (int)i;
				}
			}
			idx = (idx + 1) % (int)ARRAY_SIZE(levels);
			g_bright = levels[idx];
			printk("[btn] 长按 %u ms -> 亮度=%d%%\n", dur, g_bright);
		}

		cfg_save();        /* 按键改的也是配置，和 shell 命令一样要落盘 */
	}
}
#endif

/* ------------------------------------------------- 功能键：IO42 ADC 分压梯 */
static const struct device *const adc0_dev = DEVICE_DT_GET(DT_NODELABEL(adc0));

/*
 * ADC 的"分辨率"是 SoC 属性，不是应用常量：
 *   ESP32-S31  17 位（驱动接受 17）
 *   ESP32-S3   12 位（硬要 17 位会得到 <err> adc_esp32: unsupported resolution (17)）
 * 不去猜芯片名/不靠 CONFIG，直接问驱动：从 17 往下降，第一个读成功的就记住。
 * 这是和 HAS_STRIP 同样的思路——能力靠探测，不靠假设。
 */
static uint8_t g_adc_res;

static int adc_probe_read(const struct device *dev, uint8_t ch, int32_t *raw)
{
	static const uint8_t cand[] = {17, 16, 15, 14, 13, 12};
	struct adc_sequence seq = {
		.buffer = raw,
		.buffer_size = sizeof(*raw),
		.channels = BIT(ch),
	};

	if (g_adc_res != 0) {
		seq.resolution = g_adc_res;
		return adc_read(dev, &seq);
	}

	for (size_t i = 0; i < ARRAY_SIZE(cand); i++) {
		seq.resolution = cand[i];
		if (adc_read(dev, &seq) == 0) {
			g_adc_res = cand[i];
			return 0;
		}
	}
	return -ENOTSUP;
}

/* 折算回 17 位尺度：按键窗口是按 Korvo-1 的 17 位读数标定的 */
static int32_t adc_raw_to_17bit(int32_t raw)
{
	return (g_adc_res == 0 || g_adc_res == 17) ? raw : (raw << (17 - g_adc_res));
}

/*
 * Korvo-1 V1.1 四个功能键共用一个网络：R77 10K 上拉到 3V3，四键分压进 IO42 = ADC1_CH0。
 * 实测电压（ESP-IDF 侧标定值，见 display_audio_photo/docs/项目规划/改动记录.md 8.2）：
 *   SET    ~260 mV      PLAY  ~759 mV
 *   VOL-  ~1237 mV      VOL+  ~1647 mV
 * 本板 Zephyr adc 读数为 17 位原始值，与 mV 的实测比例约 1.10（288/260、1362/1237、1811/1647）。
 * 所以把 mV 窗口 ×1.10 换成原始值窗口；窗口沿用工程里验证过的 ±100 mV。
 */
struct key_window {
	const char *name;
	int32_t raw_lo;
	int32_t raw_hi;
};

static const struct key_window key_win[] = {
	{"SET",   180 * 110 / 100, 360 * 110 / 100},
	{"PLAY",  660 * 110 / 100, 860 * 110 / 100},
	{"VOL-", 1140 * 110 / 100, 1340 * 110 / 100},
	{"VOL+", 1550 * 110 / 100, 1750 * 110 / 100},
};

static const struct led_rgb key_color[] = {
	{.r = 0x30, .g = 0x00, .b = 0x00},   /* SET  红 */
	{.r = 0x00, .g = 0x30, .b = 0x00},   /* PLAY 绿 */
	{.r = 0x00, .g = 0x00, .b = 0x30},   /* VOL- 蓝 */
	{.r = 0x30, .g = 0x20, .b = 0x00},   /* VOL+ 黄 */
};

static int key_index(int32_t raw)
{
	for (int i = 0; i < ARRAY_SIZE(key_win); i++) {
		if (raw >= key_win[i].raw_lo && raw <= key_win[i].raw_hi) {
			return i;
		}
	}
	return -1;
}

static int read_io42(int32_t *out)
{
	return adc_probe_read(adc0_dev, 0, out);
}

static int cmd_key(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 40;
	int last = -2;

	if (n <= 0 || n > 500) {
		n = 40;
	}

	struct adc_channel_cfg cfg = {
		.gain = ADC_GAIN_1,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME_DEFAULT,
		.channel_id = 0,
	};

	adc_channel_setup(adc0_dev, &cfg);
	shell_print(sh, "[key] 监听 %d 次（每 100 ms），按键时 RGB 会亮起对应颜色", n);

	for (int i = 0; i < n; i++) {
		int32_t raw = 0;
		int k = -1;

		if (read_io42(&raw) == 0) {
			k = key_index(adc_raw_to_17bit(raw));
		}

		if (k != last) {
			last = k;
			shell_print(sh, "[key] raw=%d（%u bit） -> %s", raw, g_adc_res,
				    (k < 0) ? "松开" : key_win[k].name);

			if (k < 0) {
				g_mode = RGB_OFF;
			} else {
				g_base = key_color[k];
				g_mode = RGB_SOLID;
			}
			cfg_save();
		}
		k_msleep(100);
	}
	return 0;
}

/* ------------------------------------------------------------ ADC 扫通道 */
#if DT_NODE_HAS_STATUS(DT_NODELABEL(adc1), okay)
static const struct device *const adc1_dev = DEVICE_DT_GET(DT_NODELABEL(adc1));
#endif

static void adc_row(const struct shell *sh, const struct device *dev, const char *tag)
{
	int32_t raw = 0;

	if (!device_is_ready(dev)) {
		shell_print(sh, "%s: not ready", tag);
		return;
	}

	shell_fprintf(sh, SHELL_NORMAL, "%s [t=%ums]:", tag, k_uptime_get_32());

	for (int ch = 0; ch < 8; ch++) {
		struct adc_channel_cfg cfg = {
			.gain = ADC_GAIN_1,
			.reference = ADC_REF_INTERNAL,
			.acquisition_time = ADC_ACQ_TIME_DEFAULT,
			.channel_id = ch,
		};

		if (adc_channel_setup(dev, &cfg) != 0) {
			shell_fprintf(sh, SHELL_NORMAL, " err");
			continue;
		}

		if (adc_probe_read(dev, ch, &raw) != 0) {
			shell_fprintf(sh, SHELL_NORMAL, " err");
			continue;
		}
		shell_fprintf(sh, SHELL_NORMAL, " %5d", raw);
	}
	shell_print(sh, "");
}

static int cmd_adc(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 10;

	if (n <= 0 || n > 200) {
		n = 10;
	}

	/* 先读一次把分辨率探出来，好把这行打印完整 */
	int32_t probe = 0;
	struct adc_channel_cfg cfg = {
		.gain = ADC_GAIN_1,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME_DEFAULT,
		.channel_id = 0,
	};

	adc_channel_setup(adc0_dev, &cfg);
	adc_probe_read(adc0_dev, 0, &probe);

	shell_print(sh, "[adc] 采 %d 次（每次 200 ms），ADC 分辨率=%u 位", n, g_adc_res);
	for (int i = 0; i < n; i++) {
		adc_row(sh, adc0_dev, "adc0(u1)");
#if DT_NODE_HAS_STATUS(DT_NODELABEL(adc1), okay)
		adc_row(sh, adc1_dev, "adc1(u2)");
#endif
		k_msleep(200);
	}
	return 0;
}

/* ---------------------------------------------------------------- shell */
static int cmd_rgb(const struct shell *sh, size_t argc, char **argv)
{
	int ret = 0;

	if (argc < 2) {
		shell_print(sh, "用法: app rgb <off|status|rainbow|solid r g b|breath r g b>");
		return -EINVAL;
	}

	if (!strcmp(argv[1], "off")) {
		g_mode = RGB_OFF;
	} else if (!strcmp(argv[1], "status")) {
		g_mode = RGB_STATUS;
	} else if (!strcmp(argv[1], "rainbow")) {
		g_mode = RGB_RAINBOW;
	} else if (!strcmp(argv[1], "solid") || !strcmp(argv[1], "breath")) {
		if (argc < 5) {
			shell_error(sh, "%s 需要 r g b 三个值", argv[1]);
			return -EINVAL;
		}
		g_base.r = (uint8_t)strtoul(argv[2], NULL, 0);
		g_base.g = (uint8_t)strtoul(argv[3], NULL, 0);
		g_base.b = (uint8_t)strtoul(argv[4], NULL, 0);
		g_mode = strcmp(argv[1], "solid") ? RGB_BREATH : RGB_SOLID;
	} else {
		shell_error(sh, "没有模式 %s", argv[1]);
		return -EINVAL;
	}

	cfg_save();

	shell_print(sh, "[rgb] 模式 = %s, 基色 = %02x %02x %02x, 亮度 = %d%%, 返回 = %d（已落盘）",
		    mode_name[g_mode], g_base.r, g_base.g, g_base.b, g_bright, ret);
	return 0;
}

static int cmd_bright(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_error(sh, "用法: app bright <0-100>");
		return -EINVAL;
	}

	int v = atoi(argv[1]);

	if (v < 0) {
		v = 0;
	}
	if (v > 100) {
		v = 100;
	}
	g_bright = v;
	cfg_save();
	shell_print(sh, "[bright] 亮度 = %d%%（下一帧生效，已落盘）", g_bright);
	return 0;
}

static int cmd_pa(const struct shell *sh, size_t argc, char **argv)
{
#if !HAS_PA
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "[pa] 本板 dts 里没有 pa_ctrl 别名（S3 最小板没有功放使能脚），此命令不生效");
	return -ENOTSUP;
#else
	if (argc < 2) {
		shell_error(sh, "用法: app pa <on|off>");
		return -EINVAL;
	}

	int on = !strcmp(argv[1], "on");
	int ret = gpio_pin_set_dt(&pa, on);

	g_pa = on ? 1 : 0;

	shell_print(sh, "[pa] PA_CTRL(GPIO%d) = %d, 返回 = %s", pa.pin, on, ret == 0 ? "0" : "?");

	if (on) {
		g_mode = RGB_SOLID;             /* 开功放时给个常亮提示 */
		g_base.r = 0x08;
		g_base.g = 0x20;
		g_base.b = 0x08;
	}
	cfg_save();
	return 0;
#endif
}

/* ------------------------------------------------------- 时间与同步 */
/*
 * 本板没有网络也没有 RTC，能用的授时通道只剩「人工对表」这一条：
 *     app sync 1735689600
 * 对应规范 §6.2 表里的 MANUAL 一档（不确定度约 200 ms → 一致性等级 2，
 * 并排看已经看不出“慢半拍”）。对一次之后设备就并到全局时间上。
 *
 * 没对表时是等级 0：灯照常呼吸，但被半亮守卫压一半 —— 这不是 bug，
 * 是让“没对上表”在视觉上一眼可辨（规范 §6.5）。
 */
static int cmd_sync(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_print(sh, "用法: app sync <epoch_seconds>");
		shell_print(sh, "  例: app sync 1735689600   （= 2025-01-01T00:00:00Z，相位 0）");
		shell_print(sh, "  epoch 秒用 PC 上 `date +%%s` 取，±200 ms 以内都算对上");
		return -EINVAL;
	}

	/* 用 strtoul 而不是 strtoll：和 cmd_rgb 用同一套 libc 函数，肯定编得过；
	 * 32 位 long 能表示到 2106 年，够了。 */
	int64_t sec = (int64_t)strtoul(argv[1], NULL, 0);
	int64_t mono = (int64_t)k_uptime_get() * 1000;
	int r = breath_clock_observe_src(&g_clk, mono, sec * 1000000,
					 BREATH_SRC_MANUAL, 0);

	shell_print(sh, "[sync] 授时 %u s（人工对表，±200 ms）→ 处理=%d（1=直接吸收 2=缓释中 3=跳变）",
		    (unsigned)sec, r);
	shell_print(sh, "[sync] 等级=%u/3  当前相位=%u/256  预算=%d ms",
		    (unsigned)breath_clock_level(&g_clk, mono),
		    (unsigned)breath_phase((uint64_t)(sec * 1000000)),
		    (int)(breath_clock_error_budget_us(&g_clk, mono) / 1000));
	return 0;
}

static int cmd_time(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	int64_t mono = (int64_t)k_uptime_get() * 1000;
	int64_t utc = breath_clock_now_us(&g_clk, mono);
	uint8_t lvl = breath_clock_level(&g_clk, mono);
	uint8_t phase = breath_phase((uint64_t)utc);
	uint8_t env = breath_envelope((uint64_t)utc);

	shell_print(sh, "[time] utc=%u s  相位=%u/256  亮度=%u/255",
		    (unsigned)(utc / 1000000), (unsigned)phase, (unsigned)env);

	if (lvl == 0) {
		shell_print(sh, "[time] 等级 0（未授时）：自由运行 + 半亮守卫。用 app sync <epoch秒> 对表");
	} else {
		int64_t bud = breath_clock_error_budget_us(&g_clk, mono);

		shell_print(sh, "[time] 等级 %u/3（3=≤25ms 严格同相）  预算=%u ms  频率偏差=%d ppm",
			    (unsigned)lvl, (unsigned)(bud / 1000),
			    (int)breath_clock_rate_ppm(&g_clk));
	}

	shell_print(sh, "[time] 两台上各敲一次：相位相同、utc 差 <25 ms 即为同相（规范 §10.3）");
	return 0;
}

static int cmd_stats(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[stats] 模式=%s 亮度=%d%% 帧数=%u 单帧耗时=%u ms 最后颜色=%02x %02x %02x",
		    mode_name[g_mode], g_bright, g_frames, g_last_ms,
		    g_last_rgb[0], g_last_rgb[1], g_last_rgb[2]);
	return 0;
}

/* ------------------------------------------------------- 持久化命令 */
static int cmd_saved(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t v;
	uint8_t rgb[3];
	size_t n = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[saved] ---- NVS 里存的内容 ----");
	if (store_get_u32(K_MODE, &v) == 0 && v <= RGB_STATUS) {
		shell_print(sh, "  模式   = %s (%u)", mode_name[v], v);
	} else {
		shell_print(sh, "  模式   = 未存");
	}
	if (store_get_blob(K_RGB, rgb, sizeof(rgb), &n) == 0 && n == 3) {
		shell_print(sh, "  基色   = %02x %02x %02x", rgb[0], rgb[1], rgb[2]);
	} else {
		shell_print(sh, "  基色   = 未存");
	}
	if (store_get_u32(K_BRIGHT, &v) == 0) {
		shell_print(sh, "  亮度   = %u%%", v);
	} else {
		shell_print(sh, "  亮度   = 未存");
	}
	if (store_get_u32(K_PA, &v) == 0) {
		shell_print(sh, "  功放   = %s", v ? "on" : "off");
	} else {
		shell_print(sh, "  功放   = 未存");
	}
	if (store_get_u32(STORE_ID_BOOT_CNT, &v) == 0) {
		shell_print(sh, "  开机次数 = %u", v);
	}
	shell_print(sh, "[saved] ---------------------------");
	return 0;
}

static int cmd_clear(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	cfg_clear();
	shell_print(sh, "[clear] 已清空 NVS 里的模式/基色/亮度/功放（下次上电回到默认）");
	return 0;
}

static int cmd_btn(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

#if HAS_KEY
	int level = gpio_pin_get_dt(&key_gpio);       /* 逻辑电平（已考虑 ACTIVE_LOW） */
	int raw = gpio_pin_get_raw(key_gpio.port, key_gpio.pin);   /* 物理电平 */

	shell_print(sh, "[btn] 设备=%s 脚=%s", key_dev->name, key_gpio.port->name);
	shell_print(sh, "[btn] 逻辑电平=%d 物理电平=%d（本板空闲实测=物理 0，所以 0=松开 / 1=按下）",
		    level, raw);
	shell_print(sh, "[btn] 按下=%u 次，短按=%u 次（切模式），长按=%u 次（切亮度 15/30/60/100）",
		    g_btn_presses, g_btn_short, g_btn_long);
	return 0;
#else
	shell_print(sh, "[btn] 本板没有 sw0 别名（没有 GPIO 按键），此命令不生效");
	return -ENOTSUP;
#endif
}

#if HAS_KEY
/*
 * 自测命令：直接往 input 子系统"注入"一次按下/松开，不碰 GPIO。
 * 用途：验证 回调 → 消息队列 → 业务线程 → 计数/切模式/写 NVS 这条软件链路。
 * 注意：它绕过 gpio-keys 驱动，所以"电气+去抖"这一段不在覆盖范围内。
 */
static int cmd_btnsim(const struct shell *sh, size_t argc, char **argv)
{
	int ms = (argc > 1) ? atoi(argv[1]) : 300;

	if (ms < 0 || ms > 5000) {
		ms = 300;
	}

	input_report_key(key_dev, INPUT_KEY_0, 1, false, K_NO_WAIT);
	k_msleep(ms);
	input_report_key(key_dev, INPUT_KEY_0, 0, false, K_NO_WAIT);

	shell_print(sh, "[btn] 已注入一次 %d ms 按键事件（只走 input 软件链路，不碰 GPIO）", ms);
	return 0;
}
#endif

SHELL_STATIC_SUBCMD_SET_CREATE(sub_app,
	SHELL_CMD(rgb, NULL, "RGB 模式：off/status/rainbow/solid/breath", cmd_rgb),
	SHELL_CMD(bright, NULL, "全局亮度 0-100", cmd_bright),
	SHELL_CMD(pa, NULL, "功放 PA_CTRL on/off", cmd_pa),
	SHELL_CMD(adc, NULL, "扫两个 ADC 单元的 8 个通道（找功能键）", cmd_adc),
	SHELL_CMD(key, NULL, "IO42 四键识别 + RGB 反馈", cmd_key),
	SHELL_CMD(btn, NULL, "GPIO 按键：状态 + 短按切模式 / 长按切亮度", cmd_btn),
#if HAS_KEY
	SHELL_CMD(btnsim, NULL, "注入一次按键事件（只测 input 软件链路，不碰 GPIO）", cmd_btnsim),
#endif
	SHELL_CMD(stats, NULL, "帧率与当前状态", cmd_stats),
	SHELL_CMD(sync, NULL, "人工授时：app sync <epoch秒>（对表后多台设备同相）", cmd_sync),
	SHELL_CMD(time, NULL, "查 UTC / 相位 / 一致性等级 / 误差预算", cmd_time),
	SHELL_CMD(saved, NULL, "查看 NVS 里记住的配置", cmd_saved),
	SHELL_CMD(clear, NULL, "清空 NVS 配置", cmd_clear),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(app, &sub_app, "Korvo-1 应用示例", NULL);

int main(void)
{
	uint32_t boot = 0;
	int ret = 0;
	int64_t mono0;

	/* 呼吸用的 UTC 估计器必须在 rgb_thread 起来之前建好，否则第一帧读到的是全零结构。
	 * 本板没有网络也没有 RTC → 先自由运行：上电时刻当 epoch 0，synced=0（等级 0、半亮）。
	 * 之后用 `app sync <epoch秒>` 对一次表，就并到全局时间上并恢复满亮。 */
	mono0 = (int64_t)k_uptime_get() * 1000;
	breath_clock_init_freerun(&g_clk, mono0);

#if HAS_STRIP
	if (!device_is_ready(strip)) {
		LOG_ERR("led_strip 未就绪");
		return -ENODEV;
	}
#endif

	/* 先恢复上次的配置，再起动画线程，这样第一帧就是"上次的样子" */
	if (store_init() == 0) {
		cfg_load();
		boot = store_boot_count_bump();
	} else {
		LOG_WRN("NVS 不可用，本次运行不记忆状态");
	}

#if HAS_PA
	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
	gpio_pin_set_dt(&pa, g_pa);         /* 把存档里的功放状态也恢复 */
#endif

#if HAS_STRIP
	LOG_INF("led_strip=%s length=%d", strip->name, led_strip_length(strip));
#endif
#if HAS_KEY
	if (!device_is_ready(key_dev)) {
		LOG_ERR("按键设备未就绪");
	}
#endif
#if HAS_PA
	LOG_INF("PA_CTRL=GPIO%d 配置返回 %d", pa.pin, ret);
#else
	ARG_UNUSED(ret);
#endif

	/* 用 printk 而不是 LOG_INF：printk 同步直出，日志后端是异步线程，
	 * 开机这一行必须让用户一定看到 */
#if HAS_PA
	printk("[boot] 恢复配置：模式=%s 基色=%02x %02x %02x 亮度=%d%% PA=%d 第 %u 次开机\n",
	       mode_name[g_mode], g_base.r, g_base.g, g_base.b, g_bright, g_pa, boot);
#else
	printk("[boot] 恢复配置：模式=%s 基色=%02x %02x %02x 亮度=%d%% 第 %u 次开机（本板无 PA_CTRL）\n",
	       mode_name[g_mode], g_base.r, g_base.g, g_base.b, g_bright, boot);
#endif

	k_thread_create(&rgb_thread_data, rgb_stack, K_THREAD_STACK_SIZEOF(rgb_stack),
			rgb_thread, NULL, NULL, NULL, RGB_PRIORITY, 0, K_NO_WAIT);
#if HAS_KEY
	k_thread_create(&btn_thread_data, btn_stack, K_THREAD_STACK_SIZEOF(btn_stack),
			btn_thread, NULL, NULL, NULL, RGB_PRIORITY + 1, 0, K_NO_WAIT);
#endif

	printk("\n=== Korvo apps（同一份代码，%s）===\n", CONFIG_BOARD_TARGET);
	printk("[breath] 规范 v1.0：4 s/周期，亮度=UTC 的纯函数（LUT 校验和 0x51521FF6）\n");
	printk("[breath] 当前未授时（等级 0，灯半亮）；对表：app sync <epoch秒>\n");
	printk("app rgb status|rainbow|off\n");
	printk("app rgb solid 20 0 0\n");
	printk("app rgb breath 0 0 60\n");
	printk("app bright 30\n");
	printk("app pa on|off\n");
#if HAS_KEY
	printk("app btn（按键：短按切模式 / 长按切亮度）\n");
#endif
	printk("app stats | app saved | app clear\n");
	return 0;
}
