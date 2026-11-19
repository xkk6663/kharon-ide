# 附录 A · ESP32-S31（Korvo-1 V1.1）：一块还没进主干的芯片

> 这一章不属于主线。主线是 S3（主干直接支持）。这一章存在的理由只有一个：**它能把"一块新芯片是怎么进 Zephyr 的"讲清楚**，而主线板子永远讲不出这一层。
>
> 如果你手上正好是 Korvo-1，这一章就是你的入口；如果你只是想学 Zephyr，可以跳过，直到你想知道"为什么有些板子要打补丁"。

---

## A.1 为什么 S31 要单独成附录

截至本书写作时：

| 项 | 状况 |
|---|---|
| `zephyr/boards/espressif/` 里有 `esp32s31_*` 吗 | 没有（主干没有 Korvo-1 的板级） |
| `zephyr/soc/espressif/esp32s31/` 有吗 | 没有（主干没有这个 SoC 层） |
| `hal_espressif` 支持列表里有 S31 吗 | 没有 |
| 唯一入口 | 未合并 PR <https://github.com/zephyrproject-rtos/zephyr/pull/119851> |

本仓库的工作区是**主干 + 这个 PR 的补丁**，所以本地能看到：

```
west\zephyr\soc\espressif\esp32s31\                     ← PR 带来的 SoC 层
west\zephyr\boards\espressif\esp32s31_function_coreboard\  ← PR 带来的板级（不是 Korvo-1）
west\modules\hal\espressif\components\soc\esp32s31\       ← HAL 的 SoC 描述
```

**关键提醒**：PR 会 rebase、会被改写。你要复现，就记下自己验证通过的 commit（本机是 `34e025569cbb`），不要盲目跟进最新。

## A.2 怎么用（三条命令）

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide
. .\scripts\kharon-env.ps1                                   # 设 SDK / PATH / ZEPHYR_BASE

west build -b esp32s31_function_coreboard/esp32s31/hpcore `
  -d C:\Users\xiao1\Desktop\ELAB\kharon-ide\build\esp32s31\korvo_shell\s31-debug `
  C:\Users\xiao1\Desktop\ELAB\kharon-ide\applications\esp32s31\korvo_shell

west flash -d C:\Users\xiao1\Desktop\ELAB\kharon-ide\build\esp32s31\korvo_shell\s31-debug `
  --esp-device COM9
```

要点：

- 板目标尾巴 **`hpcore`** 不能省（PR 只启用 HP 核，CPU1 保持在复位）；
- 串口是 **COM9 / 115200 8N1**（CP2102，Silicon Labs）；
- app 镜像偏移 **`0x2000`**（S3 是 `0x0000`，别混）；
- 16 MB flash 需要显式参数：`--esp-flash-size=16MB --esp-flash-freq=80m --esp-flash-mode=dio`。

## A.3 这块板上"能跑"和"不能跑"的边界

| 能力 | 状态 | 为什么 |
|---|---|---|
| UART / USB-Serial-JTAG / GPIO / pinctrl | ✅ | PR 已做 |
| I2C / SPI(+GDMA) / LEDC/PWM / MCPWM / RMT | ✅ | PR 已做；`korvo i2c` 能扫到板载 codec 的 `0x10`/`0x14` |
| ADC | ⚠️ 能读数，**按键判定不成立** | dtsi 的 `unit` 写错一基 + `io_map` 越界，见 `docs\课程同款\07-七项外设落地实录.md` §9.1 |
| TWAI-FD（CAN） | ⚠️ 驱动在，环回 `-EIO` | TX 完成状态位报 ERR，`drivers\can\can_esp32_twaifd.c:590` |
| Wi-Fi / BLE / 802.15.4 | ❌ | 白名单不含 S31（`drivers\bluetooth\hci\hci_esp32.c:24`） |
| I2S（音频） | ❌ | `esp32s31_common.dtsi` 里没有 `i2s` 节点 |
| SD 卡（SDMMC） | ❌ | `drivers\sdhc\sdhc_esp32.c:32` 只 gate `ESP32S3/ESP32P4` |
| 并口屏 / DVP 摄像头（LCD_CAM） | ❌ | dtsi 无 `lcd_cam` 节点，`drivers\display` 里 ESP32 只有 P4 的 DSI |
| 双核 | ❌ | PR 明确只用 HP 核 |

