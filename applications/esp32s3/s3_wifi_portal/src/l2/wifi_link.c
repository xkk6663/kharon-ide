/*
 * wifi_link —— STA 链路状态机（g_state 的唯一写者）
 *
 * L2 服务层。本文件拥有"目标 SSID / 真实连上的 SSID / IP / 状态文案 / 失败原因 /
 * 两套重连计数 / RSSI 历史"，对外只暴露 struct link_view 与少量动作函数。
 * 事件由 main.c 的分派器通过 link_ev_* 推进；本模块不注册 net_mgmt（避免
 * 多个模块各自监听同一事件）。
 */
#include <string.h>
#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/net/dhcpv4.h>

#include "portal_cfg.h"
#include "wifi_link.h"
#include "wifi_scan.h"
#include "ota_cloud.h"
#include "portal_ap.h"
#include "cred.h"
#include "netcheck.h"
#include "ops.h"
#include "ota.h"

LOG_MODULE_REGISTER(portal_link, LOG_LEVEL_INF);


static volatile int g_state = LINK_IDLE;
static char g_ssid[WIFI_SSID_MAX_LEN + 1];
static size_t g_ssid_len;
/* "实际连着的" SSID，和 g_ssid（本次连接的目标）分开存。
 * 实测：点一个不存在的 SSID 时，STA 往往还挂在原来那条上，而 g_ssid 已经被
 * 改成了目标名。若页面照 g_ssid 显示，就会出现"已连接 NoSuchAP-ZZ"这种假信息。
 * 所以：g_ssid = 目标；g_link_ssid = 真的连上的那个。 */
static char g_link_ssid[WIFI_SSID_MAX_LEN + 1];
static char g_psk[65];
static size_t g_psk_len;
static char g_ip[NET_IPV4_ADDR_LEN] = "0.0.0.0";
static char g_gw[NET_IPV4_ADDR_LEN] = "-";
/* 96 字节。原来只有 48，因为失败提示只够放一个码（"connect failed 4"），
 * 用户看不出是密码错还是信道被踢。现在要放整句原因：
 * "connect failed 4 · 关联超时（信道冲突或信号弱）"。 */
static char g_msg[96] = "waiting for credentials";
/* "上一次为什么没连上"，和 g_msg（当前状态文案）分开存。
 * 原因：STA 往往还连着原来的网络（实测连 bogus SSID 时 IP/页面都还在），
 * 这时 g_msg 必须回到 "connected"，否则页面显示"未连接"却还能上网；
 * 但这样失败原因就丢了，所以单独留一份给页面读。 */
static char g_last_err[96];

/* 掉线那一刻 wifi_rssi() 已经返回 0 了，所以"断线时的信号强度"只能靠定时采样
 * 留住的最后一个值。它决定问题是"边缘信号掉线"还是"信号很好却被踢"——
 * 这两种修法完全不同（一个要调省电/漫游，一个要查路由器和信道）。 */
static int g_last_rssi;

static struct net_if *sta_iface;
#define RSSI_HIST_LEN   60
#define RSSI_SAMPLE_MS  10000

static int16_t g_rssi_hist[RSSI_HIST_LEN];
static uint8_t g_rssi_head;   /* 下一个写入位置 */
static uint8_t g_rssi_cnt;    /* 已采到多少个点（未装满时只输出前 cnt 个） */

/* --------------------------------------------------- 凭据持久化（NVS/settings）
 * 用 settings 子系统把 SSID/密码写进板子自带的 storage 分区
 * （esp32s3_devkitc 的 partitions_0x0_amp_4M: 0x3b0000，192 KB）。
 * settings 的键是 "portal/wifi"，值是一张定长凭据表（CRED_MAX 条）。
 *
 * 表是"最近使用优先"：连上成功的网络会被移到 items[0]，上电自动连它。
 * 这样用户连过几个网络都能被记住，网页里能列出来、能直接点着连、也能忘记。
 */
static uint8_t g_cred_try;               /* "逐条轮"当前试到第几条（下标 g_creds.items[]） */
static uint8_t g_cred_rounds;            /* 本轮已经换过几次网，到 count-1 就说明轮完一圈 */
static bool g_auto_reconnect;            /* 本次连接是不是"上电自动重连" */
static uint8_t g_auto_attempts;          /* 自动重连已尝试几次 */

/* 信道对齐走到第几步（见 portal_cfg.h 的 ALIGN_STEP_MAX）：
 *   0 = 直接连（AP 处于 auto，交给驱动自己跟随）
 *   1 = 已经为这次失败扫过一次信道
 *   2 = 已经把 AP 钉到目标信道
 * 封顶 2 是为了防止"扫 → 连 → 败 → 再扫"无限循环。连上或换目标时归零。 */
static uint8_t g_align_step;
static uint32_t g_scan_waited_ms;

/* 运行期掉线重连的状态 */
static bool g_ever_connected;            /* 上电以来成功连上过至少一次 */
static bool g_rt_reconnect;              /* 正在跑运行期掉线重连 */
static uint8_t g_rt_attempts;            /* 运行期重连已试几次 */
static uint32_t g_reconnects;            /* 运行期重连成功次数（网页可见） */
static uint32_t g_connected_at;          /* 本次连上的时间戳（0 = 当前没连着） */
static uint32_t g_link_held_s;           /* 主动断开前算好的"这条链路活了多久" */
static bool g_user_disconnect;           /* 用户主动断开：不要触发运行期重连 */

static void retry_work_handler(struct k_work *work);

static K_WORK_DELAYABLE_DEFINE(retry_work, retry_work_handler);

