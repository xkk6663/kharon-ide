/*
 * korvo_periph —— 应用层七项的实操台（ESP32-S31 / Korvo-1 V1.1）
 *
 * 分层（和课程第三、四课一致的叫法）：
 *   应用层   本文件：shell 命令 + 业务逻辑，只认 dts 别名和标准驱动 API
 *   驱动层   adc / led_strip / pwm / i2c / can —— 全是 Zephyr 自带，不写一行驱动
 *   板级     boards\esp32s31_function_coreboard_esp32s31_hpcore.overlay
 *            （公共差异从 applications\common\korvo_board.dtsi / korvo_i2c.dtsi 借）
 *   SoC 层   zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi（一个字节都不动）
 *
 * 命令一览：
 *   korvo info                所有设备 ready 状态
 *   korvo adc [n]             打印 adc0 8 个通道的原始读数
 *   korvo keys [n]            监听 IO42 四键（原始值按"量程比例"归类，不写死 mV）
 *   korvo led <r> <g> <b>     WS2812 单色
 *   korvo ledcycle            六色循环
 *   korvo pwm <0..100>        LEDC0 通道 0 的占空比（1 kHz）
 *   korvo pwmsweep            0→100→0 扫一遍
 *   korvo can                 内部回环：发一帧再自己收回来
 *   korvo i2c                 扫 I2C0 总线（板载 ES8389 在 0x10 / 0x14）
 */

#include <stdlib.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/can.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/led_strip.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/printk.h>

/* 同步呼吸灯规范 v1.0（applications\common）：纯函数 + UTC 估计器，只依赖 stdint.h */
#include "breath_sync.h"

/* ---------------------------------------------------------------- 设备句柄 */
static const struct device *const strip_dev = DEVICE_DT_GET(DT_ALIAS(led_strip));
static const struct device *const adc_dev = DEVICE_DT_GET(DT_NODELABEL(adc0));
static const struct device *const pwm_dev = DEVICE_DT_GET(DT_NODELABEL(ledc0));
static const struct device *const i2c_dev = DEVICE_DT_GET(DT_NODELABEL(i2c0));
static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));

/*
 * 呼吸灯规范 v1.0 的 UTC 估计器。
 * 本板没有网络也没有 RTC，授时通道只剩「人工对表」：korvo synctime <epoch秒>。
 * 没对表时是等级 0，亮度被半亮守卫压一半（规范 §6.5 的诚实提示）。
 */
static struct breath_clock g_breath_clk;

/* --------------------------------------------------------------- ADC 基础 */
/*
 * S31 的 SAR ADC 只有 17 位（SOC_ADC_DIGI_MIN_BITWIDTH == MAX == 17），
 * 但驱动/ HAL 在不同版本上可能给别的位宽，所以按"从高往低试"探测一次，
 * 和 korvo_apps 里的做法一致——能力靠探测，不靠猜。
 */
static uint8_t g_adc_res;

/*
 * ④ 关键坑：ADC 的 pad 必须显式切成"模拟"，否则读到恒定 0
 *
 * Zephyr 的 adc_esp32 驱动在 chan setup 时会调
 *     adc_channel_io_map[unit][channel]  ->  gpio_pin_configure_dt(pad, GPIO_DISCONNECTED)
 * 但 S31 的 dts 里 `adc0 { unit = <1>; }` 是一基编号（ADC1/ADC2），
 * 而这张 io_map 表是零基（第 0 行=ADC1、第 1 行=ADC2）。
 * 于是"配 adc0 的 ch0"实际把 ADC2_CH0(IO50) 的 pad 切成了模拟，
 * 真正要采的 IO42 还停在数字模式 —— 采样永远是 0，按键怎么按都不动。
 * （2026-11-14 实机现象：korvo keys 一直 raw=0，ch1..7 是浮空的小值）
 *
 * 应用层能做的、且不改上游的修法：用公开的 GPIO API 把 IO42 自己切成
 * GPIO_DISCONNECTED（这正是驱动干 ADC pad 用的同一个动作），再做 adc_read。
 */
static const struct gpio_dt_spec key_pad = {
	.port = DEVICE_DT_GET(DT_NODELABEL(gpio1)),
	.pin = 10,                /* IO42 = 42 % 32，落在 gpio1 的第 10 脚 */
	.dt_flags = 0,
};

static void key_pad_to_analog(void)
{
	static bool done;

	if (done) {
		return;
	}
	done = true;
	(void)gpio_pin_configure_dt(&key_pad, GPIO_DISCONNECTED);
}

static int adc_setup_ch(const struct device *dev, uint8_t ch)
{
	struct adc_channel_cfg cfg = {
		.gain = ADC_GAIN_1,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME_DEFAULT,
		.channel_id = ch,
	};

	if (ch == 0) {
		key_pad_to_analog();
	}

	return adc_channel_setup(dev, &cfg);
}

