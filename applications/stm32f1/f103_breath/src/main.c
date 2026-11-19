/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * f103_breath —— STM32F103C8T6（Blue Pill）最小闭环：
 *   呼吸灯（PC13）+ 串口日志（USART1 PA9/PA10）
 *
 * 这份源码里不写任何引脚号：
 *   LED ← dts 别名 led0（板级 dts 里是 gpioc 13，GPIO_ACTIVE_LOW）
 * 换一块"有 led0 别名"的板子，这份 main.c 不用改。
 *
 * 为什么还是"软件 PWM"：
 *   F103 的 PC13 只复用给 RTC/TAMPER 和 EXTI13，没有 TIMx_CHy 通道，硬件 PWM 到不了这颗灯
 *   （和 F411 的 PC13 是同一个道理）。这里用 1 kHz 帧 + k_busy_wait 切占空比，
 *   亮度分辨率 1 µs（≈0.1%）。代价是这段时间 CPU 被占满（见教程"代价与替代方案"一节）。
 *
 * 与 f411_breath 的差别：
 *   Blue Pill 板载只有 NRST / BOOT0 两个按钮，没有接到 GPIO 的用户按键，
 *   所以这一版不做按键切换，只跑"呼吸 + 周期统计"。
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>
#include <zephyr/version.h>

/* 1 kHz 帧：人眼看不到闪烁，同时 1 µs 的 busy_wait 分辨率足够细 */
#define PWM_PERIOD_US 1000

/* 呼吸曲线：sin^2，0→1000→0 一个完整周期，单位 µs（占空比 = duty / 1000） */
static const uint16_t duty_lut[] = {
	0, 2, 10, 22, 38, 59, 84, 113, 146, 183, 222, 264, 309, 355, 402, 451,
	500, 549, 598, 645, 691, 736, 778, 817, 854, 887, 916, 941, 962, 978, 990, 998,
	1000, 998, 990, 978, 962, 941, 916, 887, 854, 817, 778, 736, 691, 645, 598, 549,
	500, 451, 402, 355, 309, 264, 222, 183, 146, 113, 84, 59, 38, 22, 10, 2, 0
};

/* 每一档保持 47 帧 → 65 × 47 ≈ 3055 帧 ≈ 3.05 s 一次完整呼吸 */
#define FRAMES_PER_STEP 47

#define REPORT_PERIOD_S 2

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

static uint32_t cycles_to_us(uint32_t cycles)
{
	return (uint32_t)((uint64_t)cycles * 1000000ULL / sys_clock_hw_cycles_per_sec());
}

/* 开机自检：先量一次 k_busy_wait 的真实误差，再闪三下灯证明 GPIO 通路通 */
static void self_test(void)
{
	uint32_t t0, dt;

	t0 = k_cycle_get_32();
	k_busy_wait(1000);
	dt = k_cycle_get_32() - t0;
	printk("self-test: k_busy_wait(1000 us) = %u cycles = %u us (core %u MHz)\n",
	       dt, cycles_to_us(dt),
	       (uint32_t)(sys_clock_hw_cycles_per_sec() / 1000000U));

	for (int i = 0; i < 3; i++) {
		gpio_pin_set_dt(&led, 1); /* ACTIVE_LOW → 逻辑 1 = 引脚拉低 = 亮 */
		k_msleep(80);
		gpio_pin_set_dt(&led, 0);
		k_msleep(120);
	}
	printk("self-test: LED blink 3x done\n");
}

int main(void)
{
	uint32_t frame_start, dur;
	int64_t last_report_ms;
	uint32_t step = 0, frame_in_step = 0;
	uint32_t frames = 0, late = 0;
	uint32_t period_min = UINT32_MAX, period_max = 0, period_sum = 0, period_n = 0;

	printk("\n=== f103_breath ===\n");
	printk("board   : %s\n", CONFIG_BOARD_TARGET);
	printk("kernel  : %s\n", KERNEL_VERSION_STRING);
	printk("led0    : %s pin %u\n", led.port->name, led.pin);
	printk("pwm     : software, %u us frame, duty step 1 us\n", PWM_PERIOD_US);

	if (!gpio_is_ready_dt(&led)) {
		printk("GPIO device not ready, abort\n");
		return -1;
	}
	if (gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) != 0) {
		printk("GPIO configure failed, abort\n");
		return -1;
	}

	self_test();

	last_report_ms = k_uptime_get();
	printk("breathing ON\n");

	while (true) {
		frame_start = k_cycle_get_32();

		/* ---- 软件 PWM：一帧内先亮 on_us，再灭剩下的时间 ---- */
		uint16_t on_us = duty_lut[step];

		if (on_us > 0) {
			gpio_pin_set_dt(&led, 1);
			k_busy_wait(on_us);
		}
		gpio_pin_set_dt(&led, 0);
		k_busy_wait(PWM_PERIOD_US - on_us);

		frames++;
		if (++frame_in_step >= FRAMES_PER_STEP) {
			frame_in_step = 0;
			step = (step + 1) % ARRAY_SIZE(duty_lut);
		}

		/* 统计：空载 PWM 帧 ≈1005 us，"迟到"门限放 1050 us，
		 * 只有被中断/打印拉长的帧才算 late。 */
		dur = cycles_to_us(k_cycle_get_32() - frame_start);
		if (dur > PWM_PERIOD_US + 50) {
			late++;
		}
		period_sum += dur;
		period_n++;
		if (dur < period_min) {
			period_min = dur;
		}
		if (dur > period_max) {
			period_max = dur;
		}

		/* 上报节拍用 k_uptime_get()（毫秒、64 位）：
		 * 72 MHz 下 k_cycle_get_32() 的 2^32 个 cycle 只有 59.6 s，
		 * 拿它算秒数会在 1 分钟处回绕。周期测量仍然用 cycle（只取两次采样之差，回绕相减照样对）。 */
		if (k_uptime_get() - last_report_ms >= REPORT_PERIOD_S * 1000) {
			printk("t=%4lld s duty=%4u us frame avg=%u min=%u max=%u us late=%u frames=%u\n",
			       k_uptime_get() / 1000,
			       duty_lut[step],
			       period_sum / period_n,
			       period_min == UINT32_MAX ? 0 : period_min,
			       period_max, late, frames);
			last_report_ms = k_uptime_get();
			period_sum = 0;
			period_n = 0;
			period_min = UINT32_MAX;
			period_max = 0;
			late = 0;
		}
	}

	return 0;
}