/* 在表里找一条凭据，返回下标；找不到返回 -1 */
static void cred_apply(uint8_t idx)
{
	const struct cred_item *it = cred_at(idx);

	if (it == NULL) {
		return;
	}

	g_cred_try = idx;
	memcpy(g_ssid, it->ssid, it->ssid_len + 1);
	g_ssid_len = it->ssid_len;
	memcpy(g_psk, it->psk, it->psk_len + 1);
	g_psk_len = it->psk_len;

	LOG_INF("try saved network [%u] \"%s\"", (unsigned)idx, g_ssid);
}

/* 换到下一条已保存网络，返回 true 表示"还有下一条可试"。
 * 用 g_cred_rounds 记录"本轮已换过几次"，到 count-1 就说明轮完一圈。
 * 早先用"下标转回 0 就停"判断，起点不在 0 时会少轮一次，这里改成显式计数。 */
static bool cred_advance(void)
{
	if (cred_count() < 2 || g_cred_rounds >= (uint8_t)(cred_count() - 1)) {
		return false;
	}

	g_cred_rounds++;
	cred_apply((uint8_t)((g_cred_try + 1U) % cred_count()));

	return true;
}

/* 把表头那条填进当前连接参数（g_ssid / g_psk），供上电自动重连使用。
 * 旧固件是 cred_settings_set() 顺手做这件事的；迁到 wifi_credentials 之后旧键
 * 被删掉，这里必须自己补一次，否则重启时 g_ssid_len == 0，明明存着网络却
 * 永远不会自动连（实测过一次：迁移后 ping 不通、页面一直"未连接"）。 */
static void cred_pick_default(void)
{
	if (g_ssid_len > 0 || cred_count() == 0) {
		return;
	}

	const struct cred_item *it = cred_at(0);

	if (it == NULL) {
		return;
	}

	memcpy(g_ssid, it->ssid, it->ssid_len + 1);
	g_ssid_len = it->ssid_len;
	memcpy(g_psk, it->psk, it->psk_len + 1);
	g_psk_len = it->psk_len;

	LOG_INF("default network for boot: \"%s\"", g_ssid);
}

/* 一次性迁移：旧固件把整张表存在 settings 的 "portal/wifi" 键里，
 * settings_load() 已经回调 cred_settings_set() 把它读进了 g_creds。
 * 若官方存储还是空的，就把它搬过去再删掉旧键，用户升级不用重新配网。 */
static K_MUTEX_DEFINE(conn_lock);

static void connect_work_handler(struct k_work *work);
static void conn_timeout_handler(struct k_work *work);

static K_WORK_DEFINE(connect_work, connect_work_handler);

static K_WORK_DELAYABLE_DEFINE(conn_timeout_work, conn_timeout_handler);

static void ap_reopen_handler(struct k_work *work);
static void connect_defer_handler(struct k_work *work);

static void align_work_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(align_work, align_work_handler);

static K_WORK_DELAYABLE_DEFINE(connect_defer_work, connect_defer_handler);

/* ---------------------------------------------------- 扫描专用工作队列
 *
 * 为什么不能直接调 wifi_scan_start()：net_mgmt(NET_REQUEST_WIFI_SCAN) 在
 * ESP32 驱动里是**阻塞**的（全信道扫一轮实测 ~10 s 才返回）。系统工作队列
 * 是单线程，放进去找就等于把 LED 刷新、RSSI 采样、连接重试全体冻 10 s——
 * 实测现象就是"触发换网后串口一行日志都不出了，但 HTTP 还能通"。
 *
 * 所以扫描单独一条队列 + 独立栈：堵也只堵它自己。 */
K_THREAD_STACK_DEFINE(align_scan_stack, 2048);

static struct k_work_q align_scan_wq;
static bool align_scan_wq_ready;

static void align_scan_kick_handler(struct k_work *work);
static K_WORK_DEFINE(align_scan_kick, align_scan_kick_handler);

static void align_scan_wq_init_once(void)
{
	if (align_scan_wq_ready) {
		return;
	}

	k_work_queue_start(&align_scan_wq, align_scan_stack,
			   K_THREAD_STACK_SIZEOF(align_scan_stack), K_PRIO_PREEMPT(2), NULL);
	k_thread_name_set(&align_scan_wq.thread, "align_scan");
	align_scan_wq_ready = true;
}

static void align_scan_kick_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	/* 这里阻塞 ~10 s 是预期行为，只堵 align_scan 这条队列 */
	LOG_INF("align: scanning all channels (blocking, own workqueue)");
	wifi_scan_start();
	LOG_INF("align: scan request returned");
}

/* 这一版驱动对 AP 接口返回的 channel 只是"配置值"（我们配 0=自动），拿不到真实
 * 信道；在系统工作队列里同步等 net_mgmt 也有卡死风险，所以只读内存状态。
 * 0 表示"自动、位置不确定"（上电默认 ch1，或跟着 STA 走）。 */
int portal_disconnect(void)
{
	if (sta_iface == NULL) {
		return -ENODEV;
	}

	k_work_cancel_delayable(&retry_work);
	k_work_cancel_delayable(&conn_timeout_work);

	g_user_disconnect = true;
	g_rt_reconnect = false;
	g_auto_reconnect = false;
	g_rt_attempts = 0;
	/* 主动断开时这条链路活了多久，要在清时间戳之前算出来：
	 * 否则 DISCONNECT 事件到达时 g_connected_at 已经是 0，账本里的
	 * "held" 永远记成 0 s，等于把"连了很久才被断开"和"刚连上就断"混成一类。 */
	g_link_held_s = g_connected_at ? (k_uptime_get_32() - g_connected_at) / 1000u : 0;
	g_connected_at = 0;

	int ret = net_mgmt(NET_REQUEST_WIFI_DISCONNECT, sta_iface, NULL, 0);

	if (ret == 0) {
		g_state = LINK_IDLE;
		strcpy(g_ip, "0.0.0.0");
		g_link_ssid[0] = '\0';
		strcpy(g_msg, "disconnected by user");
		LOG_INF("STA disconnect requested by user");
	} else {
		LOG_WRN("STA disconnect request failed (%d)", ret);
		g_user_disconnect = false;
	}

	return ret;
}

