/*
 * led_status —— RGB 呼吸灯（红=未连接 / 蓝=连接中 / 绿=已连接 / 紫=OTA 进行中）
 *
 * L2 服务层。灯的颜色只由 link_state() 决定，本模块不认识 HTTP，也不认识凭据。
 * 板上的 KEY 可以整体关掉这块灯（开关状态在 led_ctrl 里，本模块只负责"灭=写黑帧"）。
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/led_strip.h>
#include <errno.h>
#include <string.h>

#include "portal_cfg.h"
#include "breath_sync.h"
#include "wifi_link.h"
#include "led_ctrl.h"
#include "led_status.h"
#include "ota.h"

LOG_MODULE_REGISTER(portal_led, LOG_LEVEL_INF);

#define STRIP_NODE   DT_ALIAS(led_strip)

/* -------------------------------------------------------------- 运行状态 */
static struct breath_clock g_breath_clk;

/* 云端 led_test 的临时覆盖：到期自动失效，不改变正常状态色逻辑。
 * 现场找板子/验证"命令真的到设备了"时用，5 s 足够肉眼确认。 */
static volatile int32_t g_test_until_ms;
static uint8_t g_test_rgb[3];

int led_status_test(const char *color, int ms)
{
	uint8_t r = 0, g = 0, b = 0;

	if (color == NULL) {
		return -EINVAL;
	}
	if (strcmp(color, "red") == 0) {
		r = 160;
	} else if (strcmp(color, "green") == 0) {
		g = 160;
	} else if (strcmp(color, "blue") == 0) {
		b = 160;
	} else if (strcmp(color, "purple") == 0) {
		r = 150;
		b = 175;
	} else if (strcmp(color, "off") != 0) {
		return -EINVAL;   /* "off" 落到这里就是全 0 */
	}

	g_test_rgb[0] = r;
	g_test_rgb[1] = g;
	g_test_rgb[2] = b;
	g_test_until_ms = (int32_t)k_uptime_get_32() + (int32_t)(ms > 0 ? ms : 5000);
	LOG_INF("led test: %s for %d ms", color, ms > 0 ? ms : 5000);
	return 0;
}

#if DT_HAS_ALIAS(led_strip)
static const struct device *const strip = DEVICE_DT_GET(STRIP_NODE);

/*
 * 原来这里是一个 2.4 s 的三角波：breathe(k_uptime_get_32(), 2400)。
 *
 * 两个问题：
 *   1) 三角波两端速度不为零，看起来像“折返”，不是“呼吸”；
 *   2) 它的自变量是本机开机时长。两台设备上电时刻不同 → 相位永远差一截；
 *      重启 → 相位归零；帧率不同 → 速度不同。
 *
 * 现在换成规范 v1.0：4 s 余弦 + gamma 2.0 的 256 项表，自变量是绝对 UTC。
 * 表字节相同（校验和 0x51521FF6）→ 算出的亮度就相同，跨设备可判定。
 * 帧率只影响采样密度，不影响轨迹：25 fps 每帧走 1% 相位。
 */

