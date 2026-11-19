/*
 * Korvo-1 驱动实验台：lab gpio / lab i2c / lab adc / lab led
 *
 * 每个子命令打印"接口返回值 + 现场读数"，教程正文里的 [xxx] 行就是这里的原样输出。
 */

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/led_strip.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/drivers/can.h>
#include <zephyr/logging/log.h>

#include <stdlib.h>

/* 同步呼吸灯规范 v1.0（applications\common）：纯函数 + UTC 估计器，只依赖 stdint.h */
#include "breath_sync.h"

LOG_MODULE_REGISTER(lab, LOG_LEVEL_INF);

#define PA_GPIOS   DT_ALIAS(pa_ctrl)
#define KEY_GPIOS  DT_ALIAS(sw0)
#define STRIP_NODE DT_ALIAS(led_strip)

static const struct gpio_dt_spec pa  = GPIO_DT_SPEC_GET(PA_GPIOS, gpios);
static const struct gpio_dt_spec key = GPIO_DT_SPEC_GET(KEY_GPIOS, gpios);
static const struct device *strip = DEVICE_DT_GET(STRIP_NODE);
static const struct device *bus = DEVICE_DT_GET(DT_NODELABEL(i2c0));
static const struct device *adc_dev = DEVICE_DT_GET(DT_NODELABEL(adc0));

/* 第 3 项：PWM（breath 节点 = &ledc0 通道 0 → GPIO26） */
static const struct pwm_dt_spec breath = PWM_DT_SPEC_GET(DT_NODELABEL(breath));

/* 呼吸灯规范 v1.0 的 UTC 估计器：没网没 RTC，靠 lab synctime 人工对表 */
static struct breath_clock g_breath_clk;

/* 第 6 项：CAN（板载 twai0，环回自发自收） */
static const struct device *can_dev = DEVICE_DT_GET(DT_NODELABEL(twai0));
K_MSGQ_DEFINE(can_rxq, sizeof(struct can_frame), 4, 4);

/* ES8389 在 0x10（i2c scan 实测），另一个从设备在 0x14 */
#define CODEC_ADDR 0x10
#define GHOST_ADDR 0x50

static const char *err(int ret)
{
	switch (ret) {
	case 0:          return "0";
	case -ENOTSUP:   return "-ENOTSUP";
	case -ENODEV:    return "-ENODEV";
	case -EIO:       return "-EIO";
	case -EINVAL:    return "-EINVAL";
	case -EBUSY:     return "-EBUSY";
	case -ENOSYS:    return "-ENOSYS";
	case -ETIMEDOUT: return "-ETIMEDOUT";
	default:         return "?";
	}
}

static volatile uint32_t cb_count;
static volatile uint32_t cb_pins;
static struct gpio_callback key_cb;

static void on_key(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);

	cb_count++;
	cb_pins = pins;
}

/* --------------------------------------------------------------- GPIO 原始寄存器 */
/*
 * gpio0 基址 0x20583000，gpio1 基址 0x20583800
 *   IN   = +0x64   IN1 = +0x68   ENABLE = +0x34
 * 用来验证"按键到底有没有动"，绕开驱动抽象层。
 */
#define GPIO0_BASE 0x20583000u
#define GPIO1_BASE 0x20583800u
#define REG_IN     0x64u
#define REG_IN1    0x68u
#define REG_ENA    0x34u