static int adc_read_raw(const struct device *dev, uint8_t ch, int32_t *raw)
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

/* ------------------------------------------------------- ① IO42 四个功能键 */
/*
 * 为什么不用 dts 里的 adc-keys 驱动：
 *   adc-keys 的判断条件是"mV 阈值"，它内部调 adc_raw_to_millivolts_dt()，
 *   这条换算依赖 io-channels 子节点上的 zephyr,resolution / zephyr,vref-mv，
 *   而 S31 的 adc0/adc1 在 SoC dtsi 里没有任何 channel 子节点（只有 unit /
 *   channel-count），且 SOC_ADC_ATTEN_NUM == 1（只有 0 dB 一档）。
 *   换算出来的 mV 与实际电压不在一个尺度上，阈值就没法定。
 *
 * 所以这里自己做判断：把 17 位原始值按"满量程 = 3.3 V"折算成 mV，再和
 * 实测的四档电压做最近匹配。容差给到 ±180 mV，键与键之间最小间隔 500 mV，
 * 换算误差（±10%）也翻不了车。
 *
 * 电压来源（Korvo-1 V1.1 实测）：松开 0 mV、SET 260、PLAY 759、VOL- 1237、VOL+ 1647。
 * "松开 = 0 V"很反直觉——这条梯子是"按键把不同分压点接到 ADC 脚"，不按时被下拉到 0。
 * 用"松开≈满量程"那套常见接法去判，会一直误报第一个键。这个坑写进 06 文档。
 */
struct korvo_key {
	const char *name;
	int32_t mv;
};

static const struct korvo_key g_keys[] = {
	{"SET",   260},
	{"PLAY",  759},
	{"VOL-", 1237},
	{"VOL+", 1647},
};

#define KEY_RELEASE_MV 150    /* 低于这个电压算松开 */
#define KEY_TOL_MV     180    /* 每个键的容差（窗口半宽） */
#define ADC_FSR_MV     3300   /* S31 只有一档衰减，满量程约 3.3 V */

static int32_t raw_to_mv(int32_t raw)
{
	uint32_t full = (g_adc_res >= 1 && g_adc_res <= 17) ? (1u << g_adc_res) : (1u << 17);

	return (int32_t)((int64_t)raw * ADC_FSR_MV / full);
}

/* 返回 -1 = 松开，0..3 = 第几个键 */
static int key_pick(int32_t mv)
{
	int best = -1;
	int32_t best_diff = INT32_MAX;

	if (mv < KEY_RELEASE_MV) {
		return -1;
	}

	for (int i = 0; i < (int)ARRAY_SIZE(g_keys); i++) {
		int32_t d = (mv > g_keys[i].mv) ? (mv - g_keys[i].mv) : (g_keys[i].mv - mv);

		if (d < best_diff) {
			best_diff = d;
			best = i;
		}
	}

	return (best_diff <= KEY_TOL_MV) ? best : -1;
}

/* ------------------------------------------------ 按键通道体检（哪一路才是按键） */
/*
 * 现象线索：korvo keys 里 raw 一直 0，按键不动。要么通道号不是 ch0，
 * 要么那个 pad 根本没进模拟模式。这条命令不猜，直接把 8 路的最小/最大值打出来：
 * 你在窗口期内轮流按 SET/PLAY/VOL-/VOL+，哪一路 delta 变大，哪一路才是按键梯。
 * ch 与 IO 的对应（S31 ADC1）：ch0=IO42 ch1=43 ch2=44 ch3=45 ch4=46 ch5=47 ch6=48 ch7=49
 */
static int cmd_keyscan(const struct shell *sh, size_t argc, char **argv)
{
	int sec = (argc > 1) ? atoi(argv[1]) : 6;
	int32_t mn[8];
	int32_t mx[8];

	if (sec < 2 || sec > 30) {
		sec = 6;
	}
	for (int c = 0; c < 8; c++) {
		mn[c] = 0x7FFFFFFF;
		mx[c] = -0x7FFFFFFF;
	}

	shell_print(sh, "[keyscan] %d 秒窗口，每 30 ms 采 8 路；现在请轮流按 SET / PLAY / VOL- / VOL+", sec);

	int loops = sec * 1000 / 30;

	for (int i = 0; i < loops; i++) {
		for (uint8_t ch = 0; ch < 8; ch++) {
			int32_t raw = 0;

			if (adc_setup_ch(adc_dev, ch) == 0 && adc_read_raw(adc_dev, ch, &raw) == 0) {
				mn[ch] = MIN(mn[ch], raw);
				mx[ch] = MAX(mx[ch], raw);
			}
		}
		k_msleep(30);
	}

	shell_print(sh, "[keyscan] res=%u bit，结果：", g_adc_res);
	for (int c = 0; c < 8; c++) {
		int32_t delta = mx[c] - mn[c];

		shell_print(sh, "  ch%d min=%d max=%d delta=%d%s", c, mn[c], mx[c], delta,
			    (delta > 200) ? "   <== 有变化，这一路才是按键" : "");
	}
	shell_print(sh, "[keyscan] 全 8 路 delta 都 <200 的话，就是 pad 没进模拟模式（驱动层问题）");
	return 0;
}