static void led_thread(void *a, void *b, void *c)
{
	uint32_t led_tick = 0U;      /* 日志节流：每 2 s 一行 */
	uint32_t led_fails = 0U;     /* 累计传输失败次数 */
	int led_lasterr = 0;
	bool led_was_on = true;      /* 上一帧是否在亮：灭灯只需写一次黑帧 */

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (1) {
		struct led_rgb px = {0};

		/* 按键在 25 fps 的帧里采样，不额外开线程：按一下切"亮/灭"。
		 * 灭灯时只写一次全黑帧，之后整个循环空转 —— 既省 CPU，也不会
		 * 因为下一帧又算颜色而把灯点亮。 */
		if (led_ctrl_poll()) {
			led_ctrl_toggle();
		}

		if (!led_ctrl_is_on()) {
			if (led_was_on) {
				struct led_rgb black = {0};

				if (device_is_ready(strip)) {
					(void)led_strip_update_rgb(strip, &black, 1);
				}
				led_was_on = false;
				LOG_INF("led: strip blanked (KEY off)");
			}
			k_msleep(40);
			continue;
		}
		led_was_on = true;

		/* 每帧从时间重算，不留累加状态：掉帧/卡顿/重启后下一帧自动回到正确相位 */
		int64_t mono = (int64_t)k_uptime_get() * 1000;
		int64_t utc = breath_clock_now_us(&g_breath_clk, mono);
		uint64_t now_u = (utc > 0) ? (uint64_t)utc : 0ull;
		uint8_t lvl_grade = breath_clock_level(&g_breath_clk, mono);
		/* 未授时（等级 0）时把亮度砍半：让“没对上表”在视觉上一眼可辨，
		 * 而且 gain 不参与相位，所以半亮不会破坏任何一致性（规范 §6.5） */
		uint8_t gain = breath_gain_guard(100, lvl_grade);
		uint8_t base[3] = {0, 0, 0};
		uint8_t out[3] = {0, 0, 0};

		/* OTA 进行中优先显示紫色（R+B）。判断依据直接读 OTA 快照里的 stage：
		 *   recv   = 正在收字节
		 *   verify = 收完在回读校验
		 *   ready  = 已写好、等切槽重启
		 * 这三种状态下灯不再是"联网绿"，否则升级时看起来和平时一模一样，
		 * 现场根本分不清"卡住了"还是"正在写"。灯仍然走同一套呼吸包络，
		 * 只换基色 —— 相位/节奏与规范保持一致。 */
		bool ota_on = false;

		{
			const char *st = ota_snapshot_json();

			ota_on = (strstr(st, "\"stage\":\"recv\"") != NULL) ||
				 (strstr(st, "\"stage\":\"verify\"") != NULL) ||
				 (strstr(st, "\"stage\":\"ready\"") != NULL);
		}

		if ((int32_t)k_uptime_get_32() < g_test_until_ms) {
			/* 云端 led_test 覆盖窗口内：只换基色，呼吸包络照旧 */
			base[0] = g_test_rgb[0];
			base[1] = g_test_rgb[1];
			base[2] = g_test_rgb[2];
		} else if (ota_on) {
			base[0] = 150;   /* R */
			base[2] = 175;   /* B */
		} else {
			switch (link_state()) {
			case LINK_CONNECTED:               /* 绿：原来 80 上限偏暗，提到 160 */
				base[1] = 160;
				base[0] = 4;
				break;
			case LINK_CONNECTING:              /* 蓝 */
				base[2] = 160;
				break;
			default:                           /* 红 */
				base[0] = 160;
				break;
			}
		}

		/* 通道层：同一个亮度标量铺到 n 个通道。
		 * 同步的是亮度而不是灯 —— 换成单色 LED 只需把 n 填 1，相位一字不改。 */
		breath_channels(now_u, base, 3, gain, out);

		px.r = out[0];
		px.g = out[1];
		px.b = out[2];

		if (device_is_ready(strip)) {
			int ret = led_strip_update_rgb(strip, &px, 1);

			/* 返回值必须看：RMT 传输失败是静默的，忽略它灯就冻在上一帧。 */
			if (ret != 0) {
				led_fails++;
				led_lasterr = ret;
				if ((led_fails & 0x3FU) == 1U) {
					LOG_WRN("led update failed (%d), total %u",
						ret, (unsigned)led_fails);
				}
			}
		}

		/* 每 2 s 一行，用来判断线程在跑、包络在动、传输有没有失败。
		 * grade 是关键：0=未授时（半亮），3=≤25 ms 严格同相。 */
		if (++led_tick >= 50U) {
			led_tick = 0U;
			LOG_INF("led: state=%d lvl=%u gain=%u grade=%u err=%d fails=%u",
				link_state(), (unsigned)breath_envelope(now_u), (unsigned)gain,
				(unsigned)lvl_grade, led_lasterr, (unsigned)led_fails);
		}

		k_msleep(40);                      /* 25 fps */
	}
}

/* 优先级 7 是最低一档，Wi-Fi 收包忙时容易被挤；降到 2 留出余量。
 * 栈 1024 -> 2048 -> 4096：
 * 2048 实测会被打断。Xtensa 上中断跑在当前线程的栈上，Telnet 建连那一下会带来
 * 一串 TCP/WiFi 中断，叠加 led_strip_update_rgb -> pulse_io/RMT 的调用链后，
 * 栈顶直接越过 _k_thread_stack_led_tid 下沿 240 B 写进相邻的 dns_stack，
 * 异常帧落进别人栈里，于是只看到 "Invalid BSA" + Current thread: led_tid。
 * 4096 留出约 1.8 KB 余量给中断嵌套。
 */

/* ===================== 对外接口 ===================== */
/* 时钟对象留本文件（static）：外面只表达"现在自由跑"或"这是一次授时观测"，
 * 拿不到 breath_clock 本身，也就改不坏它的相位。 */
void led_clock_start(void)
{
	breath_clock_init_freerun(&g_breath_clk, (int64_t)k_uptime_get() * 1000);
}

void led_clock_observe(int64_t mono_us, int64_t utc_us, int src, int32_t unc_us)
{
	breath_clock_observe_src(&g_breath_clk, mono_us, utc_us,
				 (uint8_t)src, unc_us);
}

/* 优先级 2（不是最低的 7）：Wi-Fi 收包忙时低优先级线程会被挤，呼吸灯就"卡帧"。
 * 栈 4096：2048 实测会被中断嵌套（Xtensa 上中断跑在当前线程栈上）踩穿，
 * 4096 留约 1.8 KB 给 TCP/Wi-Fi 中断 + led_strip_update_rgb -> RMT 调用链。 */
K_THREAD_DEFINE(led_tid, 4096, led_thread, NULL, NULL, NULL, 2, 0, 0);
#endif /* DT_HAS_ALIAS(led_strip) */