**结论**：这块板在 Zephyr 上的定位是"RISC-V + 基础外设的实验台"。要音频/屏幕/Wi-Fi，回 ESP-IDF（乐鑫有 `esp32_s31_korvo_1` BSP），两条栈并存。

## A.4 它教会你的东西（这才是附录的价值）

### ① 一块新 SoC 进 Zephyr 要补哪几层

```
zephyr/soc/espressif/<soc>/
├── Kconfig.soc        # SOC_SERIES_xxx / SOC_xxx / 工具链名
├── Kconfig.caps       # 这个 SoC "有什么"（外设能力位）
├── Kconfig.defconfig  # 默认开哪些子系统
├── Kconfig.mac        # MAC 地址怎么取
└── soc.c / soc.h      # 启动、时钟、堆等
zephyr/dts/<arch>/espressif/<soc>/
├── <soc>_common.dtsi  # 外设地址、中断、clock、unit 编号
└── <soc>_pinctrl.h    # 每个外设能用哪些脚
zephyr/boards/espressif/<board>/   # board.yml + Kconfig.board + <board>.dts + _defconfig + pinmux
```

主线板子上这一层是"别人已经写好、你只读"，附录 A 让你**亲手看一遍**。

### ② 一个编号写错，整条通路作废

`esp32s31_common.dtsi:456` 写的是 `adc0 { unit = <1>; }`（一基），而 HAL `adc_types.h:22` 的 `adc_unit_t` 是 `ADC_UNIT_1 = 0`（零基）。驱动 `adc_esp32.c:334` 直接拿 `unit` 当数组下标：`adc_channel_io_map[unit][ch]`。于是 `adc0` 实际采的是 ADC2 的通道，IO42 从头到尾没进采样。

**这种错误在主干板子上不会出现**——因为它早就被 CI 和用户踩平了。附录 A 让你见识一次"未合并代码"的真实质量水位。

### ③ 驱动里的 SoC gate 长什么样

```c
// drivers/sdhc/sdhc_esp32.c:32
#if defined(CONFIG_SOC_SERIES_ESP32P4) || defined(CONFIG_SOC_SERIES_ESP32S3)
```

看到这种写法你就明白：**dts 里有节点 ≠ 能用**，还要驱动里认这块 SoC。判断一块新板"能不能跑某外设"，永远是三连问：

1. SoC dtsi 有节点吗？
2. pinctrl 头文件有这个外设的宏吗？
3. 驱动里有 `#if defined(CONFIG_SOC_SERIES_xxx)` 吗？

三问有一个是"没有"，结论就是"造轮子级"，不是"改个 overlay 就行"。

## A.5 和主线的对照

| 维度 | 主线 S3（主干） | 附录 A S31（PR 补丁） |
|---|---|---|
| 板目标 | `esp32s3_devkitc/esp32s3/procpu` | `esp32s31_function_coreboard/esp32s31/hpcore` |
| 架构 | Xtensa | RISC-V |
| 工具链 | `xtensa-espressif_esp32s3_zephyr-elf` | `riscv64-zephyr-elf` |
| 串口 | COM11（原生 USB-Serial-JTAG） | COM9（CP2102） |
| 镜像偏移 | `0x0000` | `0x2000` |
| ADC | 12 位 | 17 位（但驱动不可靠） |
| Wi-Fi/BLE | ✅ | ❌ |
| I2S/SD/LCD_CAM/DVP | ✅ | ❌ |
| 板载外设 | 少（BOOT 键 + RGB） | 多（WS2812、四键、codec、并口屏子板、摄像头） |

更细的对照与迁移步骤见：

- `docs\课程同款\08-教程主线选型-主干S3还是补丁S31.md`
- `docs\参考\平台现状与坑.md`
- `docs\参考\工程结构与可迁移性.md`

## A.6 自检

- S31 的 SoC 层和板级分别来自哪里？为什么说"主干没有它"？
- 为什么这块板的板目标后面必须带 `hpcore`？
- 判断"某外设能不能在这块板上跑"的三连问是什么？
- 如果要在这块板上点 4.3″ 并口屏，缺的是哪几层？（提示：`06` §4.3、`07` §2）
