/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * f411_breath —— STM32F411CEU6（WeAct Black Pill V2.0）最小闭环：
 *   呼吸灯（PC13）+ 按键（PA0）+ 串口日志（USART1 PA9/PA10）
 *
 * 这一份源码里不写任何引脚号：
 *   LED  ← dts 别名 led0（板级 dts 里是 gpioc 13，GPIO_ACTIVE_LOW）
 *   KEY  ← dts 别名 sw0 （板级 dts 里是 gpioa 0，GPIO_ACTIVE_LOW | GPIO_PULL_UP）
 * 换一块"有 led0/sw0 别名"的板子，这份 main.c 不用改。
 *
 * 为什么是"软件 PWM"：
 *   F411 的 PC13 只复用给 RTC/TAMP，没有 TIMx_CHy 通道，硬件 PWM 到不了这颗灯。
 *   这里用 1 kHz 帧 + k_busy_wait 切占空比，亮度分辨率 1 µs（≈0.1%）。
 *   代价是这段时间 CPU 被占满（见教程"代价与替代方案"一节）。
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

#define KEY_DEBOUNCE_FRAMES 20 /* 1 kHz 采样 → 20 ms 去抖 */
#define REPORT_PERIOD_S 2

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec key = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

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
	uint32_t key_count = 0;
	int key_state, key_prev;
	bool breathe = true;

	printk("\n=== f411_breath ===\n");
	printk("board   : %s\n", CONFIG_BOARD_TARGET);
	printk("kernel  : %s\n", KERNEL_VERSION_STRING);
	printk("led0    : %s pin %u\n", led.port->name, led.pin);
	printk("sw0     : %s pin %u\n", key.port->name, key.pin);
	printk("pwm     : software, %u us frame, duty step 1 us\n", PWM_PERIOD_US);

	if (!gpio_is_ready_dt(&led) || !gpio_is_ready_dt(&key)) {
		printk("GPIO device not ready, abort\n");
		return -1;
	}
	if (gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) != 0 ||
	    gpio_pin_configure_dt(&key, GPIO_INPUT) != 0) {
		printk("GPIO configure failed, abort\n");
		return -1;
	}

	self_test();

	key_prev = gpio_pin_get_dt(&key);
	last_report_ms = k_uptime_get();
	printk("breathing ON (press KEY/PA0 to toggle)\n");

	while (true) {
		frame_start = k_cycle_get_32();

		/* ---- 按键：1 kHz 采样 + 连续 20 帧一致才算一次跳动 ---- */
		key_state = gpio_pin_get_dt(&key);
		if (key_state != key_prev) {
			if (++key_count >= KEY_DEBOUNCE_FRAMES) {
				key_prev = key_state;
				key_count = 0;
				if (key_state) { /* 逻辑 1 = 按下（ACTIVE_LOW） */
					breathe = !breathe;
					step = 0;
					frame_in_step = 0;
					printk("KEY pressed -> breathing %s\n",
					       breathe ? "ON" : "OFF");
				}
			}
		} else {
			key_count = 0;
		}

		/* ---- 软件 PWM：一帧内先亮 on_us，再灭剩下的时间 ---- */
		if (breathe) {
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
		} else {
			gpio_pin_set_dt(&led, 0);
			k_msleep(1); /* 关灯时把 CPU 让出去 */
		}

		/* 统计：只统计"正在呼吸"的软 PWM 帧。
		 * 关灯态主循环是 k_msleep(1)，帧周期≈1094 us，那不是 PWM 帧，
		 * 若一起计入会把 late 每帧 +1、frames 冻结（本轮按键验证时就撞到了）。
		 * 空载 PWM 帧 ≈1005 us，所以"迟到"门限放 1050 us：只有被中断/打印拉长的帧才算。 */
		if (breathe) {
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
		}

		/* 上报节拍用 k_uptime_get()（毫秒、64 位）。
		 * 早期版本用 k_cycle_get_32() 算秒数，96 MHz 下 2^32 个 cycle 只有 44.7 s，
		 * 于是 t= 会在 44.7 s 处回绕（实测 t=44 s 之后突然跳成 t=1 s）。
		 * 周期测量仍然用 cycle：它只需要"两次采样之差"，回绕相减照样正确。 */
		if (k_uptime_get() - last_report_ms >= REPORT_PERIOD_S * 1000) {
			if (breathe && period_n) {
				printk("t=%4lld s duty=%4u us frame avg=%u min=%u max=%u us late=%u frames=%u\n",
				       k_uptime_get() / 1000,
				       duty_lut[step],
				       period_sum / period_n,
				       period_min == UINT32_MAX ? 0 : period_min,
				       period_max, late, frames);
			} else {
				printk("t=%4lld s breathing OFF (idle, CPU released, frames=%u)\n",
				       k_uptime_get() / 1000, frames);
			}
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
