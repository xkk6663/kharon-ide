/*
 * led_status.h —— 呼吸灯（L2）
 *
 * 颜色只由 link_state() 决定，相位只由内部的 breath_clock 决定；
 * 对时成功时由 time_sync 用 led_clock_observe() 喂一次观测。
 */
#ifndef LED_STATUS_H_
#define LED_STATUS_H_

#include <stdint.h>

void led_clock_start(void);
void led_clock_observe(int64_t mono_us, int64_t utc_us, int src, int32_t unc_us);

/* 临时点亮（云端 led_test 命令用）：color ∈ red/green/blue/purple/off，
 * ms 到期后自动回到"链路/OTA"状态色。返回 0 = 接受，-EINVAL = 颜色不认识。 */
int led_status_test(const char *color, int ms);

#endif /* LED_STATUS_H_ */
