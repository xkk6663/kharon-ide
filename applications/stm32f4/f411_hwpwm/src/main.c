/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * f411_hwpwm —— STM32F411CEU6（WeAct Black Pill V2.0）硬件 PWM 对照实验：
 *   PB6 = TIM4_CH1（pwm4 通道 1）：sin² 呼吸
 *   PB7 = TIM4_CH2（pwm4 通道 2）：反相（period - duty），接第二只灯能看到跷跷板
 *   PC13 = 板载灯：只在开机闪三下（证明板子跑起来了）
 *   PA0  = 按键：按一下停/继续呼吸（与 f411_breath 行为一致）
 *   USART1 (PA9/PA10) → DAP-Link CDC：每 2 s 打一行统计
 *
 * 和 f411_breath 的对照点（这也是这一轮的结论）：
 *   - 软件 PWM：1 kHz 帧靠 k_busy_wait 撑，CPU 100% 被占，占空比分辨率 1 µs
 *   - 硬件 PWM：CPU 只负责"改比较值"，帧由 TIM4 自己跑；同样的 1 kHz 只需要每 47 ms
 *     写两次寄存器，主循环剩下全是 k_msleep(1)
 *
 * 引脚不由源码指定：PB6/PB7 是板级 dts 里 pwm4 的 pinctrl 决定的
 * （&timers4 { pwm4: pwm { pinctrl-0 = <&tim4_ch1_pb6 &tim4_ch2_pb7>; } }）。
 * 换一块 pwm 通道映射不同的板子，改 dts 即可，这份 main.c 不用动。
 *
 * 接线（任选其一，切勿把 PB6 和 PB7 短接在一起）：
 *   A) PB6 → 220~1kΩ → LED 阳极，LED 阴极 → GND
 *   B) 万用表 DC 电压档：红笔 PB6、黑笔 GND，电压会随呼吸在 0~3.3 V 之间摆
 *   C) 示波器：PB6 对 GND，1 kHz 方波，占空比 0~100% 缓变
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>
#include <zephyr/version.h>

/* 1 kHz：与 f411_breath 的软 PWM 帧率一致，方便对比 */
#define PWM_FREQ_HZ 1000
#define PWM_PERIOD_NS 1000000U /* 1 kHz = 1000 us = 1,000,000 ns */

#define CH_A 1 /* TIM4_CH1 → PB6 */
#define CH_B 2 /* TIM4_CH2 → PB7 */

/* 呼吸曲线：sin²，0→1000→0 一个完整周期，单位 µs（占空比 = duty/1000）。
 * 与 f411_breath 用的是同一张表，这样"软/硬两条路径"的亮度曲线可以直接对比。 */
static const uint16_t duty_lut_us[] = {
	0, 2, 10, 22, 38, 59, 84, 113, 146, 183, 222, 264, 309, 355, 402, 451,
	500, 549, 598, 645, 691, 736, 778, 817, 854, 887, 916, 941, 962, 978, 990, 998,
	1000, 998, 990, 978, 962, 941, 916, 887, 854, 817, 778, 736, 691, 645, 598, 549,
	500, 451, 402, 355, 309, 264, 222, 183, 146, 113, 84, 59, 38, 22, 10, 2, 0
};

/* 每一档保持 47 帧 → 65 × 47 ≈ 3055 帧 ≈ 3.05 s 一次完整呼吸 */
#define FRAMES_PER_STEP 47
#define KEY_DEBOUNCE_FRAMES 20 /* 1 kHz 采样 → 20 ms 去抖 */
#define REPORT_PERIOD_S 2

static const struct device *pwm = DEVICE_DT_GET(DT_NODELABEL(pwm4));
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec key = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

/* 出错只报一次：否则 1 kHz 循环会把串口刷满（本轮实测踩过，-134 刷了 200 行） */
static bool pwm_err_reported;

static void report_pwm_error(int ret)
{
	if (!pwm_err_reported) {
		printk("pwm_set failed: %d (only printed once)\n", ret);
		pwm_err_reported = true;
	}
}

/* 两路一起设：ch1 = duty，ch2 = 反相 duty（1000 - duty） */
static int set_pair(uint16_t duty_us)
{
	int ret;

	ret = pwm_set(pwm, CH_A, PWM_PERIOD_NS,
		      (uint32_t)duty_us * 1000U, PWM_POLARITY_NORMAL);
	if (ret < 0) {
		return ret;
	}
	return pwm_set(pwm, CH_B, PWM_PERIOD_NS,
		       (uint32_t)(1000U - duty_us) * 1000U, PWM_POLARITY_NORMAL);
}