/* 运行期掉线重连的退避：3 s → 6 s → 12 s → 24 s 封顶，最多 RT_RETRY_MAX 次。
 * 全部失败就把 g_rt_reconnect 清掉，灯停在红灯，等网页重新配网或复位。 */
static void schedule_runtime_retry(void)
{
	if (!g_rt_reconnect) {
		return;
	}

	if (g_rt_attempts >= RT_RETRY_MAX) {
		/* 当前这条用满了：换一条已保存网络，退避从头算。
		 * cred_advance() 轮完一圈就返回 false，所以不会在两条都不稳定的
		 * 网络之间无限来回跳。 */
		if (cred_advance()) {
			g_rt_attempts = 0;
			snprintf(g_msg, sizeof(g_msg), "switch to \"%s\"", g_ssid);
			LOG_INF("runtime: switching to saved network \"%s\"", g_ssid);
			k_work_reschedule(&retry_work, K_MSEC(RT_RETRY_BASE_MS));
			return;
		}

		g_rt_reconnect = false;
		g_state = LINK_IDLE;
		snprintf(g_msg, sizeof(g_msg), "reconnect gave up after %u tries",
			 (unsigned)g_rt_attempts);
		LOG_WRN("runtime reconnect gave up after %u tries",
			(unsigned)g_rt_attempts);
		return;
	}

	uint32_t delay = RT_RETRY_BASE_MS << (g_rt_attempts > 3U ? 3U : g_rt_attempts);

	if (delay > RT_RETRY_CAP_MS) {
		delay = RT_RETRY_CAP_MS;
	}

	g_rt_attempts++;
	snprintf(g_msg, sizeof(g_msg), "reconnecting %u/%u in %us",
		 (unsigned)g_rt_attempts, RT_RETRY_MAX, (unsigned)(delay / 1000U));
	LOG_INF("runtime reconnect %u/%u, retry in %u ms",
		(unsigned)g_rt_attempts, RT_RETRY_MAX, (unsigned)delay);

	k_work_reschedule(&retry_work, K_MSEC(delay));
}

/* 前置声明：connect_failed_now() 要按"实际链路还在不在"决定状态，
 * 而 wifi_rssi() 的定义在后面（status_handler 那边）。 */
int wifi_rssi(void);

/* 把 ESP 的连接失败 reason 翻成"人话"。
 *
 * wifi_status.status 是驱动直接透传的 ESP reason code，页面上只写
 * "connect failed 4" 的话没人知道该怎么办。下面这些码是 ESP-IDF
 * `wifi_err_reason_t` 里的定义，本项目实测见到的是 1 / 3 / 4：
 *   1  = UNSPECIFIED（多半是没在超时内完成，信号弱或信道被占）
 *   3  = AUTH_LEAVE（认证阶段被踢，密码错最典型）
 *   4  = ASSOC_EXPIRE（关联阶段超时，本项目里是单射频信道冲突）
 * 15/204 是四次握手超时，201 是扫不到 AP，202/203 是认证/关联被拒——
 * 路由器换了加密方式或信号太弱时会落到这几个。 */
static const char *reason_text(int code)
{
	switch (code) {
	case 0:   return "成功";
	case 1:   return "路由器没响应（信号弱，或 2.4G 信道被占）";
	case 2:   return "认证超时（密码或加密方式不匹配）";
	case 3:   return "被路由器拒绝（最常见的原因：密码不对）";
	case 4:   return "关联超时（单射频信道冲突，或信号太弱）";
	case 5:   return "路由器拒绝接入（连接数已满）";
	case 6:
	case 7:
	case 8:
	case 9:   return "认证/关联状态异常（路由器侧问题）";
	case 15:  return "四次握手超时（密码不对最常见）";
	case 17:  return "扫描不到这个网络（SSID 打错，或超出覆盖）";
	case 201: return "扫描不到这个网络（SSID 打错，或距离太远）";
	case 202: return "认证被拒（密码不对，或路由器不认这个加密方式）";
	case 203: return "关联被拒（路由器拒绝接入）";
	case 204: return "四次握手超时（密码不对最常见）";
	case 205: return "连接失败（路由器侧原因）";
	default:  return NULL;
	}
}

/* 一次连接尝试失败（超时，或 CONNECT_RESULT 的 status 非 0）的统一出口。
 *
 * status 1 / 4 的物理含义都是"STA 没在目标网络的信道上完成关联"。单射频
 * APSTA 下这是唯一能自救的故障，所以单独给它一条阶梯：
 *
 *   第 0 次失败 → 0.5 s 便宜重试。这类失败常见的真相是"AP 刚起、驱动还没
 *                 扫完第一轮"，0.5 s 后自己就好了；为此先付 10 s 扫描的钱
 *                 不值（改造前实测第一次失败只花了 0.61 s 就返回）。
 *   第 1 次失败 → 缓存里知道目标信道，而 AP 被钉在别的信道（或者 AP 处于
 *                 auto 却反复连不上）→ 把 AP 搬过去。2.4 s，换来一次真对齐。
 *   缓存里压根没有目标信道 → 只有此时才付全信道扫描（~10 s），扫完由
 *                 align_work 接力重连。
 *
 * 阶梯最多走一遍（ALIGN_STEP_MAX），之后一律回落到"上电逐条轮 / 运行期
 * 退避"，不会出现"扫 → 连 → 败 → 再扫"的死循环。 */