static int cmd_key(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	const struct device *g0 = DEVICE_DT_GET(DT_NODELABEL(gpio0));
	int ret;

	shell_print(sh, "配置前: ENABLE1=0x%08x IN1=0x%08x", sys_read32(GPIO0_BASE + 0x40u),
		    sys_read32(GPIO0_BASE + REG_IN1));

	/* BOOT 是物理引脚 61（bank1 第 29 个）：直接用 gpio0 设备 + 61 */
	ret = gpio_pin_configure(g0, 61, GPIO_INPUT | GPIO_PULL_UP);
	shell_print(sh, "gpio_pin_configure(gpio0, 61, INPUT|PULL_UP) = %s", err(ret));
	k_msleep(20);

	shell_print(sh, "配置后: ENABLE1=0x%08x IN1=0x%08x, api_get(61)=%d",
		    sys_read32(GPIO0_BASE + 0x40u), sys_read32(GPIO0_BASE + REG_IN1),
		    gpio_pin_get(g0, 61));

	uint32_t last = 0xFFFFFFFFu;

	shell_print(sh, "6 秒内监听，请按住 BOOT 键 2 秒再松开...");

	for (int i = 0; i < 60; i++) {
		uint32_t in1 = sys_read32(GPIO0_BASE + REG_IN1);
		int lvl_api = gpio_pin_get(g0, 61);
		uint32_t lvl_raw = !!(in1 & BIT(29));

		if (lvl_raw != last) {
			shell_print(sh, "[t=%d.%01ds] IN1=0x%08x bit29=%u api_get=%d", i / 10,
				    i % 10, in1, lvl_raw, lvl_api);
			last = lvl_raw;
		}
		k_msleep(100);
	}

	shell_print(sh, "结束: IN1=0x%08x, api_get=%d", sys_read32(GPIO0_BASE + REG_IN1),
		    gpio_pin_get(g0, 61));
	return 0;
}

/* ---------------------------------------------------------------- GPIO */
static int cmd_gpio(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	int ret;
	int level;
	gpio_port_value_t v;

	shell_print(sh, "[node] pa=%s key=%s", pa.port->name, key.port->name);

	/* ---- configure ---- */
	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
	shell_print(sh, "[configure] PA%d 输出，初始低 = %s", pa.pin, err(ret));

	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT | GPIO_OPEN_DRAIN);
	shell_print(sh, "[configure] PA%d 开漏输出 = %s", pa.pin, err(ret));

	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT | GPIO_OPEN_SOURCE);
	shell_print(sh, "[configure] PA%d 开源输出 = %s", pa.pin, err(ret));

	ret = gpio_pin_configure_dt(&key, GPIO_INPUT | GPIO_PULL_UP);
	shell_print(sh, "[configure] BOOT(gpio1 pin%d) 输入+上拉 = %s", key.pin, err(ret));

	/* ---- set / get / raw ---- */
	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
	ret = gpio_pin_set_dt(&pa, 1);
	shell_print(sh, "[set] gpio_pin_set_dt(PA,1) = %s", err(ret));

	level = gpio_pin_get_dt(&pa);
	shell_print(sh, "[get] gpio_pin_get_dt(PA) = %d", level);

	ret = gpio_port_get_raw(pa.port, &v);
	shell_print(sh, "[get_raw] gpio_port_get_raw(gpio0) = %s, PA 位 = %d", err(ret),
		    !!(v & BIT(pa.pin)));

	ret = gpio_pin_set_dt(&pa, 0);
	shell_print(sh, "[set] gpio_pin_set_dt(PA,0) = %s", err(ret));

	/* ---- toggle ---- */
	ret = gpio_pin_toggle_dt(&pa);
	shell_print(sh, "[toggle] gpio_pin_toggle_dt(PA) = %s", err(ret));
	for (int i = 0; i < 6; i++) {
		k_msleep(100);
		gpio_pin_toggle_dt(&pa);
	}
	shell_print(sh, "[toggle] 翻转 6 次完成（PA_CTRL 无指示灯，靠返回值确认）");

	/* ---- 端口级操作 ---- */
	ret = gpio_port_clear_bits_raw(pa.port, BIT(pa.pin));
	gpio_port_get_raw(pa.port, &v);
	shell_print(sh, "[port_clear] gpio_port_clear_bits_raw = %s, PA 位 = %d", err(ret),
		    !!(v & BIT(pa.pin)));

	ret = gpio_port_set_bits_raw(pa.port, BIT(pa.pin));
	gpio_port_get_raw(pa.port, &v);
	shell_print(sh, "[port_set] gpio_port_set_bits_raw = %s, PA 位 = %d", err(ret),
		    !!(v & BIT(pa.pin)));

	ret = gpio_port_toggle_bits(pa.port, BIT(pa.pin));
	gpio_port_get_raw(pa.port, &v);
	shell_print(sh, "[port_toggle] gpio_port_toggle_bits = %s, PA 位 = %d", err(ret),
		    !!(v & BIT(pa.pin)));

	ret = gpio_port_set_masked_raw(pa.port, BIT(pa.pin), 0);
	gpio_port_get_raw(pa.port, &v);
	shell_print(sh, "[port_masked] gpio_port_set_masked_raw(PA,0) = %s, PA 位 = %d", err(ret),
		    !!(v & BIT(pa.pin)));

	/* ---- 按键输入 ---- */
	k_msleep(10);
	level = gpio_pin_get_dt(&key);
	shell_print(sh, "[get] gpio_pin_get_dt(BOOT) = %d  (0=按下 1=松开)", level);

	ret = gpio_port_get_raw(key.port, &v);
	shell_print(sh, "[get_raw] gpio_port_get_raw(gpio1) = %s, BOOT 位 = %d", err(ret),
		    !!(v & BIT(key.pin)));

	/* ---- 中断 ---- */
	gpio_init_callback(&key_cb, on_key, BIT(key.pin));
	ret = gpio_add_callback(key.port, &key_cb);
	shell_print(sh, "[init_callback] gpio_add_callback = %s, pin_mask = 0x%08x", err(ret),
		    (unsigned int)key_cb.pin_mask);

	ret = gpio_pin_interrupt_configure_dt(&key, GPIO_INT_EDGE_BOTH);
	shell_print(sh, "[interrupt] 装 EDGE_BOTH = %s", err(ret));
	shell_print(sh, "[interrupt] 现在按几下 BOOT 键（10 秒窗口）...");

	cb_count = 0;
	uint32_t edges = 0;
	int last = gpio_pin_get_dt(&key);

	for (int i = 0; i < 100; i++) {
		k_msleep(100);
		int cur = gpio_pin_get_dt(&key);

		if (cur != last) {
			edges++;
			last = cur;
		}
	}
	shell_print(sh, "[interrupt] 轮询到电平变化 %u 次，回调 %u 次", edges, cb_count);
	shell_print(sh, "[interrupt] 最后 pins = 0x%08x，当前电平 = %d", cb_pins,
		    gpio_pin_get_dt(&key));

	ret = gpio_pin_interrupt_configure_dt(&key, GPIO_INT_DISABLE);
	shell_print(sh, "[interrupt] GPIO_INT_DISABLE = %s", err(ret));

	ret = gpio_get_pending_int(key.port);
	shell_print(sh, "[pending] gpio_get_pending_int(gpio1) = %d", ret);

	return 0;
}