static int cmd_keys(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 40;
	int last = -2;
	int32_t warm = 0;

	if (n <= 0 || n > 500) {
		n = 40;
	}

	/* 先读一次：让位宽探测和通道 setup 先完成，否则表头会打 res=0 */
	(void)adc_setup_ch(adc_dev, 0);
	(void)adc_read_raw(adc_dev, 0, &warm);

	shell_print(sh, "[keys] res=%u bit，监听 %d 次（每 100 ms），按下/松开才打印", g_adc_res, n);

	for (int i = 0; i < n; i++) {
		int32_t raw = 0;
		int32_t mv;
		int k = -1;

		if (adc_setup_ch(adc_dev, 0) == 0 && adc_read_raw(adc_dev, 0, &raw) == 0) {
			mv = raw_to_mv(raw);
			k = key_pick(mv);
		} else {
			mv = -1;
		}

		if (k != last) {
			last = k;
			shell_print(sh, "[keys] raw=%d -> %d mV -> %s", raw, mv,
				    (k < 0) ? "松开" : g_keys[k].name);
		}
		k_msleep(100);
	}
	return 0;
}

static int cmd_adc(const struct shell *sh, size_t argc, char **argv)
{
	int n = (argc > 1) ? atoi(argv[1]) : 5;

	if (n <= 0 || n > 100) {
		n = 5;
	}

	shell_print(sh, "[adc] adc0(u1) 8 通道，res=%u bit（ch0 = IO42 = 四键电阻梯）", g_adc_res);

	for (int i = 0; i < n; i++) {
		shell_fprintf(sh, SHELL_NORMAL, "[adc] t=%ums:", k_uptime_get_32());

		for (uint8_t ch = 0; ch < 8; ch++) {
			int32_t raw = 0;

			if (adc_setup_ch(adc_dev, ch) != 0 || adc_read_raw(adc_dev, ch, &raw) != 0) {
				shell_fprintf(sh, SHELL_NORMAL, "    err");
				continue;
			}
			shell_fprintf(sh, SHELL_NORMAL, " %6d", raw);
		}
		shell_print(sh, "");
		k_msleep(200);
	}
	return 0;
}

/* ------------------------------------------------------------ ② WS2812 */
static void strip_set(uint8_t r, uint8_t g, uint8_t b)
{
	struct led_rgb px = {.r = r, .g = g, .b = b};

	if (led_strip_update_rgb(strip_dev, &px, 1) != 0) {
		printk("[led] update failed\n");
	}
}

static int cmd_led(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 4) {
		shell_print(sh, "用法: korvo led <r> <g> <b>  (0..255)");
		return -EINVAL;
	}

	int r = atoi(argv[1]);
	int g = atoi(argv[2]);
	int b = atoi(argv[3]);

	strip_set(CLAMP(r, 0, 255), CLAMP(g, 0, 255), CLAMP(b, 0, 255));
	shell_print(sh, "[led] r=%d g=%d b=%d", r, g, b);
	return 0;
}

/* ------------------------------------- ⑨ WS2812 呼吸（用改数值，不是灌 PWM） */
/*
 * 板载那颗 RGB LED 是 WS2812（IO37，单线协议），不是普通 LED：
 *   普通 LED —— 靠 PWM 占空比连续调亮度，人眼看到"呼吸"
 *   WS2812   —— 每颗自带驱动 IC，亮度写在 24 bit 数据里，靠 RMT 波形发出去
 * 所以"呼吸"要改 RGB 数值（等价于改占空比），不能给 DATA 脚灌 PWM；
 * 真灌 PWM 只会收到乱码/闪烁，灯不会按占空比线性变亮。
 */
static int cmd_breath(const struct shell *sh, size_t argc, char **argv)
{
	int half_ms = (argc > 1) ? atoi(argv[1]) : 1500;   /* 0→最亮（或最亮→0）的时长 */
	int peak = (argc > 2) ? atoi(argv[2]) : 0x20;      /* 最亮时的单通道值 */
	int steps = 30;

	if (half_ms < 200 || half_ms > 20000) {
		half_ms = 1500;
	}
	peak = CLAMP(peak, 1, 255);

	shell_print(sh, "[breath] 单程 %d ms，峰值 %d（板载 WS2812 @ IO37）", half_ms, peak);

	for (int i = 0; i <= steps; i++) {
		int v = peak * i / steps;

		strip_set(v, v, v);           /* 白色呼吸；想换色改这三个分量即可 */
		k_msleep(half_ms / steps);
	}
	for (int i = steps; i >= 0; i--) {
		int v = peak * i / steps;

		strip_set(v, v, v);
		k_msleep(half_ms / steps);
	}
	shell_print(sh, "[breath] 一轮结束（已归零）");
	return 0;
}

