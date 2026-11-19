/*
 * led_ctrl.h —— RGB 灯开关（L2）
 *
 * 板上的 KEY 按键按一下切换"呼吸灯亮 / 全灭"。灭灯是给人看的：
 * 夜里、桌面上、放床头时不希望那块 WS2812 一直闪。
 *
 * 只持有"亮还是灭"这一个布尔状态，不认识链路状态，也不认识 HTTP；
 * 按键消抖在 led_status 的 25 fps 帧里采样完成，不额外开线程。
 */
#ifndef LED_CTRL_H_
#define LED_CTRL_H_

#include <stdbool.h>

/* 配置按键 GPIO。可重复调用；没有按键节点或 GPIO 没就绪时返回负值，
 * 之后 poll 恒返回 false（按键功能静默缺席，灯照常亮）。 */
int led_ctrl_init(void);

/* 采样一次按键。返回 true 表示"这一帧检测到一次新的按下"（已消抖）。
 * 需要在固定节奏里调用（呼吸灯 25 fps 正好够）。 */
bool led_ctrl_poll(void);

void led_ctrl_set(bool on);
void led_ctrl_toggle(void);
bool led_ctrl_is_on(void);

#endif /* LED_CTRL_H_ */