/* ----------------------------------------------------------------- I2C */
static int cmd_i2c(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (!device_is_ready(bus)) {
		shell_error(sh, "i2c0 not ready");
		return -ENODEV;
	}

	shell_print(sh, "[node] bus=%s", bus->name);

	/* ---- i2c_configure ---- */
	int ret;

	ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD) | I2C_MODE_CONTROLLER);
	shell_print(sh, "[configure] standard 100 kHz = %s", err(ret));

	ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_FAST) | I2C_MODE_CONTROLLER);
	shell_print(sh, "[configure] fast 400 kHz = %s", err(ret));

	ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_FAST_PLUS) | I2C_MODE_CONTROLLER);
	shell_print(sh, "[configure] fast plus 1 MHz = %s", err(ret));

	ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD) | I2C_MODE_CONTROLLER |
			    I2C_ADDR_10_BITS);
	shell_print(sh, "[configure] 10 bit 地址 = %s", err(ret));

	ret = i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_STANDARD));
	shell_print(sh, "[configure] 缺 I2C_MODE_CONTROLLER = %s", err(ret));

	i2c_configure(bus, I2C_SPEED_SET(I2C_SPEED_FAST) | I2C_MODE_CONTROLLER);

	/* ---- i2c_get_config ---- */
	uint32_t cfg = 0;

	ret = i2c_get_config(bus, &cfg);
	shell_print(sh, "[get_config] i2c_get_config = %s, cfg = 0x%08x", err(ret), cfg);

	/* ---- 扫描 ---- */
	int found = 0;

	shell_print(sh, "[scan] 0x08..0x77 逐个零字节写：");
	for (uint16_t a = 0x08; a <= 0x77; a++) {
		if (i2c_write(bus, NULL, 0, a) == 0) {
			shell_print(sh, "[scan] 发现 0x%02x", a);
			found++;
		}
	}
	shell_print(sh, "[scan] 共 %d 个从设备", found);

	/* ---- write 设寄存器地址 + read 取值 ---- */
	uint8_t reg = 0x00;
	uint8_t val = 0;

	ret = i2c_write(bus, &reg, 1, CODEC_ADDR);
	shell_print(sh, "[write] i2c_write(reg 0x00) = %s", err(ret));

	ret = i2c_read(bus, &val, 1, CODEC_ADDR);
	shell_print(sh, "[read]  i2c_read(1B) = %s, val = 0x%02x", err(ret), val);

	/* ---- 读一串寄存器 ---- */
	uint8_t regs[8] = {0};

	for (uint8_t r = 0; r < 8; r++) {
		uint8_t v = 0;

		ret = i2c_reg_read_byte(bus, CODEC_ADDR, r, &v);
		if (ret == 0) {
			regs[r] = v;
		}
	}
	shell_print(sh, "[regs] 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x 0x%02x",
		    regs[0], regs[1], regs[2], regs[3], regs[4], regs[5], regs[6], regs[7]);

	/* ---- write_read（无停止的重复起始）---- */
	uint8_t burst[4] = {0};

	ret = i2c_write_read(bus, CODEC_ADDR, &reg, 1, burst, sizeof(burst));
	shell_print(sh, "[write_read] = %s: %02x %02x %02x %02x", err(ret), burst[0], burst[1],
		    burst[2], burst[3]);

	/* ---- burst_read ---- */
	ret = i2c_burst_read(bus, CODEC_ADDR, 0x00, burst, sizeof(burst));
	shell_print(sh, "[burst] i2c_burst_read(0x00,4) = %s", err(ret));

	/* ---- transfer ---- */
	struct i2c_msg msgs[2] = {
		{ .buf = &reg, .len = 1, .flags = I2C_MSG_WRITE },
		{ .buf = burst, .len = sizeof(burst),
		  .flags = I2C_MSG_READ | I2C_MSG_RESTART | I2C_MSG_STOP },
	};

	ret = i2c_transfer(bus, msgs, 2, CODEC_ADDR);
	shell_print(sh, "[transfer] write+restart+read = %s", err(ret));

	msgs[1].flags |= I2C_MSG_ADDR_10_BITS;
	ret = i2c_transfer(bus, msgs, 2, CODEC_ADDR);
	shell_print(sh, "[transfer] 10 bit 地址 = %s", err(ret));

	msgs[1].flags &= ~I2C_MSG_ADDR_10_BITS;
	ret = i2c_transfer(bus, msgs, 2, 0x100);
	shell_print(sh, "[transfer] 地址 0x100 = %s", err(ret));

	/* ---- 没有设备应答 ---- */
	uint8_t b = 0;

	ret = i2c_read(bus, &b, 1, GHOST_ADDR);
	shell_print(sh, "[nack] 读 0x%02x = %s", GHOST_ADDR, err(ret));

	ret = i2c_write(bus, &b, 1, GHOST_ADDR);
	shell_print(sh, "[nack] 写 0x%02x = %s", GHOST_ADDR, err(ret));

	return 0;
}