/* ------------------------------------- 附加：同步呼吸（规范 v1.0）
 *
 * 和上面 korvo breath 的区别：
 *   korvo breath  是「驱动演示」—— 周期可调、逐档看得见，自变量是循环计数；
 *   korvo syncbreath 是「规范实现」—— 周期固定 4 s、亮度由绝对 UTC 查表得出。
 * 两台设备各敲一次 `korvo synctime`（同一个 epoch 秒）之后，呼吸严格同相：
 * 同一毫秒亮、同一毫秒暗、同一条余弦。
 */
static int cmd_synctime(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_print(sh, "用法: korvo synctime <epoch_seconds>");
		shell_print(sh, "  例: korvo synctime 1735689600  （= 2025-01-01T00:00:00Z，相位 0）");
		return -EINVAL;
	}

	int64_t sec = (int64_t)strtoul(argv[1], NULL, 0);
	int64_t mono = (int64_t)k_uptime_get() * 1000;
	int r = breath_clock_observe_src(&g_breath_clk, mono, sec * 1000000,
					 BREATH_SRC_MANUAL, 0);

	shell_print(sh, "[synctime] %u s → 处理=%d（1=吸收 2=缓释 3=跳变）",
		    (unsigned)sec, r);
	shell_print(sh, "[synctime] 等级=%u/3  相位=%u/256  预算=%d ms",
		    (unsigned)breath_clock_level(&g_breath_clk, mono),
		    (unsigned)breath_phase((uint64_t)(sec * 1000000)),
		    (int)(breath_clock_error_budget_us(&g_breath_clk, mono) / 1000));
	return 0;
}

static int cmd_syncbreath(const struct shell *sh, size_t argc, char **argv)
{
	int sec = (argc > 1) ? atoi(argv[1]) : 8;      /* 默认跑两个完整周期 */
	int peak = (argc > 2) ? atoi(argv[2]) : 0x20;

	if (sec < 1 || sec > 60) {
		sec = 8;
	}
	peak = CLAMP(peak, 1, 255);

	shell_print(sh, "[syncbreath] %d s，峰值 %d，周期 4 s（规范 v1.0，LUT 校验和 0x51521FF6）",
		    sec, peak);
	shell_print(sh, "[syncbreath] 等级=%u/3（0=未授时半亮，先 korvo synctime）",
		    (unsigned)breath_clock_level(&g_breath_clk,
						 (int64_t)k_uptime_get() * 1000));

	uint8_t out = 0;

	for (int i = 0; i < sec * 25; i++) {            /* 25 fps，与 korvo_apps 同 */
		int64_t mono = (int64_t)k_uptime_get() * 1000;
		int64_t utc = breath_clock_now_us(&g_breath_clk, mono);
		uint64_t now_u = (utc > 0) ? (uint64_t)utc : 0ull;
		uint8_t base = (uint8_t)peak;
		uint8_t gain = breath_gain_guard(100, breath_clock_level(&g_breath_clk, mono));

		/* n = 1：单色通道。这正是「单色 LED 与 RGB 灯同相」的做法 ——
		 * 同步的是亮度标量，通道数只决定把亮度铺到几路灯上（规范 §7.3）。 */
		breath_channels(now_u, &base, 1, gain, &out);

		strip_set(out, out, out);              /* 板载 WS2812 当单色灯用 */
		k_msleep(40);
	}

	strip_set(0, 0, 0);
	shell_print(sh, "[syncbreath] 结束。两台设备对过表后，这一路呼吸与别处严格同拍");
	return 0;
}

static int cmd_ledcycle(const struct shell *sh, size_t argc, char **argv)
{
	static const uint8_t seq[][3] = {
		{0x20, 0x00, 0x00}, {0x20, 0x10, 0x00}, {0x20, 0x20, 0x00},
		{0x00, 0x20, 0x00}, {0x00, 0x00, 0x20}, {0x10, 0x00, 0x20},
	};

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	for (size_t i = 0; i < ARRAY_SIZE(seq) + 1; i++) {
		size_t k = i % ARRAY_SIZE(seq);

		strip_set(seq[k][0], seq[k][1], seq[k][2]);
		k_msleep(400);
	}
	strip_set(0, 0, 0);
	shell_print(sh, "[led] 六色循环完成");
	return 0;
}

