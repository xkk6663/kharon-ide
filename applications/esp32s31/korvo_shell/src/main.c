/*
 * Korvo-1 / Zephyr 阶段 2：shell 工程
 *
 * shell 后端在 CONFIG_SHELL=y 时由 SYS_INIT 自动启动，
 * main() 里不需要（也不应该）手动初始化它。
 */

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(korvo, LOG_LEVEL_INF);

static const char *const pinmap[] = {
	"ES8389  I2C   SDA=GPIO0  SCL=GPIO1   (i2c0)",
	"ES8389  I2S   MCLK=GPIO2 SCLK=GPIO3 LRCLK=GPIO4 SDout=GPIO5 SDin=GPIO6",
	"PA_CTRL       GPIO7      (功放使能, gpio0 pin7)",
	"WS2812        GPIO37     (RMT OUT0, gpio1 pin5)",
	"ADC BUTTON    GPIO42     (PLAY/SET/VOL-/VOL+ 电阻分压)",
	"BOOT          GPIO61     (gpio1 pin29)",
	"microSD       GPIO20..25 (SDIO, 与 SPI NAND 复用)",
};

static int cmd_pins(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	for (size_t i = 0; i < ARRAY_SIZE(pinmap); i++) {
		shell_print(sh, "%s", pinmap[i]);
	}
	return 0;
}

static int cmd_pa(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	const struct device *gpio = DEVICE_DT_GET(DT_NODELABEL(gpio0));

	if (!device_is_ready(gpio)) {
		shell_error(sh, "gpio0 not ready");
		return -ENODEV;
	}

	gpio_pin_configure(gpio, 7, GPIO_OUTPUT_ACTIVE);
	gpio_pin_set(gpio, 7, 1);
	shell_print(sh, "PA_CTRL(GPIO7) -> high (功放使能)");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_korvo,
	SHELL_CMD(pins, NULL, "打印 Korvo-1 官方管脚分配", cmd_pins),
	SHELL_CMD(pa, NULL, "拉高 PA_CTRL(GPIO7)", cmd_pa),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(korvo, &sub_korvo, "Korvo-1 板级辅助命令", NULL);

int main(void)
{
	printk("\n=== Korvo-1 Zephyr shell ===\n");
	printk("device list / i2c scan i2c0 / adc read / gpio get / kernel threads\n");
	printk("korvo pins  -> 管脚分配\n");
	LOG_INF("shell ready, build %s", CONFIG_BOARD_TARGET);
	return 0;
}