/* ----------------------------------------------------------------- ADC */
static int cmd_adc(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (!device_is_ready(adc_dev)) {
		shell_error(sh, "adc0 not ready");
		return -ENODEV;
	}

	int32_t raw = 0;
	struct adc_sequence seq = {
		.resolution = 17,           /* S31 只有 17 位，别的值驱动拒绝 */
		.buffer = &raw,
		.buffer_size = sizeof(raw),
		.channels = 0,
	};

	shell_print(sh, "[node] adc=%s, resolution=17", adc_dev->name);

	for (int ch = 0; ch < 8; ch++) {
		struct adc_channel_cfg cfg = {
			.gain = ADC_GAIN_1,
			.reference = ADC_REF_INTERNAL,
			.acquisition_time = ADC_ACQ_TIME_DEFAULT,
			.channel_id = ch,
		};

		int ret = adc_channel_setup(adc_dev, &cfg);

		if (ret != 0) {
			shell_print(sh, "[setup] ch%d = %s", ch, err(ret));
			continue;
		}

		seq.channels = BIT(ch);
		ret = adc_read(adc_dev, &seq);
		if (ret != 0) {
			shell_print(sh, "[read] ch%d = %s", ch, err(ret));
			continue;
		}

		int32_t mv = raw;

		ret = adc_raw_to_millivolts(1100, ADC_GAIN_1, 17, &mv);
		shell_print(sh, "[read] ch%d 原始=%d%s", ch, raw,
			    ret == 0 ? "" : "(换算失败)");

		if (ret == 0) {
			shell_print(sh, "       约 %d mV", mv);
		}
	}
	return 0;
}

