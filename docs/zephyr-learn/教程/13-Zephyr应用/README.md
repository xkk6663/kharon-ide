# Zephyr 应用（Korvo-1 实机版）

> **现状校准（2026-11-19）**：本组是 S31 学习线的应用层；当前仓库主线换成 F411 后，
> “一条线程持续跑 + 一组业务命令”的组织方式没有变，最小样例是
> [`applications/stm32f4/f411_breath`](../../../../applications/stm32f4/f411_breath)
> （见 [`tutorials/10`](../../../tutorials/10-Zephyr模式上手.md)）。

驱动层教的是「一个 API 怎么调通」，应用层要回答的是另一个问题：**这几颗硬件凑在一起，能做出什么功能，代码该怎么组织**。

这一组页面讲的就是这件事。和 `11-Zephyr驱动` 的区别：

| | 驱动层 `applications/esp32s31/korvo_lab` | 应用层 `applications/esp32s31/korvo_apps` |
|---|---|---|
| 关注点 | 单个 API 的入参、返回值、驱动行为 | 模式、状态、帧率、用户命令、硬件联动 |
| 代码形态 | 一个命令测一个接口 | 一条线程持续跑 + 一组业务命令 |
| 判据 | 返回值、寄存器、总线扫描 | 灯真的在动、按键真的被认出来、帧率真的够 |
| 会不会碰寄存器 | 会（`lab key` 直接读 `IN1`） | 不会，只调驱动 |

> 板子：ESP32-S31-Korvo-1 V1.1（`esp32s31_function_coreboard/esp32s31/hpcore`）
> 树：`west`，`VERSION` 4.5.99 + PR #119851，commit `34e025569cbb`
> 串口：COM9 / 115200 8N1
> 工程：`applications/esp32s31/korvo_apps`，构建目录 `build\esp32s31\korvo_apps\s31-debug`

---

## 一、板上到底有什么（官方硬件资源清单）