static void connect_failed_now(int status)
{
	/* 这条阶梯只在 **开机自动重连** 时走。
	 *
	 * 为什么要按场景分开：开机重连是无人值守的，多花几秒换一次真对齐是净赚；
	 * 而运行期（网页换网 / 掉线重连）在失败后立刻再发一次 CONNECT，实测会把
	 * 整个网络栈卡死 —— 146 的真机现场：切到不存在的 501A 报 status 4，
	 * 500 ms 后重试，之后 HTTP 与串口日志同时停、设备彻底失联，只能 USB 复位。
	 * 所以运行期退回改造前的行为（3 s 退避 + 保持原链路），不做对齐。 */
	if (g_auto_reconnect && (status == 1 || status == 4)) {
		uint8_t pinned = ap_pinned_channel();
		uint8_t target = ap_channel_of_ssid(g_ssid);

		if (g_align_step == 0) {
			/* 第一档只重试、不扫：这类失败常见的真相是"AP 刚起、驱动还没
			 * 扫完第一轮"。status 1 实测 0.6 s 就返回，等 0.5 s 就够；
			 * status 4 是关联超时（已经耗了 ~2.8 s），别紧接着再发
			 * CONNECT，按正常 3 s 间隔来。 */
			uint32_t delay = (status == 1) ? AUTO_RETRY_FIRST_MS
						       : AUTO_RETRY_DELAY_MS;

			g_align_step = 1;
			LOG_INF("connect failed %d -> retry in %u ms", status, (unsigned)delay);
			k_work_reschedule(&retry_work, K_MSEC(delay));
			return;
		}

		/* 知道目标网络在哪个信道，而 AP 不在那儿（被钉错，或 auto 也没跟随
		 * 上）→ 主动搬。搬完 ap_retarget 会自己接力发起连接。 */
		if (target != 0 && (pinned == 0 || pinned != target)) {
			g_align_step = ALIGN_STEP_MAX;
			LOG_INF("align: ap ch=%u(pinned=%u), \"%s\" ch=%u -> retarget",
				(unsigned)ap_current_channel(), (unsigned)pinned, g_ssid,
				(unsigned)target);
			ap_retarget(target);
			return;
		}

		/* 缓存里没有目标网络：既不知道信道，也就无从对齐。扫一次把信道问
		 * 出来 —— 这是唯一"必须先扫"的分支（原死锁就卡在这里：缓存空 →
		 * 不搬信道 → 关联超时 → 仍是空缓存）。 */
		if (target == 0 && g_align_step < ALIGN_STEP_MAX) {
			g_align_step = ALIGN_STEP_MAX;
			scan_invalidate();      /* 旧缓存不可信，否则会拿旧信道跳过扫描 */
			align_scan_wq_init_once();
			k_work_submit_to_queue(&align_scan_wq, &align_scan_kick);
			g_scan_waited_ms = 0;
			LOG_INF("align: \"%s\" not in scan cache -> full scan", g_ssid);
			k_work_reschedule(&align_work, K_MSEC(1000));
			return;
		}
	}

	if (g_auto_reconnect) {
		if (g_auto_attempts < AUTO_RETRY_MAX) {
			g_auto_attempts++;
			/* 第一次失败只等 0.5 s：实测这是"AP 刚起、驱动还没扫过"的
			 * 短窗口，等满 3 s 纯属白等（见 portal_cfg.h）。 */
			uint32_t delay = (g_auto_attempts <= 2) ? AUTO_RETRY_FIRST_MS
							       : AUTO_RETRY_DELAY_MS;

			k_work_reschedule(&retry_work, K_MSEC(delay));
			return;
		}

		/* 这条试满了，换下一条，计数归零重新给满 AUTO_RETRY_MAX 次 */
		if (cred_advance()) {
			g_auto_attempts = 1;
			snprintf(g_msg, sizeof(g_msg), "try next \"%s\"", g_ssid);
			k_work_reschedule(&retry_work, K_MSEC(AUTO_RETRY_DELAY_MS));
			return;
		}

		g_auto_reconnect = false;
		g_state = LINK_IDLE;
		strcpy(g_msg, "all saved networks failed");
		LOG_WRN("all saved networks failed");
		return;
	}

	if (g_rt_reconnect) {
		schedule_runtime_retry();
		return;
	}

	/* 用户从网页手动连一个不存在/连不上的网络时，STA 往往压根没离开原来那条。
	 * 实测（NoSuchAP-ZZ）：返回 connect failed 1，但 IP 仍是 10.168.1.226、
	 * 页面照样能打开 —— 说明链路还在，只是状态被打成了 idle，前端就显示
	 * "未连接"却还能上网，等于给了个假状态。所以以实际链路为准：
	 * 还能读到 RSSI 就把状态拉回 connected。 */
	if (wifi_rssi() != 0) {
		g_state = LINK_CONNECTED;
		strcpy(g_msg, "connected");
		LOG_INF("keep existing link (rssi!=0) while \"%s\" failed", g_ssid);
	} else {
		g_state = LINK_IDLE;
	}
}

static void conn_timeout_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (g_state == LINK_CONNECTING) {
		g_state = LINK_IDLE;
		strcpy(g_msg, "connect timeout 20s · 路由器没回完握手（信号弱或密码不对）");
		strcpy(g_last_err, "connect timeout 20s · 路由器没回完握手（信号弱或密码不对）");
		LOG_WRN("connect timeout");
		connect_failed_now(-1);
	}
}