/* 开机自检：先证明 pwm4 设备就绪、时基是多少，再闪三下板载灯 */
static void self_test(void)
{
	uint64_t pps = 0;
	int ret;

	if (!device_is_ready(pwm)) {
		printk("self-test: pwm4 NOT ready\n");
		return;
	}

	ret = pwm_get_cycles_per_sec(pwm, CH_A, &pps);
	if (ret == 0) {
		printk("self-test: pwm4 ready, cycles/sec=%llu -> 1 个周期 %u cycles\n",
		       (unsigned long long)pps, (uint32_t)(pps / PWM_FREQ_HZ));
	} else {
		printk("self-test: pwm_get_cycles_per_sec failed: %d\n", ret);
	}

	/* 两路各给 50%，让人先用万用表/示波器确认"有波出来" */
	(void)set_pair(500);
	k_msleep(200);

	/* 寄存器回读：不靠示波器也能证明"参数真的进了 TIM4"，而不是只被 API 收下。
	 * TIM_TypeDef 的关键偏移（RM0383）：PSC 0x28、ARR 0x2C、CCR1 0x34、CCR2 0x38。
	 * 期望：PSC=1（分频 2）→ 时基 48 MHz；ARR=47999 → 1 kHz；CCR1=24000、CCR2=24000（各 50%）。 */
	{
		uintptr_t tim = DT_REG_ADDR(DT_NODELABEL(timers4));

		printk("self-test: TIM4 PSC=%u ARR=%u CCR1=%u CCR2=%u\n",
		       *(volatile uint32_t *)(tim + 0x28),
		       *(volatile uint32_t *)(tim + 0x2C),
		       *(volatile uint32_t *)(tim + 0x34),
		       *(volatile uint32_t *)(tim + 0x38));
	}

	/* 板载灯闪三下：证明烧进去的是这块板、不是别的镜像 */
	for (int i = 0; i < 3; i++) {
		gpio_pin_set_dt(&led, 1); /* ACTIVE_LOW → 逻辑 1 = 引脚拉低 = 亮 */
		k_msleep(80);
		gpio_pin_set_dt(&led, 0);
		k_msleep(120);
	}
	printk("self-test: PC13 blink 3x done\n");
}

int main(void)
{
	uint32_t frames = 0;
	uint32_t frame_in_step = 0;
	uint32_t step = 0;
	int64_t last_report_ms;
	int key_prev = 0;
	int key_count = 0;
	bool breathe = true;
	int ret;

	printk("\n=== f411_hwpwm (TIM4 hardware PWM) ===\n");
	printk("board   : blackpill_f411ce/stm32f411xe\n");
	printk("kernel  : %s\n", KERNEL_VERSION_STRING);
	printk("pwm     : pwm4 ch1=PB6 ch2=PB7(inverted), %u Hz\n", PWM_FREQ_HZ);
	printk("wiring  : PB6 -> (220~1k) -> LED+ , LED- -> GND ; 别把 PB6/PB7 短接\n");

	if (!gpio_is_ready_dt(&led) || !gpio_is_ready_dt(&key)) {
		printk("gpio not ready\n");
		return 0;
	}
	(void)gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	(void)gpio_pin_configure_dt(&key, GPIO_INPUT);

	self_test();

	ret = set_pair(duty_lut_us[0]);
	if (ret < 0) {
		report_pwm_error(ret);
	}

	printk("breathing via TIM4 (press KEY/PA0 to toggle)\n");

	last_report_ms = k_uptime_get();

	while (true) {
		/* 按键：1 kHz 采样 + 连续 20 帧一致才算一次跳动（与 f411_breath 同法） */
		int key_state = gpio_pin_get_dt(&key);

		if (key_state != key_prev) {
			if (++key_count >= KEY_DEBOUNCE_FRAMES) {
				key_prev = key_state;
				key_count = 0;
				if (key_state) { /* 逻辑 1 = 按下（ACTIVE_LOW） */
					breathe = !breathe;
					step = 0;
					frame_in_step = 0;
					(void)set_pair(breathe ? duty_lut_us[0] : 0);
					printk("KEY pressed -> breathing %s\n",
					       breathe ? "ON" : "OFF");
				}
			}
		} else {
			key_count = 0;
		}

		if (breathe) {
			if (++frame_in_step >= FRAMES_PER_STEP) {
				frame_in_step = 0;
				step = (step + 1) % ARRAY_SIZE(duty_lut_us);
				ret = set_pair(duty_lut_us[step]);
				if (ret < 0) {
					report_pwm_error(ret);
				}
			}
		}

		frames++;

		if (k_uptime_get() - last_report_ms >= REPORT_PERIOD_S * 1000) {
			/* 每行都带 TIM4 寄存器回读：不用复位、不用示波器也能看到
			 * "duty 变了 → CCR 跟着变 → ARR 钉在 47999（1 kHz）" 这条因果链。 */
			uintptr_t tim = DT_REG_ADDR(DT_NODELABEL(timers4));
			uint32_t psc = *(volatile uint32_t *)(tim + 0x28);
			uint32_t arr = *(volatile uint32_t *)(tim + 0x2C);
			uint32_t ccr1 = *(volatile uint32_t *)(tim + 0x34);
			uint32_t ccr2 = *(volatile uint32_t *)(tim + 0x38);

			if (breathe) {
				printk("t=%4lld s duty=%4u us frames=%u psc=%u arr=%u ccr1=%u ccr2=%u\n",
				       k_uptime_get() / 1000, duty_lut_us[step], frames,
				       psc, arr, ccr1, ccr2);
			} else {
				printk("t=%4lld s breathing OFF (duty=0, TIM4 still running)\n",
				       k_uptime_get() / 1000);
			}
			last_report_ms = k_uptime_get();
		}

		k_msleep(1); /* 硬件 PWM 的帧由 TIM4 自己跑，这里只值 1 ms 一拍 */
	}
	return 0;
}
