# 14 Zephyr 系统层与进阶路线

> **现状校准（2026-11-19）**：本组是 S31 学习线的系统层；当前仓库主线 F411 的
> 运行时事实（当前线程、就绪队列、IRQ 栈、`0xAA` 水位）已经能在 Kharon 调试区直接读，
> 不必先写 shell 命令，见 [`tutorials/14`](../../../tutorials/14-Zephyr调试区操作.md)。

> 这一组回答两个问题：
> 1. **korvo_apps（应用层）做完之后，这块板子还能往上做什么？**（本文的「路线图」部分）
> 2. **系统层（复位原因 / 片上温度 / 硬件随机数 / 看门狗）怎么在 Zephyr 里做？**（见 `系统层.md`，已烧录实测）
>
> 标 ✅ = 本机烧录 + 串口实测；标 📖 = 只有代码与原理，无法在本机演示（原因写明）。

---

## 一、路线图：还剩哪些能开发

判断一个外设「能不能在 Zephyr 里跑」，要同时看三件事，缺一不可：

| 条件 | 怎么看 | 例子 |
|---|---|---|
| ① **SoC dts 里有节点** | `zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi` | `wdt0`、`trng0`、`coretemp` 有 |
| ② **树里有驱动源文件** | `zephyr\drivers\*\*esp32*.c` | `wdt_esp32.c`、`entropy_esp32.c` 有 |
| ③ **Kconfig 真的把它选进来了** | 编译日志里的 `libdrivers__xxx.a`，或 `device_is_ready()` 的返回值 | `wdt0` 节点默认 disabled → 驱动没编进去 |

**第 ③ 条最容易被忽略。** 很多"驱动明明存在却用不了"的坑，都出在这里：节点是 `disabled`，或者总开关（`CONFIG_WATCHDOG`）没打开，Kconfig 于是把驱动判成 `n`，链接时才会以 `undefined reference to __device_dts_ord_NN` 的形式炸出来。

### S31 这棵树的外设节点清单（实测 grep 结果，共 42 个）

```
cpu0/cpu1  usb_otg  dma  dma_axi  rmt  mcpwm0/1  i2c0/1  pcnt  uart0/1/2  spi2/3
usb_serial  ledc0  twai0/1  systimer0  flash  aes  sha  timer0/1  wdt0/1
gpio0/1  intc  rtc_timer  lp_uart  trng0  adc0/1  coretemp
sramlp  sramhp  soc_rom  ext_ram(PSRAM @0x50000000)  ldo1
```

对照一下就知道边界在哪：

| 官方硬件资源 | S31 dts 节点 | 树里的驱动 | 结论 |
|---|---|---|---|
| RGB LED（GPIO37，WS2812） | 有（`rmt` + `led-strip` 别名） | `pulse_io_esp32_rmt` + `led_strip` | ✅ 已跑通（13 组） |
| 4 个功能键（IO42 电阻梯） | 有（`adc0`） | `adc_esp32` | ✅ 已跑通（13 组） |
| 功放 PA_CTRL（GPIO7） | 有（`gpio0`） | `gpio_esp32` | ✅ 已跑通（13 组） |
| ES8389 codec + 喇叭 + 麦克风 | **❌ 无 `i2s` 节点** | `i2s_esp32.c` 存在 | ❌ 缺 dts 节点，放音做不了 |
| microSD（SDIO 3.0） | **❌ 无 `sdhc` 节点** | `sdhc_esp32.c` 存在 | ❌ 同上 |
| 摄像头（DVP，13×IO） | **❌ 无 `video`/`dvp` 节点** | `video_esp32_dvp.c` 存在 | ❌ 同上 |
| 4.3" LCD（MIPI-DSI/DBI 座） | **❌ 无 `mipi_dsi`/`mipi_dbi` 节点** | `dsi_esp32.c`、`mipi_dbi_esp32.c` 存在 | ❌ 同上 |
| 片内 Flash + PSRAM | 有（`flash`、`ext_ram`） | `flash_esp32.c` | ✅ 可做持久化 / PSRAM 实验 |
| 片上温度 | 有（`coretemp`） | `esp32_temp` | ✅ 本轮跑通 |
| 硬件随机数 | 有（`trng0`） | `entropy_esp32` | ✅ 本轮跑通 |
| 看门狗 | 有（`wdt0/1`，**默认 disabled**） | `wdt_esp32.c` | ✅ 设备可用（见下方安全说明） |
| AES / SHA 加速 | 有（`aes`、`sha`） | `crypto_esp32_aes/sha` | 待做 |
| CAN（TWAI） | 有（`twai0/1`） | `can_esp32_twai` | 需外接收发器 |
| PWM | 有（`ledc0`、`mcpwm0/1`） | `pwm_led_esp32`、`pwm_mc_esp32` | 需外接 LED/舵机才有观感 |
| 脉冲计数 | 有（`pcnt`） | `pcnt_esp32` | 需外部脉冲源 |
| 计数器 / RTC | 有（`timer0/1`、`rtc_timer`、`systimer0`） | `counter_esp32_tmr/rtc` | 可做 |
| UART1/2、SPI2/3 | 有 | `uart_esp32`、`spi_esp32_spim` | 环回需跳线，UART 会与 COM9 shell 抢 |
| Wi-Fi / BLE | 无 | 未合入本树 | ❌ 等上游 |

### 三档优先级（按"现在就能做且没有副作用"排序）

**A 档：不接线、不重启，纯软件就能做完**