/* ----------------------------------------------------------------- LED */
static int cmd_led(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (!device_is_ready(strip)) {
		shell_error(sh, "led_strip not ready：检查 CONFIG_WS2812_STRIP_PULSE_IO 和 &rmt");
		return -ENODEV;
	}

	shell_print(sh, "[node] strip=%s, length=%d", strip->name, led_strip_length(strip));
	shell_print(sh, "[color-mapping] 板级 dts 是 GRB，驱动自动按此重排");

	struct led_rgb px = {0};
	const char *names[] = {"红", "绿", "蓝", "白", "灭"};
	struct led_rgb colors[] = {
		{.r = 0x20, .g = 0x00, .b = 0x00},
		{.r = 0x00, .g = 0x20, .b = 0x00},
		{.r = 0x00, .g = 0x00, .b = 0x20},
		{.r = 0x20, .g = 0x20, .b = 0x20},
		{.r = 0x00, .g = 0x00, .b = 0x00},
	};

	for (int i = 0; i < ARRAY_SIZE(colors); i++) {
		px = colors[i];
		int ret = led_strip_update_rgb(strip, &px, 1);

		shell_print(sh, "[update] %s = %s", names[i], err(ret));
		k_msleep(400);
	}
	shell_print(sh, "[update] 完成（WS2812 在 GPIO37）");
	return 0;
}

/* ------------------------------------------------------------- 第 3 项：PWM */
static int cmd_pwm(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (!device_is_ready(breath.dev)) {
		shell_error(sh, "ledc0 通道 0 未就绪：检查 &ledc0 的 status / channel0@0 / pinctrl");
		return -ENODEV;
	}

	uint32_t period = breath.period;
	int ret;

	shell_print(sh, "[node] pwm=%s, channel=%u, period=%u ns (%u Hz)",
		    breath.dev->name, breath.channel, period, 1000000000u / period);

	/* 0 → 满 → 0 扫两遍：外接 LED 能看到两次呼吸 */
	for (int pass = 0; pass < 2; pass++) {
		for (int i = 0; i <= 20; i++) {
			uint32_t pulse = period * (pass == 0 ? i : (20 - i)) / 20u;

			ret = pwm_set_dt(&breath, period, pulse);
			if (ret != 0) {
				shell_error(sh, "[set] duty=%u%% = %s", i * 5, err(ret));
				return ret;
			}
			shell_print(sh, "[set] duty=%3u%%  pulse=%u ns = %s",
				    i * 5, pulse, err(ret));
			k_msleep(120);
		}
	}

	ret = pwm_set_dt(&breath, period, 0);
	shell_print(sh, "[set] 收尾 duty=0 = %s（GPIO26 上没有负载时只能看返回值）", err(ret));
	return 0;
}

/* ------------------------------- 附加：同步呼吸（规范 v1.0，PWM 路径）
 *
 * 上面 lab pwm 是「逐档扫一遍看返回值」的驱动实验，自变量是循环计数；
 * 这里 lab syncbreath 是「规范实现」：周期固定 4 s、亮度由绝对 UTC 查表，
 * 所以两台设备 `lab synctime` 对过同一个 epoch 秒之后，呼吸严格同相。
 *
 * 这条命令同时演示规范 §7.5 的做法：用 12 位占空比，而不是 8 位 ——
 * 8 位在 level=1..5 时只有 5 个台阶，暗部会“咯噔”一下。
 */
