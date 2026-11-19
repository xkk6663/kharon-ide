/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * f411_modbus —— STM32F411CEU6（WeAct Black Pill V2.0）上的 Modbus RTU 从站
 *
 * 它替代的是 classic 的 examples/STM32F411_modbus，但**只替代功能，不替代引擎**：
 *   classic：自己写 CRC16、自己写循环 DMA 收 + 软件乒乓发、自己写静默定界；
 *   这里    ：把 USART1 交给 Zephyr 的 Modbus 子系统（subsys/modbus），
 *             内核只负责「寄存器表里每个地址是什么意思」。
 *
 * 换句话说：这一版里没有一行 CRC、没有一个状态机。想改协议行为去改 dts 与 Kconfig，
 * 想改业务语义才改这个文件 —— 这是迁移到 Zephyr 最直接的好处。
 *
 * 寄存器表与 classic 点表（projects/STM32F411_modbus.modbus.json）严格对齐：
 *   0x0000 device_id = 0x0411 (STM32F411)     0x0001 fw_version = 0x0100
 *   0x0002 tick（每秒 +1）                     0x0003 toggles（每秒 +2）
 *   0x0004 stage = 1                           0x0010 LED 命令（写 0/1）
 *   0x0011 burst（接受写入，Zephyr 侧不产生连发）
 *   0x0020/21 runtime u32（高字在前）          0x0030/31 temp f32（高字在前）
 *   0x0040/41 uptime u32                       0x0110..0x0117 scratch（可读写）
 *   线圈 0..3（线圈 0 与 LED 联动）            离散输入 0 = LED 状态 / 1 = 最近 2s 有事务
 *
 * 0x0100..0x0109 在 classic 里是「循环 DMA 的仪表盘」（HT/TC/IDLE/TX 乒乓）。
 * Zephyr 用的是子系统自己的收发路径，没有这些计数，所以这段**恒返回 0**。
 * 不编造数字，是这个工程的一条硬规矩。
 */

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/modbus/modbus.h>
#include <zephyr/sys/printk.h>
#include <zephyr/version.h>

/* ── 从站参数 ──────────────────────────────────────────────────── */
#define UNIT_ID   1u
#define HOLD_N    0x0120u          /* 保持寄存器地址空间 0x0000–0x011F（与 classic 同） */
#define COIL_N    4u

#define REG_LED       0x0010u
#define REG_BURST     0x0011u
#define REG_SCRATCH0  0x0110u
#define SCRATCH_N     8u

/* classic 的 DMA 仪表盘段：Zephyr 无对应物，恒 0 */
#define REG_DMA_FIRST 0x0100u
#define REG_DMA_LAST  0x0109u

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

static uint16_t holding[HOLD_N];
static uint16_t scratch[SCRATCH_N];
static uint8_t  coil_bits;

/* 写过 0x0010 或线圈 0 之后，LED 交给主站；否则自己按 500 ms 翻转当年心跳灯 */
static bool     led_manual;
static int64_t  last_rx_ms;

/* 模拟温度：不引 libm，只验证 f32 的字序与缩放 */
static const float temp_tab[8] = {
	25.0f, 25.5f, 26.0f, 26.5f, 27.0f, 26.5f, 26.0f, 25.5f
};

static uint32_t uptime_s(void)
{
	return (uint32_t)(k_uptime_get() / 1000);
}

/* 任何一次被服务端调用的回调 = 总线上刚有一帧。离散输入 1 用它判「主站还在不在」 */
static void mark_rx(void)
{
	last_rx_ms = k_uptime_get();
}

static uint32_t temp_u32(void)
{
	union {
		float f;
		uint32_t u;
	} cv;

	cv.f = temp_tab[uptime_s() % 8u];
	return cv.u;
}

/* ── 服务端回调：只回答“这个地址是什么意思” ──────────────────── */

static int coil_rd(uint16_t addr, bool *state)
{
	if (addr >= COIL_N) {
		return -ENOTSUP;
	}
	mark_rx();
	*state = (coil_bits >> addr) & 1u;
	return 0;
}