/* 不带 shell 的 PWM 输出（demo 线程用；shell 命令复用同一份） */
static int pwm_apply(int duty, uint32_t *period_out, uint32_t *pulse_out)
{
	uint64_t cycles = 0;

	if (pwm_get_cycles_per_sec(pwm_dev, 0, &cycles) != 0) {
		return -EIO;
	}

	uint32_t period = (uint32_t)(cycles / 1000);          /* 1 kHz */
	uint32_t pulse = (uint32_t)((uint64_t)period * duty / 100);

	if (pwm_set_cycles(pwm_dev, 0, period, pulse, PWM_POLARITY_NORMAL) != 0) {
		return -EIO;
	}

	if (period_out != NULL) {
		*period_out = period;
	}
	if (pulse_out != NULL) {
		*pulse_out = pulse;
	}
	return 0;
}

static int pwm_set_duty(const struct shell *sh, int duty)
{
	uint32_t period = 0;
	uint32_t pulse = 0;

	if (pwm_apply(duty, &period, &pulse) != 0) {
		shell_print(sh, "[pwm] set_cycles 失败");
		return -EIO;
	}

	shell_print(sh, "[pwm] %3d%%  period=%u pulse=%u (1 kHz @ IO44)", duty, period, pulse);
	return 0;
}

static int cmd_pwmsweep(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	for (int duty = 0; duty <= 100; duty += 10) {
		pwm_set_duty(sh, duty);
		k_msleep(300);
	}
	for (int duty = 100; duty >= 0; duty -= 10) {
		pwm_set_duty(sh, duty);
		k_msleep(300);
	}
	(void)pwm_set_cycles(pwm_dev, 0, 0, 0, PWM_POLARITY_NORMAL);   /* 停掉输出 */
	shell_print(sh, "[pwm] 扫描完成，输出已关闭");
	return 0;
}

/* --------------------------------------------------------------- ③ PWM */
static int cmd_pwm(const struct shell *sh, size_t argc, char **argv)
{
	int duty = (argc > 1) ? atoi(argv[1]) : 50;

	return pwm_set_duty(sh, CLAMP(duty, 0, 100));
}

/* --------------------------------------------------------------- ⑤ I2C */
static int cmd_i2c(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t dummy = 0;
	int found = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[i2c] 扫 I2C0（IO0=SDA / IO1=SCL）0x08..0x77 ...");

	for (uint8_t addr = 0x08; addr < 0x78; addr++) {
		if (i2c_write(i2c_dev, &dummy, 1, addr) == 0) {
			shell_print(sh, "[i2c]   0x%02x ACK", addr);
			found++;
		}
	}

	shell_print(sh, "[i2c] 共 %d 个器件（板载 ES8389 在 0x10 / 0x14）", found);
	return 0;
}

/* ------------------------------------------------- ⑥ CAN / TWAI 内部回环 */
K_MSGQ_DEFINE(can_msgq, sizeof(struct can_frame), 4, 4);

static int cmd_can(const struct shell *sh, size_t argc, char **argv)
{
	struct can_filter filter = {.id = 0, .mask = 0};
	struct can_frame tx = {
		.id = 0x123,
		.dlc = 8,
		.data = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88},
	};
	struct can_frame rx;
	int err;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	/* 回环必须在 stopped 状态下设置 */
	err = can_set_mode(can_dev, CAN_MODE_LOOPBACK);
	if (err != 0) {
		shell_print(sh, "[can] set_mode(LOOPBACK) 失败: %d", err);
		return err;
	}

	/* 位速率必须在 start 之前算好；dts 里没写 bus-speed 时驱动 timing 可能是 0，
	 * 那样 send 会直接 -EIO（TXT_TOK 等不到）。所以这里把返回值打出来。 */
	err = can_set_bitrate(can_dev, 500000);
	shell_print(sh, "[can] set_bitrate(500k) = %d%s", err, err == 0 ? "" : "  ← 这行非 0 就是发送失败的原因");

	shell_print(sh, "[can] mode=%s bitrate=500k", "LOOPBACK");
	(void)can_add_rx_filter_msgq(can_dev, &can_msgq, &filter);

	err = can_start(can_dev);
	if (err != 0 && err != -EALREADY) {
		shell_print(sh, "[can] start 失败: %d", err);
		return err;
	}

	enum can_state st = CAN_STATE_ERROR_ACTIVE;
	struct can_bus_err_cnt ec = {0, 0};

	(void)can_get_state(can_dev, &st, &ec);
	shell_print(sh, "[can] state=%d err_cnt(tx=%u rx=%u)（0=ERROR_ACTIVE 正常）",
		    (int)st, ec.tx_err_cnt, ec.rx_err_cnt);

	err = can_send(can_dev, &tx, K_MSEC(100), NULL, NULL);
	if (err != 0) {
		/* 这里不退出了：驱动的 TX-done 状态位可能把 loopback 判成 ERR，
		 * 但帧其实已经发出去并被自己收下。继续等 RX，用"收没收到"来定论。 */
		shell_print(sh, "[can] send 返回 %d（-5=EIO，驱动的 TX 完成状态位报错），继续等回环帧...", err);
	} else {
		shell_print(sh, "[can] 已发送 id=0x%03x dlc=%u（内部回环，无需收发器）", tx.id, tx.dlc);
	}

	if (k_msgq_get(&can_msgq, &rx, K_MSEC(500)) == 0) {
		shell_print(sh, "[can] 收到 id=0x%03x dlc=%u data=%02x %02x %02x %02x %02x %02x %02x %02x",
			    rx.id, rx.dlc, rx.data[0], rx.data[1], rx.data[2], rx.data[3],
			    rx.data[4], rx.data[5], rx.data[6], rx.data[7]);
		shell_print(sh, "[can] 结论: 回环通（TX 自收 OK）%s",
			    (err != 0) ? "，但 send 的状态位报 -EIO（驱动层问题，非应用层）" : "");
	} else {
		shell_print(sh, "[can] 没收到回环帧（500 ms 超时）");
	}

	/* 失败后再看一眼错误计数器：全 0 = 总线上没发生位错误/ACK 错误 */
	(void)can_get_state(can_dev, &st, &ec);
	shell_print(sh, "[can] 收尾 state=%d err_cnt(tx=%u rx=%u)", (int)st, ec.tx_err_cnt, ec.rx_err_cnt);

	(void)can_stop(can_dev);
	return 0;
}