static void connect_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (sta_iface == NULL) {
		g_state = LINK_IDLE;
		strcpy(g_msg, "no STA interface");
		return;
	}

	/* 单射频：AP 与 STA 必须同信道。但"必须同信道"不等于"必须由我们搬"——
	 *
	 * 改造前这里无条件比较"AP 信道 vs 目标信道"，而 AP 处于 auto 时
	 * ap_current_channel() 恒为 0，于是"0 != 目标"永远成立：只要缓存里扫到过
	 * 目标网络，每次连接都要 ap_disable → 等 900 ms → ap_enable → 等 1500 ms，
	 * 白等 2.4 s 还把已连的手机踢一遍。
	 *
	 * 现在只在 **AP 被钉住** 时才需要搬；auto 交给 ESP-IDF
	 * （ap.channel == 0 时 SoftAP 会跟着 STA 走）。真要搬的情形留给
	 * connect_failed_now() 的对齐第二档去触发。 */
	if (ap_pinned_channel() != 0) {
		uint8_t target_ch = ap_channel_of_ssid(g_ssid);
		uint8_t pinned_ch = ap_pinned_channel();

		if (target_ch != 0 && target_ch != pinned_ch) {
			LOG_INF("channel mismatch: ap pinned ch=%u, \"%s\" ch=%u -> reopen AP",
				pinned_ch, g_ssid, target_ch);
			g_state = LINK_CONNECTING;
			strcpy(g_msg, "switching ap channel");
			/* "把 AP 搬到目标信道"是 SoftAP 自己的事：这里只表达意图 */
			ap_retarget(target_ch);
			return;
		}
	}

	/* 换网这里不做额外处理。
	 *
	 * 曾试过"先 DISCONNECT 再连"（145 那版）：真机上一路走下来会把网络栈
	 * 卡死（切到不存在的网络 → status 4 → 重试 → HTTP 与串口同时失联，
	 * 只能 USB 复位）。在没有把驱动这条路径摸清之前，不做这种"看起来更对"
	 * 的改动 —— 现状是"换网只对存在且可达的网络有效"，失败会被如实记成
	 * status 1/4 并保持原链路（见下面的 -EALREADY 分支与 connect_failed_now
	 * 末尾的"以实际链路为准"）。 */

	k_mutex_lock(&conn_lock, K_FOREVER);

	g_user_disconnect = false;   /* 主动发起的连接，恢复掉线自动重连 */

	struct wifi_connect_req_params params = {0};

	params.ssid = (const uint8_t *)g_ssid;
	params.ssid_length = g_ssid_len;
	params.psk = (const uint8_t *)g_psk;
	params.psk_length = g_psk_len;
	params.security = g_psk_len > 0 ? WIFI_SECURITY_TYPE_PSK : WIFI_SECURITY_TYPE_NONE;
	params.channel = WIFI_CHANNEL_ANY;
	params.band = WIFI_FREQ_BAND_2_4_GHZ;
	params.timeout = CONNECT_TIMEOUT_MS;

	g_state = LINK_CONNECTING;
	strcpy(g_msg, "connecting");

	/* 用户主动发起的新连接（既不是上电重连，也不是运行期重连），
	 * 计数从 0 开始——换了个网络就不该继承上一个网络的失败次数。 */
	if (!g_auto_reconnect && !g_rt_reconnect) {
		g_rt_attempts = 0;
	}

	LOG_INF("connecting to \"%s\" ...", g_ssid);

	int ret = net_mgmt(NET_REQUEST_WIFI_CONNECT, sta_iface, &params, sizeof(params));

	k_mutex_unlock(&conn_lock);

	if (ret) {
		/* -EALREADY：驱动认为这个网络已经在连/已连上了。
		 * 这不是失败，是"目标状态已达成"，千万别把状态打成 idle、把灯打红。 */
		if (ret == -EALREADY) {
			LOG_INF("already connected to \"%s\", keeping state", g_ssid);
			/* 驱动说"已经在连/已连上了"，那状态就该是 connected。
			 * 之前这里只打日志不改 g_state：如果前一次连别的网络失败把状态
			 * 打成了 idle，再点一次已保存的网络就永远回不到绿色。 */
			if (g_state != LINK_CONNECTED) {
				g_state = LINK_CONNECTED;
				strcpy(g_msg, "connected");
				if (!g_link_ssid[0]) {
					strcpy(g_link_ssid, g_ssid);
				}
			}
			return;
		}

		g_state = LINK_IDLE;
		snprintf(g_msg, sizeof(g_msg), "connect request err %d", ret);
		LOG_ERR("connect request failed (%d)", ret);

		/* 运行期重连时驱动拒收请求，也要继续退避，别停在红灯 */
		if (g_rt_reconnect) {
			schedule_runtime_retry();
		}
		return;
	}

	k_work_reschedule(&conn_timeout_work, K_MSEC(CONNECT_TIMEOUT_MS));
}

/* 上电自动重连的退避重试：失败后隔 3 s 再发一次 CONNECT */
static void retry_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (g_auto_reconnect) {
		LOG_INF("auto reconnect attempt %u/%u", g_auto_attempts, AUTO_RETRY_MAX);
		k_work_submit(&connect_work);
	} else if (g_rt_reconnect) {
		LOG_INF("runtime reconnect attempt %u/%u",
			(unsigned)g_rt_attempts, RT_RETRY_MAX);
		k_work_submit(&connect_work);
	}
}

/* ------------------------------------------------------------ 事件回调 */

/* 记一个刚连上 SoftAP 的客户端。同一个 MAC 只占一行（重连时刷新时间戳），
 * 表满就丢掉最老的一条。 */
