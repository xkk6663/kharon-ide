/*
 * _template — 新建工程的骨架（第四课"新建"那一节的实物）
 *
 * 这个 main.c 只做三件事：
 *   1. 从设备树别名拿一个 gpio_dt_spec（对应教程里"led"那一行）
 *   2. 用 device_is_ready / gpio_is_ready_dt 判"驱动有没有真的进镜像"
 *   3. 打印横幅 + 定频心跳，证明工程跑起来了
 *
 * 注意：这里用的是 PA_CTRL（功放使能脚）。只配置成"不激活"，**不要 toggle**，
 * 否则喇叭会有爆音。要练点灯请用 WS2812（led_strip API），见 lesson-04 第三节。
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>

/* 别名来自 app.overlay 里的 my-pa -> pa_ctrl（app.overlay 又 include 了公共片段） */
#define PA_NODE DT_ALIAS(my_pa)
static const struct gpio_dt_spec pa = GPIO_DT_SPEC_GET(PA_NODE, gpios);

int main(void)
{
	int ret;

	printk("\n=== korvo_template 启动 ===\n");
	printk("board: %s\n", CONFIG_BOARD_TARGET);

	if (!gpio_is_ready_dt(&pa)) {
		/* 走到这里 = adc/gpio 驱动没进镜像，先查 prj.conf，不是查 dts */
		printk("PA_CTRL 设备未就绪（检查 CONFIG_GPIO=y 与 overlay 是否生效）\n");
		return 0;
	}

	ret = gpio_pin_configure_dt(&pa, GPIO_OUTPUT_INACTIVE);
	printk("PA_CTRL: port=%s pin=%u configure=%d\n", pa.port->name, pa.pin, ret);

	while (1) {
		printk("[tick] uptime=%u ms\n", k_uptime_get_32());
		k_msleep(1000);
	}

	return 0;
}
