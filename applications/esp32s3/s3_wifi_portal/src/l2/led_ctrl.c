/*
 * led_ctrl —— 按键切换 RGB 灯的亮/灭（L2 服务层）
 *
 * 硬件：板上 KEY 在 IO45。板级 dts 把它定义成 button0（gpio-keys），
 * 本工程的 overlay 只覆盖 gpios 到 <&gpio1 13 GPIO_PULL_DOWN>，
 * 所以这里用 DT_ALIAS(sw0) 拿引脚，不硬编码脚号 —— 换板只改 overlay。
 *
 * 极性（overlay 里已写明是实测结论）：IO45 在板上被外部下拉，
 * 空闲=低、按下=高，所以 dts 里没有 ACTIVE_LOW，gpio_pin_get_dt()
 * 直接返回 1 就是按下。
 *
 * 为什么不用 gpio-keys / input 子系统：那要开 CONFIG_INPUT + CONFIG_GPIO_KEYS，
 * 而 prj.conf 一改就是全量重编（本工程 500 s 起）。这里只要一个"电平+消抖"，
 * 直接用 GPIO 驱动轮询，零 Kconfig 改动，改完只重编两三个文件。
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#include "led_ctrl.h"

LOG_MODULE_REGISTER(portal_led_ctrl, LOG_LEVEL_INF);

#if DT_HAS_ALIAS(sw0)
#if DT_NODE_HAS_STATUS(DT_ALIAS(sw0), okay)
#define HAS_KEY 1
static const struct gpio_dt_spec key = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
#endif
#endif

#ifndef HAS_KEY
#define HAS_KEY 0
#endif

/* 消抖窗口。采样节奏是 40 ms/帧，窗口取 30 ms：
 * 只要电平连续两帧一致就认，机械抖动（<10 ms）会被吃掉。 */
#define KEY_DEBOUNCE_MS 30

static atomic_t led_on = ATOMIC_INIT(1);   /* 默认亮：上电就该看得见状态 */
static bool inited;
static bool last_raw;                      /* 上一次采到的原始电平 */
static bool stable_pressed;                /* 已确认"按住"（用于只触发一次） */
static uint32_t raw_change_ms;             /* 原始电平最近一次变化的时刻 */

int led_ctrl_init(void)
{
	if (inited) {
		return 0;
	}

#if HAS_KEY
	if (!gpio_is_ready_dt(&key)) {
		LOG_ERR("KEY gpio not ready (port %s pin %u)",
			key.port->name, (unsigned)key.pin);
		return -ENODEV;
	}

	int rc = gpio_pin_configure_dt(&key, GPIO_INPUT);

	if (rc) {
		LOG_ERR("KEY pin configure failed (%d)", rc);
		return rc;
	}

	last_raw = gpio_pin_get_dt(&key) > 0;
	stable_pressed = last_raw;
	raw_change_ms = k_uptime_get_32();
	inited = true;
	LOG_INF("KEY ready on %s pin %u (idle level %d)",
		key.port->name, (unsigned)key.pin, last_raw ? 1 : 0);
	return 0;
#else
	LOG_WRN("no sw0 alias / button node: KEY toggle disabled");
	inited = true;
	return -ENODEV;
#endif
}

bool led_ctrl_poll(void)
{
#if HAS_KEY
	if (!inited) {
		led_ctrl_init();
	}

	uint32_t now = k_uptime_get_32();
	bool raw = gpio_pin_get_dt(&key) > 0;

	if (raw != last_raw) {
		last_raw = raw;
		raw_change_ms = now;
		return false;            /* 刚变化，等消抖窗口过去再判 */
	}

	if ((now - raw_change_ms) < KEY_DEBOUNCE_MS) {
		return false;
	}

	if (raw && !stable_pressed) {
		stable_pressed = true;
		return true;             /* 一次按下只报一次 */
	}

	if (!raw) {
		stable_pressed = false;
	}
#endif
	return false;
}

void led_ctrl_set(bool on)
{
	bool was = atomic_get(&led_on) != 0;

	atomic_set(&led_on, on ? 1 : 0);
	if (was != on) {
		LOG_INF("led: %s", on ? "on" : "off");
	}
}

void led_ctrl_toggle(void)
{
	led_ctrl_set(atomic_get(&led_on) == 0);
}

bool led_ctrl_is_on(void)
{
	return atomic_get(&led_on) != 0;
}