static int cmd_synctime(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_print(sh, "用法: lab synctime <epoch_seconds>");
		shell_print(sh, "  例: lab synctime 1735689600  （= 2025-01-01T00:00:00Z，相位 0）");
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
	if (!device_is_ready(breath.dev)) {
		shell_error(sh, "ledc0 通道 0 未就绪：检查 &ledc0 的 status / channel0@0 / pinctrl");
		return -ENODEV;
	}

	int sec = (argc > 1) ? atoi(argv[1]) : 8;       /* 默认跑两个完整周期 */
	int peak = (argc > 2) ? atoi(argv[2]) : 255;

	if (sec < 1 || sec > 60) {
		sec = 8;
	}
	peak = CLAMP(peak, 1, 255);

	uint32_t period = breath.period;

	shell_print(sh, "[syncbreath] %d s，峰值 %d，周期 4 s，12 位占空比（规范 v1.0）",
		    sec, peak);
	shell_print(sh, "[syncbreath] 等级=%u/3（0=未授时半亮，先 lab synctime）",
		    (unsigned)breath_clock_level(&g_breath_clk,
						 (int64_t)k_uptime_get() * 1000));

	for (int i = 0; i < sec * 25; i++) {            /* 25 fps */
		int64_t mono = (int64_t)k_uptime_get() * 1000;
		int64_t utc = breath_clock_now_us(&g_breath_clk, mono);
		uint64_t now_u = (utc > 0) ? (uint64_t)utc : 0ull;
		uint8_t base = (uint8_t)peak;
		uint8_t out = 0;
		uint8_t gain = breath_gain_guard(100, breath_clock_level(&g_breath_clk, mono));

		/* n = 1：单色 PWM 通道。同一个亮度标量，换成 RGB 只是把 n 改成 3 */
		breath_channels(now_u, &base, 1, gain, &out);

		uint32_t duty = breath_duty(out, 12);                  /* 0..4095 */
		uint32_t pulse = (uint32_t)((uint64_t)period * duty / 4095u);

		int ret = pwm_set_dt(&breath, period, pulse);

		if (ret != 0) {
			shell_error(sh, "[syncbreath] duty=%u pulse=%u ns = %s",
				    duty, pulse, err(ret));
			return ret;
		}
		k_msleep(40);
	}

	pwm_set_dt(&breath, period, 0);
	shell_print(sh, "[syncbreath] 结束。两台设备对过表后，这一路呼吸与别处严格同拍");
	return 0;
}

/* ------------------------------------------------------------- 第 6 项：CAN */
static int cmd_can(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (!device_is_ready(can_dev)) {
		shell_error(sh, "twai0 未就绪：检查 board dts 里 twai0 是否 okay");
		return -ENODEV;
	}

	int ret;

	shell_print(sh, "[node] can=%s", can_dev->name);

	ret = can_set_mode(can_dev, CAN_MODE_LOOPBACK);
	shell_print(sh, "[set_mode] CAN_MODE_LOOPBACK = %s", err(ret));
	if (ret != 0) {
		return ret;
	}

	ret = can_set_bitrate(can_dev, 500000);
	shell_print(sh, "[set_bitrate] 500 kbit/s = %s", err(ret));

	struct can_filter filter = {
		.id = 0x123,
		.mask = 0x7FF,
	};
	int filter_idx = can_add_rx_filter_msgq(can_dev, &can_rxq, &filter);

	shell_print(sh, "[rx_filter] add = %d", filter_idx);

	ret = can_start(can_dev);
	shell_print(sh, "[start] can_start = %s", err(ret));
	if (ret != 0) {
		return ret;
	}

	struct can_frame tx = {0};
	struct can_frame rx = {0};

	tx.id = 0x123;
	tx.dlc = 8;
	for (int i = 0; i < 8; i++) {
		tx.data[i] = (uint8_t)(0xA0 + i);
	}

	ret = can_send(can_dev, &tx, K_MSEC(200), NULL, NULL);
	shell_print(sh, "[send] id=0x123 dlc=8 = %s", err(ret));

	ret = k_msgq_get(&can_rxq, &rx, K_MSEC(300));
	if (ret == 0) {
		shell_print(sh, "[recv] id=0x%03x dlc=%u data=%02x %02x %02x %02x %02x %02x %02x %02x",
			    rx.id, rx.dlc, rx.data[0], rx.data[1], rx.data[2], rx.data[3],
			    rx.data[4], rx.data[5], rx.data[6], rx.data[7]);

		bool same = (rx.id == tx.id) && (rx.dlc == tx.dlc);

		for (int i = 0; i < 8; i++) {
			if (rx.data[i] != tx.data[i]) {
				same = false;
			}
		}
		shell_print(sh, "[verify] 环回一致 = %s", same ? "是" : "否");
	} else {
		shell_print(sh, "[recv] 300ms 内没收到 = %s（环回模式下正常应立刻收到）", err(ret));
	}

	can_stop(can_dev);
	can_remove_rx_filter(can_dev, filter_idx);
	shell_print(sh, "[stop] can_stop + remove_filter 完成");

	return 0;
}