| 主题 | 用什么 | 产出 |
|---|---|---|
| 片上温度曲线 | `coretemp` + `sensor_sample_fetch` | 采样表、与 shell 命令联动（已做） |
| 硬件随机数质量 | `sys_csrand_get` / `entropy_get_entropy` | 随机性统计、与软件 PRNG 对比（已做） |
| 复位原因 / 器件 ID | `hwinfo_get_reset_cause`、`hwinfo_get_device_id` | 启动自检页（已做） |
| Flash + NVS 持久化 | `storage_partition`(0xfb0000, 192 KB) + NVS | 记住 RGB 亮度/模式、开机次数 —— **✅ 已完成**，见 `NVS持久化.md`（注意本树 NVS 在 `subsys\kvss\`，头文件 `<zephyr/kvss/nvs.h>`） |
| counter 子系统 | `timer0/1`、`rtc_timer` | 秒表、低功耗计时 |
| crypto（AES/SHA） | `aes`、`sha` 节点 + `crypto` API | 一次 SHA-256 算例、和软件实现比速度 |
| 纯软件子系统 | `zbus`、`ring_buffer`、`k_mem_slab`、`k_condvar` | 组件层练习，不碰硬件 |

**B 档：要一根杜邦线或一个负载才能"看见"**

| 主题 | 怎么接 | 注意 |
|---|---|---|
| `ledc0` PWM | GPIO 接 LED + 电阻 | 本板 RGB 是 WS2812，不走 PWM；要另找空闲 IO |
| `mcpwm0/1` | 接舵机/电机 | 需要外部供电，别从板子取大电流 |
| `pcnt` | 用 GPIO 输出方波喂进去 | 先 PWM 出脉冲，再 PCNT 数，形成自环 |
| SPI2/3 环回 | MOSI 短接 MISO | 只能验证时序，不能验证真实外设 |
| UART1/2 环回 | TX 短接 RX | **不要用 uart0**，那是 shell 的控制台 |
| TWAI/CAN | 需 CAN 收发器（如 SN65HVD230） | `chosen` 已指到 `twai0` |

**C 档：这棵树暂时做不了，别浪费时间**

I2S 放音（ES8389）、microSD、DVP 摄像头、MIPI 显示、Wi-Fi/BLE、BOOT 键（GPIO61 在 bank1，驱动未闭环）。

> 这一档的正确态度：**不是"Zephyr 不行"，而是"S31 的 dts 还没写这些节点"**。驱动源码大多已经在树里（`i2s_esp32.c` 等），要的是有人把 dtsi 补上。想练手可以自己写节点 + 编译验证 binding，但那是 SoC 移植的活，不是"用 Zephyr"。

### 顺带发现：这块板子的分区表本来就留了 OTA 的位置

`partitions_0x2000_default_16M.dtsi` 实际内容：

| 分区 | 偏移 | 大小 | 标签 |
|---|---|---|---|
| boot | 0x2000 | 56 KB | `mcuboot` |
| sys | 0x10000 | 64 KB | `sys` |
| slot0 | 0x20000 | 7936 KB | `image-0` |
| slot1 | 0x7e0000 | 7936 KB | `image-1` |
| lpcore slot0/1 | 0xfa0000 / 0xfa8000 | 32 KB×2 | 低功耗核固件 |
| **storage** | **0xfb0000** | **192 KB** | **`storage`（NVS/Settings 就放这）** |
| coredump | 0xfff000 | 4 KB | `coredump` |

双 image slot + mcuboot 意味着 MCUboot OTA 在**硬件层面**是现成的；`storage` 192 KB 意味着 NVS 有地方落盘。这两条都是 A 档里最值得做的下一步。

---

## 二、本机执行策略说明（重要）

系统层面禁止任何**可能导致板子重启 / 关机 / 强制复位**的操作。所以下面这些"经典演示"在本教程里**只有代码和原理，没有实测**：

| 演示 | 为什么不做 | 教程里怎么处理 |
|---|---|---|
| `wdt_install_timeout()` + 停止喂狗 → 看复位 | 会真的复位板子 | 给出完整代码 + 寄存器语义，标 📖 |
| `sys_reboot()` 软件复位 | 同上 | 只讲原理 |
| 让线程死循环等看门狗咬 | 同上 | 只讲原理 |

`korvo_sys` 里保留了**只读**部分：`device_is_ready(wdt0)` 能证明驱动、时钟、dts 三者都对了，但不写任何 WDT 寄存器，因此没有复位风险。这已经足够验证"看门狗这条链路是通的"。

---

## 三、页面索引

| 页面 | 内容 | 状态 |
|---|---|---|
| `系统层.md` | `applications/esp32s31/korvo_sys`：复位原因 / 片上温度 / 硬件随机数 / 看门狗就绪，含 4 条命令的实测原文和 6 个坑 | ✅ 实测 |
| `NVS持久化.md` | `applications/esp32s31/korvo_nvs`：string / blob / u32 三种类型、跨 4 次复位验证写读删、扇区与 GC 原理、换板迁移清单 | ✅ 实测 |
| 本页 | 路线图 + 节点/驱动/Kconfig 三条件判据 + 三档开发清单 | — |

## 自检

1. 你能说出"驱动存在但用不了"的三种原因吗？（节点 disabled / 总开关没开 / Kconfig 依赖不满足）
2. `CONFIG_WATCHDOG` 和 `CONFIG_WDT_ESP32` 是什么关系？
3. 为什么 I2S 放音在这块板子上做不了，而 `i2s_esp32.c` 却明明在树里？
4. `storage_partition` 在哪个偏移、多大？它能拿来干什么？
5. 一个外设要"能跑"，dts、驱动源文件、Kconfig 三者各需要满足什么？