int wifi_rssi(void)
{
	struct wifi_iface_status st;

	if (sta_iface == NULL) {
		return 0;
	}

	memset(&st, 0, sizeof(st));
	if (net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, sta_iface, &st, sizeof(st)) != 0) {
		return 0;
	}

	if (st.state != WIFI_STATE_COMPLETED) {
		return 0;
	}

	return (int)st.rssi;
}

/* RSSI 采样 work。定义必须排在 rssi_sample_handler 之前：
 * handler 里要用 &rssi_sample_work 重新排自己（自己 reschedule 自己）。 */
void rssi_sample_handler(struct k_work *work);

static K_WORK_DELAYABLE_DEFINE(rssi_sample_work, rssi_sample_handler);

/* 每 10 s 采一个 RSSI 点。只在已连接时采：
 * 断开时 wifi_rssi() 返回 0，若也记进去，折线会被一个"0 dBm"的尖峰污染。 */
void rssi_sample_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (g_state == LINK_CONNECTED) {
		int rssi = wifi_rssi();

		if (rssi != 0) {
			g_rssi_hist[g_rssi_head] = (int16_t)rssi;
			g_rssi_head = (uint8_t)((g_rssi_head + 1) % RSSI_HIST_LEN);
			if (g_rssi_cnt < RSSI_HIST_LEN) {
				g_rssi_cnt++;
			}
			/* 留住最后一个有效值：掉线时的 wifi_rssi() 恒为 0，
			 * 只有这里存的值能说明"断的时候信号到底好不好" */
			g_last_rssi = rssi;
		}
	}

	/* 借这个已有的 10 s 周期 work 做自证判定，不额外起一个 work：
	 * 新固件跑满自证窗口且链路正常就转正，否则等 mcuboot 下次启动回滚。 */
	ota_confirm_if_healthy(g_state == LINK_CONNECTED);
	/* 公网 OTA 的链路喂入（l2 -> l0 方向正确）：连上的边沿会触发一次首查 */
	ota_cloud_link_feed(g_state == LINK_CONNECTED);

	/* 借这个 10 s 周期把"已运行多久"喂给启动记录：下次车间断电重启后，
	 * /api/boot 里的 last_up 能区分"跑 8 秒就崩"和"跑了 3 小时才崩"。 */
	ops_boot_tick((uint32_t)(k_uptime_get() / 1000));

	k_work_reschedule(&rssi_sample_work, K_MSEC(RSSI_SAMPLE_MS));
}

/* 1400：msg / last_err 各 96 字节（转义后可能翻倍），再加 ota 整段、时钟、
 * wlog 条数、boot 子对象，以及 dns/sntp 诊断字段。 */

/* ===================== 对外接口：快照与动作 ===================== */

/* 惰性解析 STA 接口，理由同 portal_ap.c 的 ap_iface_get()：
 * 分层时把 main.c 里 "sta_iface = net_if_get_wifi_sta();" 漏掉了，
 * 导致 main 启动检查判 NULL 后直接 return，Wi-Fi 完全不起。
 * 第一次问就向驱动要一次并缓存，之后所有内部使用者都指向这一份。 */
struct net_if *link_sta_iface(void)
{
	if (sta_iface == NULL) {
		sta_iface = net_if_get_wifi_sta();
	}
	return sta_iface;
}

int link_state(void)
{
	return g_state;
}

bool link_is_connected(void)
{
	return g_state == LINK_CONNECTED;
}

bool link_target_is(const char *ssid, size_t len)
{
	return len > 0 && g_ssid_len == len && memcmp(g_ssid, ssid, len) == 0;
}

const char *link_ip(void)
{
	return g_ip;
}

void link_set_gw(const char *gw)
{
	if (gw == NULL) {
		return;
	}

	strncpy(g_gw, gw, sizeof(g_gw) - 1);
	g_gw[sizeof(g_gw) - 1] = '\0';
}

/* 一次取走整份状态：12 个全局变成 1 个只读结构，
 * 调用方（HTTP / 灯 / shell）不可能再各自读到一个"半更新"的组合。 */
void link_view_get(struct link_view *v)
{
	memset(v, 0, sizeof(*v));

	v->state = g_state;
	v->target_ssid = g_ssid;
	v->link_ssid = g_link_ssid;
	v->ip = g_ip;
	v->gw = g_gw;
	v->msg = g_msg;
	v->last_err = g_last_err;
	v->rssi = wifi_rssi();
	v->reconnects = g_reconnects;
	v->connected_at = g_connected_at;
}

void link_connect_now(void)
{
	k_work_submit(&connect_work);
}

void link_connect_deferred(uint32_t delay_ms)
{
	k_work_reschedule(&connect_defer_work, K_MSEC(delay_ms));
}

void link_rssi_sampling_start(void)
{
	k_work_schedule(&rssi_sample_work, K_MSEC(RSSI_SAMPLE_MS));
}

void link_rssi_hist_json(char *out, size_t len)
{
	uint8_t start = (g_rssi_cnt < RSSI_HIST_LEN) ? 0 : g_rssi_head;
	size_t n = 0;

	n += snprintf(out + n, len - n, "{\"points\":[");

	for (uint8_t k = 0; k < g_rssi_cnt && n + 8 < len; k++) {
		uint8_t idx = (uint8_t)((start + k) % RSSI_HIST_LEN);

		n += snprintf(out + n, len - n, "%s%d", k ? "," : "", g_rssi_hist[idx]);
	}

	snprintf(out + n, len - n, "],\"interval\":%d}", RSSI_SAMPLE_MS / 1000);
}

/* 网页/串口点"连接"：只改目标参数，真正的连接交给 connect_work。
 * 原来的实现散在 handler 里，直接写 6 个全局；现在一句话。 */