/* --------------------------------------------- 第 1 项：ADC 键电压校准窗口 */
static int cmd_adcwatch(const struct shell *sh, size_t argc, char **argv)
{
	if (!device_is_ready(adc_dev)) {
		shell_error(sh, "adc0 not ready");
		return -ENODEV;
	}

	uint32_t ch = (argc > 1) ? (uint32_t)atoi(argv[1]) : 0;
	int32_t raw = 0;
	struct adc_sequence seq = {
		.resolution = 17,
		.buffer = &raw,
		.buffer_size = sizeof(raw),
		.channels = BIT(ch),
	};
	struct adc_channel_cfg cfg = {
		.gain = ADC_GAIN_1,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME_DEFAULT,
		.channel_id = ch,
	};

	int ret = adc_channel_setup(adc_dev, &cfg);

	if (ret != 0) {
		shell_error(sh, "channel %u setup = %s", ch, err(ret));
		return ret;
	}

	shell_print(sh, "通道 %u，8 秒窗口：请依次按 PLAY / SET / VOL- / VOL+，每按一下停 1 秒", ch);

	int32_t last = -32768;

	for (int i = 0; i < 80; i++) {
		int32_t mv = raw;

		raw = 0;
		ret = adc_read(adc_dev, &seq);
		if (ret != 0) {
			shell_print(sh, "[read] = %s", err(ret));
			return ret;
		}
		mv = raw;
		adc_raw_to_millivolts(1100, ADC_GAIN_1, 17, &mv);

		if (last == -32768 || abs(mv - last) >= 30) {
			shell_print(sh, "[%d.%01ds] %d mV", i / 10, i % 10, mv);
			last = mv;
		}
		k_msleep(100);
	}

	shell_print(sh, "窗口结束，最后读数 %d mV", last);
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_lab,
	SHELL_CMD(gpio, NULL, "GPIO：configure/set/get/raw/端口/中断", cmd_gpio),
	SHELL_CMD(key, NULL, "BOOT 键原始寄存器观察（绕开驱动）", cmd_key),
	SHELL_CMD(i2c, NULL, "I2C：configure/get_config/scan/read/write/transfer", cmd_i2c),
	SHELL_CMD(adc, NULL, "ADC：8 个通道（17 位）", cmd_adc),
	SHELL_CMD(adcwatch, NULL, "ADC 键电压校准：lab adcwatch [通道]", cmd_adcwatch),
	SHELL_CMD(led, NULL, "WS2812：RGB 依次点亮", cmd_led),
	SHELL_CMD(pwm, NULL, "PWM：LEDC0 通道0(GPIO26) 呼吸", cmd_pwm),
	SHELL_CMD_ARG(synctime, NULL, "人工授时，对表后多台设备同相", cmd_synctime, 2, 0),
	SHELL_CMD_ARG(syncbreath, NULL, "规范同步呼吸（4 s 余弦，PWM 12 位）[秒] [峰值]", cmd_syncbreath, 1, 2),
	SHELL_CMD(can, NULL, "CAN：twai0 环回自发自收", cmd_can),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(lab, &sub_lab, "Korvo-1 驱动实验台", NULL);

int main(void)
{
	printk("\n=== Korvo-1 lab ===\n");
	printk("lab gpio / lab i2c / lab adc / lab led\n");
	printk("[breath] 同步呼吸：lab synctime <epoch秒> → lab syncbreath 8（PWM 灯，周期 4 s）\n");

	/* 呼吸灯的 UTC 估计器：先自由运行（等级 0、半亮），等 lab synctime 对表 */
	breath_clock_init_freerun(&g_breath_clk, (int64_t)k_uptime_get() * 1000);
	return 0;
}