/* ------------------------------------------------- ⑧ demo：把三层串成一个小应用 */
/*
 * 这是"应用层真用法"的样板，对照第四课的主线：
 *   输入（ADC 轮询） -> 消息队列（线程间通信） -> 动作（状态灯 + PWM）
 *
 * 为什么用轮询 + 消息队列，而不是中断回调：
 *   ADC 驱动没有"阈值中断"，按键本来就是靠轮询判电压窗；
 *   但"扫描"和"做事"仍然分成两个线程，用 msgq 解耦——
 *   扫描线程只管把"哪个键被按下"丢进队列，工作线程只管执行动作，
 *   以后换成真的 input 子系统，把 k_msgq_put 换成回调里的 put 即可，worker 不用改。
 */
K_MSGQ_DEFINE(demo_q, sizeof(int), 8, 4);

static volatile bool g_demo_on;
static volatile int g_demo_duty = 50;

static void demo_act(int k)
{
	uint32_t period = 0;
	uint32_t pulse = 0;

	switch (k) {
	case 0:                                            /* SET */
		strip_set(0x20, 0x00, 0x00);
		(void)pwm_apply(100, &period, &pulse);
		printk("[demo] SET  -> 红 + PWM 100%% (pulse=%u)\n", pulse);
		break;
	case 1:                                            /* PLAY */
		strip_set(0x00, 0x20, 0x00);
		(void)pwm_apply(50, &period, &pulse);
		printk("[demo] PLAY -> 绿 + PWM 50%% (pulse=%u)\n", pulse);
		break;
	case 2:                                            /* VOL- */
		g_demo_duty = MAX(g_demo_duty - 10, 0);
		strip_set(0x20, 0x10, 0x00);
		(void)pwm_apply(g_demo_duty, &period, &pulse);
		printk("[demo] VOL- -> 橙 + PWM %d%% (pulse=%u)\n", g_demo_duty, pulse);
		break;
	case 3:                                            /* VOL+ */
		g_demo_duty = MIN(g_demo_duty + 10, 100);
		strip_set(0x00, 0x00, 0x20);
		(void)pwm_apply(g_demo_duty, &period, &pulse);
		printk("[demo] VOL+ -> 蓝 + PWM %d%% (pulse=%u)\n", g_demo_duty, pulse);
		break;
	default:
		break;
	}
}

static void demo_worker(void)
{
	int k;

	while (true) {
		if (k_msgq_get(&demo_q, &k, K_FOREVER) == 0) {
			demo_act(k);
		}
	}
}

static void demo_scan(void)
{
	int last = -1;

	while (true) {
		int32_t raw = 0;
		int k = -1;

		if (!g_demo_on) {
			last = -1;
			k_msleep(100);
			continue;
		}

		if (adc_setup_ch(adc_dev, 0) == 0 && adc_read_raw(adc_dev, 0, &raw) == 0) {
			k = key_pick(raw_to_mv(raw));
		}

		if (k >= 0 && k != last) {                  /* 只在按下沿触发一次 */
			(void)k_msgq_put(&demo_q, &k, K_NO_WAIT);
		}
		last = k;
		k_msleep(50);
	}
}

K_THREAD_DEFINE(demo_scan_tid, 1024, demo_scan, NULL, NULL, NULL, 8, 0, 0);
K_THREAD_DEFINE(demo_work_tid, 1024, demo_worker, NULL, NULL, NULL, 7, 0, 0);