来源：[ESP32-S31-Korvo-1 用户指南](https://docs.espressif.com/projects/esp-dev-kits/zh_CN/latest/esp32s31/esp32-s31-korvo-1/user_guide.html)。下表只保留「能被软件摸到」的部分。

| 资源 | 接线 / 器件 | 软件入口 |
|---|---|---|
| 可寻址 RGB 灯 | WS2812 单灯，**GPIO37** | RMT 输出通道 0 |
| 4 个功能键 | PLAY / SET / VOL- / VOL+，**四键共 IO42**，电阻分压 | ADC1_CH0（IO42） |
| BOOT 键 | **IO61**（bank1 pin29） | GPIO 输入 |
| RESET 键 | EN 引脚 | 硬件复位，软件无关 |
| 功放使能 | **PA_CTRL = GPIO7**，双 NS4150B | GPIO 输出 |
| 音频 codec | ES8389，I2C 0x10/0x14 + I2S | i2c0 / i2s |
| 双麦克风 | 接 ES8389 | I2S |
| 左右喇叭 | 经 NS4150B | — |
| microSD | SDIO 3.0 4 线 | sdmmc |
| 摄像头 | DVP，13 根 IO | — |
| LCD 子板 | 4.3" 8080 并口 + I2C 触摸，23 根 IO | — |
| USB2.0 Type-A | host | usb |
| Flash / PSRAM | 16 MB + 16 MB | — |
| 无线 | Wi-Fi 6 / BLE 5.4 / 802.15.4 | — |

### 一.1、Zephyr 侧可跑性矩阵（本树实测过，不是猜）

| 资源 | 状态 | 依据 |
|---|---|---|
| RGB（WS2812） | ✅ 可跑 | `pulse_io` + RMT 驱动齐全，`app rgb` 五模式实测 |
| ADC（IO42 四键） | ✅ 可跑 | `adc0` 17 位，`app key` 四键全部识别 |
| 功放 PA_CTRL | ✅ 可跑 | `gpio_pin_set_dt` 返回 0，实测导出 |
| I2C（ES8389） | ✅ 可跑 | 扫到 0x10、0x14，能读寄存器 |
| BOOT 键（IO61） | ❌ 不可用 | bank1 驱动未闭环：能配回调，但中断/轮询都读不到电平，详见 `11-Zephyr驱动/GPIO.md` |
| PWM（LEDC） | ⚠️ 有节点无负载 | `ledc0` 默认 disabled，板上没接可观测负载 |
| I2S / ES8389 放音 | ❌ 无驱动 | 本树没有 i2s 驱动实例 |
| LCD / 触摸 / 摄像头 | ❌ 无驱动 | 无 ST7262E43 / GT1151 / DVP 驱动 |
| SDIO / microSD | ❌ 无驱动 | 无 sdmmc 实例 |
| Wi-Fi / BLE | ❌ 未合入 | 只有草稿 PR |

**结论：这块板子目前能用 Zephyr 玩出「完整功能闭环」的，只有三样——RGB、ADC 按键、GPIO 功放。** 应用层教程就围绕这三样做，其余资源在 `未实测的部分.md` 里如实标注，不假装能跑。

---

## 二、工程结构

```
applications/esp32s31/korvo_apps/
├── CMakeLists.txt
├── prj.conf          # GPIO / ADC / LED_STRIP / WS2812_STRIP_PULSE_IO / SHELL / LOG
├── app.overlay       # PA_CTRL 别名、RMT 引脚、adc0 & adc1 打开
└── src/main.c        # 模式状态机 + 帧计算 + 25 fps 线程 + shell 命令
```

`prj.conf` 全文（每一行都有用，没有默认值可以省）：

```conf
CONFIG_CONSOLE=y
CONFIG_SERIAL=y
CONFIG_UART_CONSOLE=y

CONFIG_GPIO=y          # PA_CTRL
CONFIG_ADC=y           # IO42 四键
CONFIG_LED_STRIP=y     # WS2812
CONFIG_WS2812_STRIP_PULSE_IO=y   # 时序后端：pulse_io（不是 RMT 直连）

CONFIG_SHELL=y
CONFIG_SHELL_BACKEND_SERIAL=y
CONFIG_SHELL_STACK_SIZE=4096

CONFIG_LOG=y
# 只保留 error：adc_esp32 每次采样都会打 uncompensated 警告，会把串口刷满
CONFIG_LOG_DEFAULT_LEVEL=1

CONFIG_MAIN_STACK_SIZE=2048
CONFIG_HEAP_MEM_POOL_SIZE=4096
```

`app.overlay` 全文：

```dts
/ {
	leds {
		compatible = "gpio-leds";
		pa_ctrl: pa_ctrl_0 {
			gpios = <&gpio0 7 GPIO_ACTIVE_HIGH>;
			label = "PA_CTRL";
		};
	};

	aliases {
		pa-ctrl = &pa_ctrl;
	};
};

&pinctrl {
	rmt_korvo: rmt_korvo {
		group1 { pinmux = <RMT_OUT0_GPIO37>; };
	};
};

&rmt {
	pinctrl-0 = <&rmt_korvo>;
	pinctrl-names = "default";
	status = "okay";
};

/* 两个 ADC 单元都打开，用来找出 IO42 上 4 个功能键落在哪个通道 */
&adc0 { status = "okay"; };
&adc1 { status = "okay"; };
```

三处细节值得记：

1. `pa_ctrl` 没走 `gpio-keys`/`gpio-leds` 里的现成节点，而是自己建了一个 `gpio-leds` 子节点，只为拿到 `GPIO_DT_SPEC_GET` 这套「引脚 + 标志位」的写法。应用层只认 `DT_ALIAS(pa_ctrl)`，引脚号是编译期从设备树里取的。
2. `CONFIG_WS2812_STRIP_PULSE_IO` 依赖 `DT_HAS_WORLDSEMI_WS2812_PULSE_IO_ENABLED`，并 `select PULSE_IO`——**它不 select RMT**。RMT 节点必须自己 `status = "okay"`，否则灯一粒都不亮，而且全程没有任何报错。
3. `adc1` 是不是 okay，直接影响 `main.c` 里 `DEVICE_DT_GET` 能不能编过。所以代码里用 `#if DT_NODE_HAS_STATUS(...)` 保护，换板子时不会编译爆炸。

---

## 三、构建 + 烧录 + 抓输出

```powershell
. .\scripts\kharon-env.ps1                       # 里面设好了 ZEPHYR_BASE / TOOLCHAIN_HOME / CROSS_COMPILE（正斜杠）
cd west
west build -b esp32s31_function_coreboard/esp32s31/hpcore ..\applications\esp32s31\korvo_apps -d build\esp32s31\korvo_apps\s31-debug
west flash -d build\esp32s31\korvo_apps\s31-debug --esp-device COM9
```

本机构建结果（`build\esp32s31\korvo_apps\s31-debug`）：

```
Memory region         Used Size  Region Size  %age Used
           FLASH:      145664 B   16776960 B      0.87%
     sram0_0_seg:       83712 B     503728 B     16.62%
       irom0_0_seg:       53376 B        16 MB      0.32%
```

`zephyr.bin` = 236.13 KB，烧录偏移 `0x2000`。

串口抓命令用仓库里的 `tools/zephyr/serial_cmd.py`，`cmd@秒数` 指定这条命令的采样窗口：

```powershell
.\.deps\venv\Scripts\python.exe tools\zephyr\serial_cmd.py COM9 "app stats" "app rgb rainbow@3" "app stats"
.\.deps\venv\Scripts\python.exe tools\zephyr\serial_cmd.py COM9 "app key 100@20"     # 20 秒内按键
.\.deps\venv\Scripts\python.exe tools\zephyr\serial_cmd.py COM9 "app adc 16@8"
```

启动首屏（原样）：

```text
uart:~$ *** Booting Zephyr OS build 34e025569cbb ***

=== Korvo-1 apps ===
app rgb status|rainbow|off
app rgb solid 20 0 0
app rgb breath 0 0 60
app bright 30
app pa on|off
app stats
uart:~$
```

### 三.1、命令速查

| 命令 | 作用 | 实测返回 |
|---|---|---|
| `app stats` | 模式 / 亮度 / 帧数 / 单帧耗时 / 最后一帧颜色 | `[stats] 模式=status 亮度=60% 帧数=74 单帧耗时=2 ms 最后颜色=00 00 00` |
| `app rgb status` | 心跳灯（亮200/灭200/亮200/灭1400 ms，周期 2 s） | `[rgb] 模式 = status, ...` |
| `app rgb solid 20 0 0` | 纯色常亮（r g b 各 0–255） | `[rgb] 模式 = solid, 基色 = 14 00 00` |
| `app rgb breath 0 0 60` | 单色呼吸，三角波 2.4 s 一周期 | `[rgb] 模式 = breath, 基色 = 00 00 3c` |
| `app rgb rainbow` | 彩虹，3.6 s 转一圈色相 | 最后一帧 `00 3c 28` |
| `app rgb off` | 关灯 | `[rgb] 模式 = off, ...` |
| `app bright 15` | 全局亮度 0–100，下一帧生效 | `[bright] 亮度 = 15%` |
| `app pa on \| off` | PA_CTRL(GPIO7) 拉高/拉低 | `[pa] PA_CTRL(GPIO7) = 1, 返回 = 0` |
| `app adc <n>` | 两个 ADC 单元 × 8 通道，n 次、每 200 ms，带时间戳 | 见 `功能按键与功放.md` |
| `app key <n>` | IO42 四键识别 + RGB 反馈，n 次、每 100 ms | 见 `功能按键与功放.md` |
| `app saved` | 列出 NVS 里记住的模式 / 基色 / 亮度 / 功放 / 开机次数 | `模式 = solid (1)` / `基色 = 14 00 00` / `亮度 = 15%` / `功放 = off` |
| `app clear` | 清空这 4 个键，下次上电回默认 | `[clear] 已清空 NVS 里的模式/基色/亮度/功放` |

> 应用层现在带**记忆**：改完 `rgb`/`bright`/`pa` 会顺手写进 NVS，复位后 `[boot] 恢复配置：…` 会把上次的状态贴回来。这段代码怎么写的、为什么恢复必须放在起线程之前、键 id 为什么要单独划 20-23 这一段，都在 `14-Zephyr系统与进阶\NVS持久化.md` 第 8 节。

## 四、页面索引

| 页面 | 内容 | 实测状态 |
|---|---|---|
| [RGB.md](RGB.md) | 从「点亮一粒灯」到「25 fps 动画引擎」：模式状态机、帧计算、HSV、亮度、线程与帧率 | ✅ 五模式 + 亮度 + 帧数全有输出 |
| [功能按键与功放.md](功能按键与功放.md) | IO42 四键 ADC 分压原理、原始值↔mV 换算、消抖与长按、PA_CTRL(GPIO7)、BOOT 键为什么不可用 | ✅ 四键全部识别 |

---

## 五、这一层最该记住的一件事

驱动层的判据是「返回 0 吗」，应用层的判据是「**用户看得见什么**」。

- `led_strip_update_rgb` 返回 0，不代表灯亮了——`bright 15` 之后最后一帧是 `03 00 00`，肉眼几乎看不见，但代码是对的。
- `app rgb rainbow` 打一行「模式 = rainbow」不代表彩虹在转——要看 `app stats` 的 `最后颜色` 两次采样是否不同（`00 3c 28` vs `00 00 00`）。
- `app key` 打出 `SET` 不代表按键真的触发——要看按下与松开之间 `raw` 从 0 跳到 284 再回 0。

**每一层都要有一路独立观测**，这是这套教程从驱动篇一路贯穿到应用篇的东西。

---

下一篇：[RGB](RGB.md)