int link_request_connect(const char *ssid, size_t ssid_len,
			 const char *psk, size_t psk_len)
{
	if (ssid_len == 0 || ssid_len > WIFI_SSID_MAX_LEN || psk_len > 64) {
		return -EINVAL;
	}

	memcpy(g_ssid, ssid, ssid_len);
	g_ssid[ssid_len] = '\0';
	g_ssid_len = ssid_len;

	memcpy(g_psk, psk, psk_len);
	g_psk[psk_len] = '\0';
	g_psk_len = psk_len;

	strcpy(g_msg, "queued");
	g_last_err[0] = '\0';
	g_auto_reconnect = false;
	g_auto_attempts = 0;
	g_rt_reconnect = false;
	/* 换了个目标：对齐账本归零（上一轮为旧网络扫过/钉过的信道不能算数） */
	g_align_step = 0;
	g_scan_waited_ms = 0;

	int saved = cred_index(ssid, ssid_len);

	g_cred_try = (saved >= 0) ? (uint8_t)saved : 0;
	g_cred_rounds = 0;

	k_work_submit(&connect_work);

	return 0;
}

/* 上电自动连：优先第 0 条（cred_migrate/cred_upsert 会把"上次能用"的排到表头） */
void link_boot_autoconnect(void)
{
	if (cred_count() == 0) {
		return;
	}

	cred_apply(0);
	g_cred_rounds = 0;
	g_auto_reconnect = true;
	g_auto_attempts = 1;
	strcpy(g_msg, "auto connect on boot");

	LOG_INF("boot reconnect: %u saved network(s), start with \"%s\"",
		(unsigned)cred_count(), g_ssid);

	k_work_submit(&connect_work);
}

/* ===================== 事件入口：由 main.c 的分派器调 ===================== */

void link_ev_connect_result(int status)
{
	k_work_cancel_delayable(&conn_timeout_work);

	if (status == 0) {
		g_state = LINK_CONNECTED;
		g_auto_reconnect = false;
		g_auto_attempts = 0;
		g_ever_connected = true;
		g_connected_at = k_uptime_get_32();
		/* 连上了：对齐账本归零，下次失败从"直接连"重新开始 */
		g_align_step = 0;
		g_scan_waited_ms = 0;
		/* 注意：这里不清 g_rt_attempts。是否清零由"这次连了多久"决定，
		 * 见 link_ev_disconnect_result —— 手工清零会让"连上 2 秒又掉"的
		 * 边缘信号永远从头计数，10 次上限就形同虚设。 */
		if (g_rt_reconnect) {
			g_rt_reconnect = false;
			g_reconnects++;
			LOG_INF("reconnected to \"%s\" (total %u)",
				g_ssid, (unsigned)g_reconnects);
		}
		strcpy(g_msg, "connected");
		g_last_err[0] = '\0';   /* 连上了，清掉上一次的失败原因 */
		/* 记录"真的连上的" SSID：g_ssid 是目标名，中途可能已经被改过 */
		strcpy(g_link_ssid, g_ssid);
		LOG_INF("connected to \"%s\"", g_ssid);
		ops_ev_add(OPS_EV_CONNECT, 0, 0, 0, (int8_t)g_last_rssi, g_ssid, 0);
		/* 只有真正连上过才落盘，避免把错密码写进 NVS。
		 * cred_upsert 内部判断"已在表头且密码没变"，不会重复写 flash。 */
		cred_upsert(g_ssid, g_ssid_len, g_psk, g_psk_len);
		/* 记住"这个网络真的能用"，重启时优先连它（不然会先白试一轮旧 SSID） */
		cred_mark_last_good(g_ssid, g_ssid_len);
		/* cred_upsert 把刚连上的这条挪到了 items[0]，轮换下标同步归零，
		 * 这样下次掉线换网是从"当前这条的下一条"开始。 */
		g_cred_try = 0;
		g_cred_rounds = 0;
		/* 拿 IP 靠 DHCP 客户端；显式启动一次更稳 */
		net_dhcpv4_start(sta_iface);
	} else {
		const char *why = reason_text(status);

		if (why) {
			LOG_WRN("connect failed, status %d (%s)", status, why);
			snprintf(g_msg, sizeof(g_msg), "connect failed %d · %s", status, why);
			snprintf(g_last_err, sizeof(g_last_err), "connect failed %d · %s",
				 status, why);
		} else {
			LOG_WRN("connect failed, status %d", status);
			snprintf(g_msg, sizeof(g_msg), "connect failed %d", status);
			snprintf(g_last_err, sizeof(g_last_err), "connect failed %d", status);
		}
		ops_ev_add(OPS_EV_CONN_FAIL, 0, 0, (uint16_t)status, (int8_t)g_last_rssi,
			   g_ssid, 0);
		connect_failed_now(status);
	}
}