static int cmd_demo(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	g_demo_on = !g_demo_on;

	if (g_demo_on) {
		g_demo_duty = 50;
		shell_print(sh, "[demo] 开始（每 50 ms 扫一次四键）：");
		shell_print(sh, "       SET   -> 红灯 + PWM 100%%");
		shell_print(sh, "       PLAY  -> 绿灯 + PWM 50%%");
		shell_print(sh, "       VOL+  -> 蓝灯 + PWM +10%%");
		shell_print(sh, "       VOL-  -> 橙灯 + PWM -10%%");
		shell_print(sh, "       再敲一次 korvo demo 停止");
	} else {
		strip_set(0x00, 0x00, 0x00);
		(void)pwm_apply(0, NULL, NULL);
		shell_print(sh, "[demo] 已停止（灯灭、PWM 0%%）");
	}
	return 0;
}

/* -------------------------------------------------------------- 总览 */
static void ready_line(const struct shell *sh, const char *tag, const struct device *dev)
{
	shell_print(sh, "  %-10s %-10s %s", tag, dev->name,
		    device_is_ready(dev) ? "ready" : "NOT READY");
}

/* --------------------------------------------------- pad 数字电平体检 */
/*
 * 当 ADC 读出恒定小值时，要分清两种可能：
 *   A) IO42 根本没连到按键分压梯（板子版本/原理图与手册不一致）
 *   B) 连上了，但 ADC 通路（pad mux / 采样窗口）没生效
 * 办法：把同一根脚按"数字输入"读电平。开内部上拉后再按低档键，
 * 分压点会被按键硬拽低 -> 1 变 0。能变 = 硬件连着，问题在 ADC 侧。
 */
static int pad_cfg(uint32_t extra_flags)
{
	const struct device *gpio1 = DEVICE_DT_GET(DT_NODELABEL(gpio1));

	return gpio_pin_configure(gpio1, 10, GPIO_INPUT | extra_flags);
}

/* 通用：把任意 IO 号（0..61）按数字输入读电平，用来核对"这根脚是不是我要的那根" */
static int pad_level(int io, uint32_t extra_flags)
{
	int pin = io % 32;
	const struct device *port = (io < 32) ? DEVICE_DT_GET(DT_NODELABEL(gpio0))
					      : DEVICE_DT_GET(DT_NODELABEL(gpio1));

	if (!device_is_ready(port)) {
		return -99;
	}
	if (gpio_pin_configure(port, pin, GPIO_INPUT | extra_flags) != 0) {
		return -99;
	}
	k_msleep(5);
	int v = gpio_pin_get(port, pin);

	(void)gpio_pin_configure(port, pin, GPIO_DISCONNECTED);
	return v;
}

static void pad_to_analog(void)
{
	const struct device *gpio1 = DEVICE_DT_GET(DT_NODELABEL(gpio1));

	(void)gpio_pin_configure(gpio1, 10, GPIO_DISCONNECTED);
}

static int cmd_pad(const struct shell *sh, size_t argc, char **argv)
{
	int io = (argc > 1) ? atoi(argv[1]) : 42;

	if (io < 0 || io > 61) {
		shell_error(sh, "io 超出 0..61");
		return -EINVAL;
	}

	int up = pad_level(io, GPIO_PULL_UP);
	int down = pad_level(io, GPIO_PULL_DOWN);
	int flut = pad_level(io, 0);

	shell_print(sh, "[pad] IO%d 数字电平：上拉=%d 下拉=%d 悬空=%d", io, up, down, flut);

	if (io == 42) {
		int ref = pad_level(61, GPIO_PULL_UP);   /* BOOT 键：板上确实有上拉，用来校验读数通道本身 */

		shell_print(sh, "[pad] 参考 IO61(BOOT, 上拉)=%d（应为 1，否则是 GPIO 读数通道有问题）", ref);
	}
	return 0;
}

static int cmd_padwatch(const struct shell *sh, size_t argc, char **argv)
{
	const struct device *gpio1 = DEVICE_DT_GET(DT_NODELABEL(gpio1));
	int sec = (argc > 1) ? atoi(argv[1]) : 10;

	if (!device_is_ready(gpio1)) {
		shell_error(sh, "gpio1 未就绪");
		return -ENODEV;
	}
	if (sec < 1) {
		sec = 1;
	}
	if (sec > 60) {
		sec = 60;
	}

	if (pad_cfg(GPIO_PULL_UP) != 0) {
		shell_error(sh, "配置 IO42 失败");
		return -EIO;
	}

	int last = gpio_pin_get(gpio1, 10);

	shell_print(sh, "[padwatch] %d 秒，IO42 数字电平（内部上拉已开），现在请按 SET/PLAY/VOL-/VOL+", sec);
	shell_print(sh, "[padwatch] t=0.0s level=%d（松手时应为 1）", last);

	int64_t t0 = k_uptime_get();

	while ((k_uptime_get() - t0) < sec * 1000) {
		k_msleep(20);

		int now = gpio_pin_get(gpio1, 10);

		if (now != last) {
			shell_print(sh, "[padwatch] t=%d.%03ds level=%d -> %d",
				    (int)((k_uptime_get() - t0) / 1000),
				    (int)((k_uptime_get() - t0) % 1000), last, now);
			last = now;
		}
	}

	pad_to_analog();
	shell_print(sh, "[padwatch] 窗口结束，最后一次 level=%d", last);
	return 0;
}