static int coil_wr(uint16_t addr, bool state)
{
	if (addr >= COIL_N) {
		return -ENOTSUP;
	}
	mark_rx();

	if (state) {
		coil_bits |= (uint8_t)BIT(addr);
	} else {
		coil_bits &= (uint8_t)~BIT(addr);
	}

	/* 线圈 0 与 LED 联动：classic 也是这个约定，点表里写的是「线圈 0 与 LED 联动」 */
	if (addr == 0u) {
		led_manual = true;
		gpio_pin_set_dt(&led, state ? 1 : 0);
	}
	return 0;
}

static int discrete_rd(uint16_t addr, bool *state)
{
	mark_rx();

	switch (addr) {
	case 0u:
		*state = gpio_pin_get_dt(&led) > 0;
		return 0;
	case 1u:
		*state = (k_uptime_get() - last_rx_ms) < 2000;
		return 0;
	default:
		return -ENOTSUP;
	}
}

static int input_reg_rd(uint16_t addr, uint16_t *reg)
{
	ARG_UNUSED(addr);
	ARG_UNUSED(reg);
	return -ENOTSUP;            /* 这片点表没有输入寄存器，如实拒绝 */
}

static int holding_reg_rd(uint16_t addr, uint16_t *reg)
{
	uint32_t t;
	uint32_t temp;

	mark_rx();

	t = uptime_s();
	temp = temp_u32();

	switch (addr) {
	case 0x0000:                                    /* device_id */
		*reg = 0x0411u;                         /* 0x04 = STM32F4, 0x11 = F411 */
		return 0;
	case 0x0001:                                    /* fw_version 1.0 */
		*reg = 0x0100u;
		return 0;
	case 0x0002:                                    /* tick：每秒 +1 */
		*reg = (uint16_t)t;
		return 0;
	case 0x0003:                                    /* toggles：每秒 +2 */
		*reg = (uint16_t)(t * 2u);
		return 0;
	case 0x0004:                                    /* stage：1 = 从站在跑 */
		*reg = 1u;
		return 0;
	case 0x0005:
		/* frames：classic 数的是「合法帧」；Zephyr 子系统不向应用暴露帧计数。
		 * 与其给个会误导人的近似值，不如返回 0 —— 判活看 tick 或离散输入 1。 */
		*reg = 0u;
		return 0;
	case 0x0020:                                    /* runtime u32 高字（100 ms 计） */
		*reg = (uint16_t)((t * 10u) >> 16);
		return 0;
	case 0x0021:                                    /* runtime u32 低字 */
		*reg = (uint16_t)((t * 10u) & 0xFFFFu);
		return 0;
	case 0x0030:                                    /* temp f32 高字 */
		*reg = (uint16_t)(temp >> 16);
		return 0;
	case 0x0031:                                    /* temp f32 低字 */
		*reg = (uint16_t)(temp & 0xFFFFu);
		return 0;
	case 0x0040:                                    /* uptime u32 高字 */
		*reg = (uint16_t)(t >> 16);
		return 0;
	case 0x0041:                                    /* uptime u32 低字 */
		*reg = (uint16_t)(t & 0xFFFFu);
		return 0;
	default:
		break;
	}

	if (addr >= REG_DMA_FIRST && addr <= REG_DMA_LAST) {
		*reg = 0u;
		return 0;
	}
	if (addr >= REG_SCRATCH0 && addr < REG_SCRATCH0 + SCRATCH_N) {
		*reg = scratch[addr - REG_SCRATCH0];
		return 0;
	}
	if (addr < HOLD_N) {
		*reg = holding[addr];
		return 0;
	}
	return -ENOTSUP;
}