void link_ev_disconnect_result(const struct wifi_status *st)
{
	bool was_connecting = (g_state == LINK_CONNECTING);
	/* 下面会把 g_link_ssid 清空，先抄下"掉的是哪条网"，否则日志里只剩空名字 */
	char ssid_then[WIFI_SSID_MAX_LEN + 1];

	strncpy(ssid_then, g_link_ssid[0] ? g_link_ssid : g_ssid,
		sizeof(ssid_then) - 1);
	ssid_then[sizeof(ssid_then) - 1] = '\0';

	if (!was_connecting) {
		g_state = LINK_IDLE;
		snprintf(g_msg, sizeof(g_msg), "disconnected %d", st ? st->status : 0);
		strcpy(g_ip, "0.0.0.0");
		g_link_ssid[0] = '\0';
	}

	if (st) {
		/* 只打 status（0/1/2）等于什么都没说清。真正能定性的是 802.11
		 * reason_code：4/34=不活跃超时、15=四次握手超时、2=认证过期、
		 * 8=AP 主动踢人。这行就是"无缘无故断连"的判据。 */
		uint32_t held = g_connected_at ? (k_uptime_get_32() - g_connected_at) / 1000u
					       : g_link_held_s;

		ops_ev_add(OPS_EV_DISCONNECT, st->disconn_reason, st->reason_code,
			   st->status_code, (int8_t)g_last_rssi, ssid_then, held);
		LOG_INF("disconnected \"%s\" reason=%d(%s) rc=%u(%s) sc=%u rssi=%d held=%us",
			ssid_then, st->status,
			wifi_disconn_reason_txt(st->disconn_reason), st->reason_code,
			ops_reason_code_txt(st->reason_code), st->status_code,
			g_last_rssi, held);
	} else {
		uint32_t held = g_connected_at ? (k_uptime_get_32() - g_connected_at) / 1000u
					       : g_link_held_s;

		ops_ev_add(OPS_EV_DISCONNECT, 0, 0, 0, (int8_t)g_last_rssi, ssid_then, held);
		LOG_INF("disconnected \"%s\" (no status info) rssi=%d held=%us", ssid_then,
			g_last_rssi, held);
	}
	g_link_held_s = 0;

	/* 连接过程中的掉线交给 CONNECT_RESULT 的分支计数，这里不重复触发。
	 * 已经连上过的网络掉线 → 走运行期退避重连，别卡在红灯等复位。
	 * 但用户从网页/串口主动断开的，不该自作主张连回去。 */
	if (!was_connecting && g_ever_connected && g_ssid_len > 0 &&
	    !g_auto_reconnect && !g_user_disconnect) {
		uint32_t now = k_uptime_get_32();

		/* 上一次连接维持了 30 s 以上，说明网络是真恢复了，重新给满 10 次；
		 * 只连上几秒又掉（信号边缘），保留已用次数，免得无限重连。 */
		if (g_connected_at != 0 && (now - g_connected_at) >= CONN_STABLE_MS) {
			g_rt_attempts = 0;
		}
		g_connected_at = 0;
		g_rt_reconnect = true;
		schedule_runtime_retry();
	}
}

void link_ev_got_ip(struct net_if *iface, const struct in_addr *addr)
{
	if (iface != sta_iface || addr == NULL) {
		return;
	}

	net_addr_ntop(AF_INET, addr, g_ip, sizeof(g_ip));
	/* 直接打出"开机到有 IP 用了多久"：这个数就是用户感知的"多久能打开页面"，
	 * 以后不用再靠秒表掐。（改造前实测 19.3 s，见 docs 的诊断文档） */
	LOG_INF("STA got IP: %s (boot -> ip = %u ms)", g_ip,
		(unsigned)(k_uptime_get_32()));
}

/* STA 当前的真实关联信道（0 = 没连上或读不到）。
 * wifi_iface_status 对 STA 返回 ap_info.primary，这是单射频下"AP 现在应该在哪"
 * 的唯一真值来源；/api/status 用它，让人一眼看出 AP/STA 是否对得上。 */
int link_sta_channel(void)
{
	struct wifi_iface_status st;

	if (link_sta_iface() == NULL) {
		return 0;
	}

	memset(&st, 0, sizeof(st));
	if (net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, sta_iface, &st, sizeof(st)) != 0) {
		return 0;
	}

	return (int)st.channel;
}

/* 对齐状态对外只读：串口日志会静默（USB-Serial-JTAG 抓取不可靠），
 * 把它挂到 /api/status 上就能用 HTTP 轮询看见 0 → 1 → 2 的推进。 */
int link_align_step(void)
{
	return (int)g_align_step;
}

/* connect_defer_work 的 handler 留在这里：它只做一件事 —— 把连接请求
 * 推到 connect_work。延后多久由调用方决定（AP 换信道要等 1.5 s）。 */
static void connect_defer_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	k_work_submit(&connect_work);
}

/* 对齐第一档：等扫描落地，再重来一次连接。
 *
 * 扫描是异步的（事件流写缓存），不能在这里阻塞等；每 1 s 回来一次，最多等
 * ALIGN_SCAN_WAIT_MS。等不到 SCAN_DONE 也照样往下走 —— 那时缓存里可能已经有
 * 部分结果，比什么都不做强。 */
static void align_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (scan_is_active() && g_scan_waited_ms < ALIGN_SCAN_WAIT_MS) {
		g_scan_waited_ms += 1000U;
		k_work_reschedule(&align_work, K_MSEC(1000));
		return;
	}

	uint8_t target = ap_channel_of_ssid(g_ssid);

	LOG_INF("align step %u: scan finished after %u ms, \"%s\" -> ch %u",
		(unsigned)g_align_step, (unsigned)g_scan_waited_ms, g_ssid, target);

	k_work_submit(&connect_work);
}

/* 单字段读：给 L1 的 snprintf 用。
 * 有 link_view_get 之后这几个看着多余，但它们让 handler 不必为了一个字段
 * 声明一个快照结构；语义仍是"只读一份拷贝，不暴露内部变量地址"。 */
const char *link_target_ssid(void)
{
	return g_ssid;
}

const char *link_link_ssid(void)
{
	return g_link_ssid;
}

const char *link_msg(void)
{
	return g_msg;
}

const char *link_err(void)
{
	return g_last_err;
}

const char *link_gw(void)
{
	return g_gw;
}

uint32_t link_reconnects(void)
{
	return g_reconnects;
}
