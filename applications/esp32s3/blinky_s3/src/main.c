/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * 第三课的"点灯"：板载 RGB（WS2812，数据脚 IO48）循环变色。
 *
 * 说明：Up 主第三课用的是 gpio-leds（一颗普通 LED）。我们这块板没有普通 LED，
 * 只有一颗 WS2812，所以不能套 gpio-leds——必须走 led_strip 驱动。
 * 设备由 dts 的 chosen（zephyr,led-strip）指定，代码里不写引脚号。
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/led_strip.h>

#define STRIP_NODE DT_CHOSEN(zephyr_led_strip)
static const struct device *strip = DEVICE_DT_GET(STRIP_NODE);

static const struct led_rgb colors[] = {
	{ .r = 0x20, .g = 0x00, .b = 0x00 }, /* 红 */
	{ .r = 0x00, .g = 0x20, .b = 0x00 }, /* 绿 */
	{ .r = 0x00, .g = 0x00, .b = 0x20 }, /* 蓝 */
	{ .r = 0x00, .g = 0x00, .b = 0x00 }, /* 关 */
};

int main(void)
{
	unsigned int i = 0;

	if (!device_is_ready(strip)) {
		return -1;
	}

	while (1) {
		struct led_rgb px = colors[i % ARRAY_SIZE(colors)];

		led_strip_update_rgb(strip, &px, 1);
		i++;
		k_msleep(1000);
	}

	return 0;
}