static int holding_reg_wr(uint16_t addr, uint16_t reg)
{
	mark_rx();

	/* 写点白名单：不在表里的一律拒绝（服务端回 02 非法地址）。
	 * 读错可以重读，写错会动硬件 —— 显式列表永远优于按地址段猜。 */
	if (addr == REG_LED) {
		led_manual = true;
		holding[addr] = reg;
		gpio_pin_set_dt(&led, reg ? 1 : 0);
		return 0;
	}
	if (addr == REG_BURST) {
		/* classic 用它触发「连发 N 帧」来观察 TX 乒乓。
		 * Zephyr 的发送路径归子系统，应用侧没有两块发送缓冲可切，
		 * 所以这里只记值、不产生连发（教程 11 §6 会解释这个差别）。 */
		holding[addr] = reg;
		return 0;
	}
	if (addr >= REG_SCRATCH0 && addr < REG_SCRATCH0 + SCRATCH_N) {
		scratch[addr - REG_SCRATCH0] = reg;
		return 0;
	}
	return -ENOTSUP;
}

static struct modbus_user_callbacks mbs_cb = {
	.coil_rd = coil_rd,
	.coil_wr = coil_wr,
	.discrete_input_rd = discrete_rd,
	.input_reg_rd = input_reg_rd,
	.holding_reg_rd = holding_reg_rd,
	.holding_reg_wr = holding_reg_wr,
};

/* ── 启动 ──────────────────────────────────────────────────────── */

int main(void)
{
	struct modbus_iface_param param = {
		.mode = MODBUS_MODE_RTU,
		.server = {
			.unit_id = UNIT_ID,
			.user_cb = &mbs_cb,
		},
		.serial = {
			.baud = 115200,
			.parity = UART_CFG_PARITY_NONE,
		},
	};
	int iface;

	/* ★ 单口方案：所有打印都在 modbus_init_server 之前完成。
	 *   协议一起来，USART1 就归 Modbus 了，此后任何 printk 都会插进帧里。
	 *   classic 的同一块板也是这个做法（首帧前打 [alive]，之后只跑协议）。
	 *
	 * ★ 为什么分三段打印、中间刻意等 1.4 s：
	 *   上位机/工具打开串口需要时间（本轮实测：烧录结束到串口可用约 0.6–1.2 s）。
	 *   如果 4 行横幅在 10 ms 内全打完，抓日志的人只能看到后半截，甚至啥也看不到。
	 *   留一段「启动静默期」既方便现场排障，也不影响 Modbus —— 主站上电后一般也要
	 *   秒级才开始轮询。代价是从站上线晚 1.4 s，嫌慢就改下面两个 k_msleep。 */
	printk("\n[boot] f411_modbus unit=%u 115200 8N1 (Zephyr %s)\n",
	       UNIT_ID, KERNEL_VERSION_STRING);
	printk("[boot] board=%s\n", CONFIG_BOARD_TARGET);

	if (!gpio_is_ready_dt(&led)) {
		printk("[boot] LED not ready, abort\n");
		return -1;
	}
	gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);

	k_msleep(700);
	printk("[boot] regs 0x0000 id | 0x0002 tick | 0x0010 led | 0x0110..117 scratch\n");
	k_msleep(700);
	printk("[boot] coils 0..3 (coil0=led) | di0=led di1=bus-active\n");

	/* 最后再留一点时间把上面两行排空：残留字节会被主站当成帧头 */
	k_msleep(150);

	iface = modbus_iface_get_by_name("modbus0");
	if (iface < 0) {
		printk("[boot] modbus0 not found (%d): overlay 没挂到 USART1？\n", iface);
		return -1;
	}
	if (modbus_init_server(iface, param) != 0) {
		printk("[boot] modbus_init_server failed\n");
		return -1;
	}

	/* 心跳：500 ms 翻一次灯 = 每秒 2 次，与 0x0003 toggles 的“每秒 +2”对得上。
	 * 主站写过 0x0010 / 线圈 0 之后，灯听主站的，这里不再抢。 */
	while (true) {
		if (!led_manual) {
			gpio_pin_toggle_dt(&led);
		}
		k_msleep(500);
	}

	return 0;
}
