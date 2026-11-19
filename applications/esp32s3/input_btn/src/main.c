/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * 第四课：按键控制板载 RGB。
 *
 * - 键码来自 dts（buttons/button0 -> INPUT_KEY_0），代码只认键码，不认引脚（KEY 在 IO45）
 * - 灯来自 dts 的 chosen（zephyr,led-strip）-> 板载 WS2812，数据脚 IO48
 * - 为什么不用 gpio-leds：WS2812 是单线时序器件，必须走 led_strip 驱动
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/led_strip.h>
#include <zephyr/input/input.h>

#define STRIP_NODE DT_CHOSEN(zephyr_led_strip)
static const struct device *strip = DEVICE_DT_GET(STRIP_NODE);

static bool led_on;

static void apply_led(void)
{
	struct led_rgb px = led_on ? (struct led_rgb){ .r = 0x20, .g = 0x20, .b = 0x20 }
				   : (struct led_rgb){ .r = 0x00, .g = 0x00, .b = 0x00 };

	led_strip_update_rgb(strip, &px, 1);
}

static void input_call_back(struct input_event *event, void *user_data)
{
	ARG_UNUSED(user_data);

	/* 只处理按键类事件 */
	if (event->type != INPUT_EV_KEY) {
		return;
	}

	/* 默认双边沿：value=1 是按下，0 是松开。只响应按下 */
	if ((event->code == INPUT_KEY_0) && (event->value == 1)) {
		led_on = !led_on;
		apply_led();
	}
}

INPUT_CALLBACK_DEFINE(NULL, input_call_back, NULL);

int main(void)
{
	if (!device_is_ready(strip)) {
		return -1;
	}

	apply_led();

	while (1) {
		k_msleep(1000);
	}

	return 0;
}