static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[info] 应用层七项的设备状态：");
	ready_line(sh, "led_strip", strip_dev);
	ready_line(sh, "adc0", adc_dev);
	ready_line(sh, "ledc0", pwm_dev);
	ready_line(sh, "i2c0", i2c_dev);
	ready_line(sh, "twai0", can_dev);
	int32_t raw = 0;
	int32_t mv = -1;

	if (adc_setup_ch(adc_dev, 0) == 0 && adc_read_raw(adc_dev, 0, &raw) == 0) {
		mv = raw_to_mv(raw);
	}
	shell_print(sh, "[info] adc res=%u bit，IO42 现值 %d mV", g_adc_res, mv);
	return 0;
}

/* ---------------------------------------------------------------- shell */
SHELL_STATIC_SUBCMD_SET_CREATE(sub_korvo,
	SHELL_CMD(info, NULL, "设备就绪状态", cmd_info),
	SHELL_CMD_ARG(adc, NULL, "ADC 通道原始值 [次数]", cmd_adc, 1, 1),
	SHELL_CMD_ARG(keys, NULL, "监听 IO42 四键 [次数]", cmd_keys, 1, 1),
	SHELL_CMD_ARG(keyscan, NULL, "8 路 ADC 变化体检 [秒]", cmd_keyscan, 1, 1),
	SHELL_CMD_ARG(pad, NULL, "任意 IO 数字电平体检 [io]", cmd_pad, 1, 1),
	SHELL_CMD_ARG(padwatch, NULL, "IO42 数字电平变化监听 [秒]", cmd_padwatch, 1, 1),
	SHELL_CMD_ARG(led, NULL, "WS2812: led <r> <g> <b>", cmd_led, 4, 0),
	SHELL_CMD(ledcycle, NULL, "WS2812 六色循环", cmd_ledcycle),
	SHELL_CMD_ARG(breath, NULL, "WS2812 呼吸 [单程ms] [峰值0..255]", cmd_breath, 1, 2),
	SHELL_CMD_ARG(synctime, NULL, "人工授时，对表后多台设备同相", cmd_synctime, 2, 0),
	SHELL_CMD_ARG(syncbreath, NULL, "规范同步呼吸（4 s 余弦）[秒] [峰值]", cmd_syncbreath, 1, 2),
	SHELL_CMD_ARG(pwm, NULL, "PWM 占空比 0..100", cmd_pwm, 2, 0),
	SHELL_CMD(pwmsweep, NULL, "PWM 0→100 扫一遍", cmd_pwmsweep),
	SHELL_CMD(can, NULL, "CAN 内部回环自测", cmd_can),
	SHELL_CMD(i2c, NULL, "扫 I2C0 总线", cmd_i2c),
	SHELL_CMD(demo, NULL, "按键→消息队列→灯/PWM 的小应用（开关切换）", cmd_demo),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(korvo, &sub_korvo, "Korvo-1 应用层七项", NULL);

/* ---------------------------------------------------------------- main */
int main(void)
{
	printk("\n=== korvo_periph (S31 / Korvo-1 V1.1) ===\n");
	printk("应用层七项：ADC四键 / WS2812 / PWM / SPI屏(待硬件) / I2C / CAN回环 / LVGL(待模块)\n");
	printk("输入 korvo help 看命令；先跑 korvo info 确认设备 ready\n");
	printk("[breath] 同步呼吸：korvo synctime <epoch秒> → korvo syncbreath 8\n");

	/* 呼吸灯的 UTC 估计器：先自由运行（等级 0、半亮），等 korvo synctime 对表 */
	breath_clock_init_freerun(&g_breath_clk, (int64_t)k_uptime_get() * 1000);

	if (device_is_ready(strip_dev)) {
		strip_set(0x02, 0x08, 0x02);   /* 暗绿：一眼确认 WS2812 通 */
	}
	if (device_is_ready(adc_dev)) {
		(void)adc_setup_ch(adc_dev, 0);      /* 先 setup，命令里直接读才不会 EINVAL */
	}
	printk("[boot] adc res=%u bit\n", g_adc_res);
	return 0;
}
