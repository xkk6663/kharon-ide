# 09 · 你的 Korvo-1 上哪些外设能跑、哪些不能、为什么

> **定位（2026-11-14 更新）**：本篇是**附录 A（S31 Korvo-1）专用**配套材料，主线 S3 读者不需要读——S3 的能力清单见 `08-教程主线选型` §2 与 `07` 的 S3 部分。

> 一句话：**能跑的都是"I2C / SPI / GPIO / ADC / RMT / UART 这类通用总线上的东西"；不能跑的，全是"需要一个 S31 专属控制器（I2S / SDMMC / LCD_CAM）"的东西**。
> 因为 Zephyr 现在对 S31 只接了 SPI/I2C/UART/GPIO/ADC/LEDC/MCPWM/PCNT/RMT/TWAI/USB-Serial/WDT/TRNG，**没有接 I2S、没有接 SDMMC、没有接 LCD_CAM**——这是 SoC 层（第①层）的空白，不是你 overlay 写错。

板子：[ESP32-S31-Korvo-1 V1.1 用户指南](https://docs.espressif.com/projects/esp-dev-kits/zh_CN/latest/esp32s31/esp32-s31-korvo-1/user_guide.html)（ESP32-S31-WROOM-3，16 MB flash + 16 MB PSRAM，ES8389 codec，4-bit SDIO，DVP 摄像头，RGB 并口 LCD 子板，ADC 按键梯，WS2812 在 IO37）

---

## 0. 先回答"外设驱动是芯片无关的吗"

**分三类，一句话结论：C 代码是芯片无关的，但"能不能在这颗芯片上编/跑"取决于 SoC 层有没有把外设接出来。**

| 类型 | 例子 | 芯片相关性 | 换芯片要动什么 |
|---|---|---|---|
| **① 总线/框架驱动** | `spi_esp32_spim.c`、`i2c_esp32.c`、`adc_esp32.c`、`uart_esp32.c` | **强相关**（直接写寄存器、管中断/DMA） | 换厂商=重写；同厂商换型号=加分支 |
| **② 器件驱动** | `display_st7789v.c`、`ov2640.c`、`wm8960.c` | **基本无关**（只调 `spi_transceive` / `i2c_burst_read`） | 不动代码，只改 dts 属性 |
| **③ 依赖 SoC 外设特性的器件驱动** | `ws2812_pulse_io.c`（要 RMT）、`sdhc_esp32.c`（要 SDMMC）、`video_esp32_dvp.c`（要 LCD_CAM）、`i2s_esp32.c`（要 I2S） | **API 通用、实现绑 SoC** | 得先有 SoC 控制器，再看驱动有没有这颗芯片的分支 |

所以"外设驱动芯片无关"这句话**只对第②类成立**，而且还有个隐藏前提：

> **第②类驱动能跑，靠的是第①类驱动把同一条总线实现完整了。**
> 你这块 ST7789V 就是活例子：`display_st7789v.c` 一行芯片相关的代码都没有，但 S31 上能不能跑，取决于 `spi2` 有没有被 Zephyr 接出来（SoC dtsi 里有 `spi2@2038f000`，✅ 所以能跑）；而同一颗屏如果挂在 S3 的 `spi2`、STM32 的 `SPI1`，代码照样不用改。

**判断口诀**：驱动代码看 `#include`——只 include `<zephyr/drivers/spi.h>`/`i2c.h` 的就是②类（跨芯片）；include 了 `soc.h`、`hal/xxx_ll.h` 的就是①/③类（绑芯片）。

---

## 1. 判断"我的板上这个外设能不能跑"的四问

```powershell
# ① SoC 层有没有这个控制器（节点名 + compatible）
Select-String -LiteralPath west\zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi `
  -Pattern 'i2s|sdmmc|sdhc|lcd_cam|spi2|i2c0|rmt' 
# ② 板级有没有把它 okay 出来（你这块是上游 coreboard，不是你自己的板）
Select-String -LiteralPath west\zephyr\boards\espressif\esp32s31_function_coreboard\esp32s31_function_coreboard_esp32s31_hpcore.dts `
  -Pattern 'status = "okay"' -Context 3,0
# ③ Zephyr 有没有这个器件的驱动
Get-ChildItem -Recurse west\zephyr\drivers -Include *es8389*,*st7789*,*ov2640* | Select-Object FullName
# ④ 驱动有没有把这颗 SoC 放进支持范围（看它 @ 的条件编译）
Select-String -LiteralPath west\zephyr\drivers\sdhc\sdhc_esp32.c -Pattern 'CONFIG_SOC_SERIES'
```

四问都 ✅ 才"能跑"；①②之间缺一个 = 你要在 overlay 补；①缺 = 上游 SoC 层没接（改不动，除非 fork）；③缺 = 自己写驱动。

---

## 2. Korvo-1 V1.1 全清单（14 项，逐项给证据）

| # | 板载外设 / 接口 | 引脚 | 需要的控制器 | SoC 层有吗 | Zephyr 驱动有吗 | 结论 |
|---|---|---|---|---|---|---|
| 1 | UART0 控制台（CP2102） | IO58/IO59 | `uart0` (`espressif,esp32-uart`) | ✅ | ✅ `uart_esp32.c` | **✅ 能跑**（你已实测 COM9） |
| 2 | BOOT 键 | **IO61**（`&gpio1 29`） | `gpio0/1` | ✅ | ✅ `gpio-keys` 通用 | **✅ 能跑**（上游板级那个 `boot_button` 就是它） |
| 3 | 四个功能键 PLAY/SET/VOL-/VOL+ | IO42（ADC 电阻梯） | `adc0`/`adc1` | ✅ | ✅ `adc_esp32.c` | **✅ 能跑**，但要自己写"分压→键值"判定（不是 `gpio-keys` 那种电平） |
| 4 | WS2812 RGB LED | IO37 | `rmt` + `pulse-io` | ✅ | ✅ `ws2812_pulse_io.c` | **✅ 能跑**（你 `korvo_board.dtsi` 里已有 `rmt_korvo`，只需 IO60→IO37） |
| 5 | 左右声道 PA 使能 | IO7 | `gpio` | ✅ | ✅ | **✅ 能跑**（你已加 `pa_ctrl` 节点） |
| 6 | ES8389 codec 的**控制口**（I2C） | IO0/IO1，地址 `0x20` | `i2c0` | ✅ | ✅ `i2c_esp32.c`；❌ **无 ES8389 驱动** | **⚠️ 半能跑**：能读写寄存器（自己写初始化表），但没有现成 codec 驱动 |
| 7 | ES8389 的**音频数据口**（I2S/TDM） | IO2~IO6（MCLK/SCLK/LRCLK/SDOUT/DSIN） | `i2s` | ❌ **SoC dtsi 里没有 i2s 节点** | ✅ 有 `i2s_esp32.c` + `espressif,esp32-i2s.yaml` | **❌ 现在不能跑**（驱动在、控制器没接 → 第①层缺失） |
| 8 | microSD（4-bit SDIO） | IO20~IO25 + `SD_CTRL` IO39 | `sdhc`/`sdmmc` | ❌ 无节点 | ⚠️ 有 `sdhc_esp32.c`，但**只 gate 到 `SOC_SERIES_ESP32S3/P4`** | **❌ 默认不能跑**；绕法见 §3.2 |
| 9 | 4.3″ RGB 并口 LCD 子板（DB0~DB15 + PCLK/HS/VS/DE + SPI 初始化脚 IO60/IO61） | IO8~IO19、IO33~IO36、IO38/40/43/44/45 | `lcd_cam` | ❌ 无节点 | ❌ display 目录只有 `display_esp32_dsi.c`（P4 的 DSI） | **❌ 不能跑**（不是 ST7789V 那种 SPI 屏） |
| 10 | DVP 摄像头（8 bit + XCLK） | IO46~IO57 + IO38(GM_FK) | `lcd_cam`（DVP 部分） | ❌ 无节点 | ✅ 有 `video_esp32_dvp.c`，compatible = `espressif,esp32-lcd-cam-dvp` | **❌ 不能跑**（同上：控制器没接） |
| 11 | USB Type-A **Host** | USB_DP/DM | `usb_otg`（Host 侧） | ⚠️ `usb_otg@20300000` 有 | ❌ `drivers/usb/uhc` 里没有 esp32 实现 | **❌ Host 不能跑**；Device 侧 `usb_serial` ✅ |
| 12 | CAN / TWAI | 复用（板级 `twai0`） | `twai0` | ✅ | ✅ `can_esp32_twai.c` | **✅ 能跑** |
| 13 | PWM（LEDC / MCPWM） | 任意可复用脚 | `ledc0`/`mcpwm0`/`mcpwm1` | ✅ | ✅ `pwm_led_esp32.c` / `pwm_mc_esp32.c` | **✅ 能跑**（背光调亮度、蜂鸣器都行） |
| 14 | SPI2 / SPI3 总线（挂你自己的 SPI 小屏/传感器） | 任意 | `spi2`/`spi3` | ✅ | ✅ `spi_esp32_spim.c` | **✅ 能跑**（板级默认 disabled，overlay 里 `status="okay"` + pinctrl 即可，见 04） |
| — | SPI NAND flash | IO20~IO25 | — | — | — | 默认**不上件**，忽略 |

**统计**：板载 14 项里 **能跑 9 项**（1/2/3/4/5/12/13/14 + 6 的 I2C 控制口），**不能跑 4 项**（7 音频数据、8 SD 卡、9 LCD、10 摄像头），**Host 1 项**（11）也不支持。

---

## 3. 三块硬骨头：为什么"驱动明明有、却跑不了"

### 3.1 I2S + ES8389：控制器没接 + 器件没驱动

- `esp32s31_common.dtsi` 全文 grep `i2s` → **0 命中**（只有 `mcpwm0/1`、`pcnt`、`i2c`、`spi`、`rmt`、`twai`、`usb_otg`、`ledc0`、`adc0/1`）。
- 但 Zephyr 里 `drivers/i2s/i2s_esp32.c` 和 `dts/bindings/i2s/espressif,esp32-i2s.yaml` **都在**。
- 结论：不是"Zephyr 不支持 I2S"，是"**S31 这颗芯片的 I2S 还没被 SoC 层描述出来**"。你要用，得往 `zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi` 里加 `i2s` 节点——**这就是第①层，属于上游的活**（本地改可以，但每次 `west update` 会被覆盖）。
- 另外 ES8389 这颗 codec，Zephyr 的 `drivers/audio/` 里只有 wm8904/wm8960/wm8962/cs43l22/da7212/tlv320…，**没有 ES8389**（连 ES8311 都没有）。所以就算 I2S 通了，codec 还得自己写驱动（I2C 寄存器表 + `audio_codec` API）。
- 现实点：**要在 Zephyr 里放音频，目前 = 补 SoC I2S 节点 + 写 ES8389 驱动，两件大工程**。

### 3.2 microSD：驱动在，但明确不支持 S31

`drivers/sdhc/sdhc_esp32.c` 开头：
```c
#define DT_DRV_COMPAT espressif_esp32_sdhc_slot
#if defined(CONFIG_SOC_SERIES_ESP32P4) || defined(CONFIG_SOC_SERIES_ESP32S3)
```
→ **只有 S3 和 P4 生效**，S31 直接编不进去；S31 SoC dtsi 里也没有 `sdhc` 节点。

**绕法（可试，属 C 形态）**：SD 卡本身支持 **SPI 模式**，而你这块板的 IO20~IO25 在接 NAND 时正好复用成 `SPI2_CLK/D/Q/CS/HOLD/WP`（官方管脚表写明的）。理论上可以：
- `&spi2` 打开 + pinctrl 到 IO20~IO25；
- 用一个 `zephyr,sdhc-spi-slot` 子节点（binding：`dts/bindings/sdhc/zephyr,sdhc-spi-slot.yaml`，驱动 `sdhc_spi.c`）；
- 速率会比 4-bit SDIO 慢很多，且要确认卡的 SPI 模式接线（CS/MOSI/MISO 对应哪几根）。

> 这条我给你标成"**待验证**"：需要实机 + 一根 SPI 模式的卡才能确认，不要当既成事实。

### 3.3 RGB 并口 LCD + DVP 摄像头：都卡在 `lcd_cam`

- Zephyr 的 DVP 驱动 `video_esp32_dvp.c` 的 compatible 是 `espressif,esp32-lcd-cam-dvp`，即"用 LCD_CAM 外设的 DVP 模式"。
- `esp32s31_common.dtsi` 里 grep `lcd|cam` → **只有 mcpwm/pcnt 命中，没有 lcd_cam 节点**。
- 显示侧更彻底：`drivers/display` 里 ESP32 相关只有 `display_esp32_dsi.c`（P4 的 MIPI-DSI），**没有 LCD_CAM/8080 并口屏驱动**。
- 所以那块 4.3″ 子板（16 位并口 + PCLK/HS/VS/DE + SPI 初始化）在 Zephyr 里**当前无解**；摄像头同理。

---

## 4. 能跑的那几项，踩过的坑（都在你已有文件里）

| 项 | 坑 | 你现在的状态 |
|---|---|---|
| BOOT 键 | 板上是 **IO61**（`&gpio1 29`，gpio1 = IO32~IO61，30 个脚）。03 里写的"G29"是误读，已修正 | 上游 `boot_button` 正好就是它，`sw0` 别名可直接用 |
| 四功能键 | ADC 电阻梯，四档电压 → 要自己定阈值；`gpio-keys` 用不了 | `korvo_apps` 已 `&adc0`+`&adc1` okay，逻辑在 main.c |
| WS2812 | IO60 → **IO37** | `korvo_board.dtsi` 的 `rmt_korvo` 已改（`RMT_OUT0_GPIO37`） |
| PA_CTRL | 上游没有这个节点，且是"高=开" | `pa_ctrl` + `pa-ctrl` 别名已在 `korvo_board.dtsi` |
| I2C0 | 上游 IO51/IO50 → **IO0/IO1（ES8389 就挂这）** | `korvo_i2c.dtsi` 已改 |

> 顺便说明：这 5 处就是"上游 coreboard ≠ 你的 Korvo-1"的全部差异——**加外设=在 overlay 里补节点，不是重写板级**，这正是 04 讲的第③层。

---

## 5. 想把"不能跑"的也跑起来，三条路

| 路线 | 做什么 | 工作量 | 代价 |
|---|---|---|---|
| **A. 等上游** | 等 Zephyr/乐鑫把 S31 的 i2s / sdhc / lcd_cam 接进 SoC 层 | 0（但时间不可控） | 关注 `esp32s31_common.dtsi` 与 `soc/espressif/esp32s31` 的提交 |
| **B. 自己补 SoC 层** | 在本地 `esp32s31_common.dtsi` 加 `i2s`/`sdhc`/`lcd_cam` 节点 + 改 `sdhc_esp32.c` 的 SoC 分支 + 写 ES8389 驱动 | **大**（音频 1~2 周量级，LCD/DVP 更久） | `west update` 会覆盖，得自己维护 patch；本质是"给上游干活"的预演 |
| **C. 分工** | 控制面/UI/联网用 Zephyr（按键、LED、SPI 屏、CAN、PWM），音频和 SD 卡用 ESP-IDF 那份固件/组件 | 中 | 双固件或双核分工（S31 有 LP 核，但 LP 核跑不了这些外设） |

**给你的建议**：学习阶段走 **C 的简化版**——Zephyr 侧先把"能跑的 9 项"吃透（尤其 ADC 按键 + RMT + SPI 屏 + PWM），音频/SD/相机留到后面；真需要时再按 B 补 SoC 层，那才是"能写驱动"的分水岭。

---

## 6. 一句话总结

- **驱动芯片无关吗？** 器件驱动（②类）代码无关，但它依赖"该 SoC 的总线驱动完整"；依赖专属外设的（③类）本质绑芯片。
- **你这块板能跑什么？** 挂通用总线的都能跑：UART/I2C/SPI/GPIO/ADC/RMT/PWM/CAN/USB-Device/WS2812/SPI 小屏；**卡在 S31 专属控制器上的都不能跑**：音频 I2S（+ES8389 无驱动）、microSD（SDIO）、RGB 并口屏、DVP 摄像头、USB Host。
- **为什么？** 不是你的 overlay 写错，是 `esp32s31_common.dtsi` 里**根本没有 `i2s`/`sdhc`/`lcd_cam` 这三个节点**，属于第①层（SoC）的空白——这一层改起来就是"给上游干活"。

---

## 7. 补查：Korvo-1 相关的"芯片无关"器件驱动，在本仓库里到底有没有

### 7.0 先纠正一个说法：不是"乐鑫的芯片没跑通"

准确的三层说法是：

| 层 | 状态 | 证据 |
|---|---|---|
| **芯片本体** | ✅ 完全没问题 | ESP32-S31 规格书里的 I2S / SDIO / LCD_CAM / DVP 都在 |
| **乐鑫自己的软件栈（ESP-IDF）** | ✅ 支持 | 出厂 demo 就能点那块 4.3″ 屏、录放音、拍视频；`esp_board_manager` 组件把 LCD/codec/按键/LED 都封装好了 |
| **Zephyr 这套栈** | ⚠️ **移植没跟上** | `esp32s31_common.dtsi` 无 `i2s`/`sdhc`/`lcd_cam`；`sdhc_esp32.c` 只 gate 到 S3/P4 |

Zephyr 的乐鑫 SoC 支持是"社区 + 乐鑫"共同维护，新芯片（S31 是较新的型号）通常滞后一两个版本。所以**"跑不通"是软件栈进度问题，不是硬件缺陷**——同一块板刷 ESP-IDF 立刻什么都能跑。

### 7.1 器件驱动（②类，芯片无关）逐项查表

| 器件 | compatible | 驱动文件 | 有吗 | 在你板上能用吗 |
|---|---|---|---|---|
| ST7789V | `sitronix,st7789v` | `display_st7789v.c` | ✅ | ✅ 挂 SPI2/SPI3 就能跑（04 那套） |
| ST7735R | `sitronix,st7735r` | `display_st7735r.c` | ✅ | ✅ 同上 |
| ILI9341 / ILI9342C | `ilitek,ili9341` / `ili9342c` | `display_ili9341.c` / `display_ili9342c.c` | ✅ | ✅ 同上 |
| GC9X01X（1.28″ 圆屏） | `galaxycore,gc9x01x` | `display_gc9x01x.c` | ✅ | ✅ 同上（waveshare 那块板就是它） |
| SSD1306 OLED | `solomon,ssd1306-i2c` / `-spi` | `display_ssd1306.c` | ✅ | ✅ I2C0 上直接挂 |
| 触摸 GT911 | `goodix,gt911` | `input_gt911.c` | ✅ | 你这块板没触摸；将来贴触摸屏可用 |
| 触摸 CST8XX | `hynitron,cst8xx` | `input_cst8xx.c` | ✅ | 同上 |
| 触摸 FT5336 / FT6146 | `focaltech,ft5336` / `ft6146` | `input_ft5336.c` / `input_ft6146.c` | ✅ | 同上 |
| **ADC 电阻梯按键** | **`adc-keys`** | **`input_adc_keys.c`** | **✅** | **✅ 你板上 IO42 那四个键可以直接用（见 §7.2）** |
| WS2812（RMT 版） | `worldsemi,ws2812-rmt`（pulse-io） | `ws2812_pulse_io.c` | ✅ | ✅ IO37（你已配 `rmt_korvo`） |
| WS2812（SPI 版） | `worldsemi,ws2812-spi` | `ws2812_spi.c` | ✅ | ✅ 备选（省 RMT） |
| 音频 codec **ES8389** | — | **无** | ❌ | ❌ 板上就是它，只能自己写（I2C 寄存器表） |
| 音频 codec ES8311 / ES7210 | — | **无** | ❌ | — |
| 音频 codec（Zephyr 有的） | wm8904 / wm8960 / wm8962、cs43l22、da7212、tlv320… | ✅ | ✅ | ❌ 都走 I2S，S31 没 I2S → 用不上 |
| 摄像头 GC2145 / OV2640 / OV5640 / OV7670… | `galaxycore,gc2145`、`omnivision,ov2640`… | `gc2145.c`、`ov2640.c`… | ✅ | ❌ 驱动在，但没 DVP 控制器 |
| SD 卡（SPI 模式） | `zephyr,sdhc-spi-slot` | `sdhc_spi.c` | ✅ | ⚠️ 理论可绕（§3.2，待验证） |
| SD 卡（SDIO） | `espressif,esp32-sdhc-slot` | `sdhc_esp32.c` | ⚠️ 仅 S3/P4 | ❌ |

**结论**：**"芯片无关的器件驱动基本都齐"**——屏幕(5 类)、触摸(4 类)、ADC 按键、WS2812 两个变体全在；**真正缺的只有 ES8389 这一颗 codec**。其余用不了的，缺的不是"驱动"，是"控制器"（第①层）。

### 7.2 一个实用收获：IO42 那四个键，不用手写阈值

Zephyr 有现成的电阻梯按键驱动 `input_adc_keys.c`（binding：`dts/bindings/input/adc-keys.yaml`，`compatible = "adc-keys"`），它**走的就是第四课那套 input 子系统 + `INPUT_CALLBACK_DEFINE`**，比你在 `main.c` 里自己读 ADC 判阈值干净得多。

```dts
#include <zephyr/dt-bindings/input/input-event-codes.h>

/ {
	adc_buttons: adc-keys {
		compatible = "adc-keys";
		io-channels = <&adc0 0>;        /* 通道号按 ESP32-S31 规格书 GPIO42↔ADC 通道表填，别猜 */
		keyup-threshold-mv = <3000>;    /* 全松开时的电压（实测后填） */
		sample-period-ms = <20>;

		key_play: key_play {
			press-thresholds-mv = <2200>;   /* 实测各档电压 */
			zephyr,code = <INPUT_KEY_0>;
		};
		key_set: key_set {
			press-thresholds-mv = <1600>;
			zephyr,code = <INPUT_KEY_1>;
		};
		key_vol_down: key_vol_down {
			press-thresholds-mv = <1000>;
			zephyr,code = <INPUT_KEY_2>;
		};
		key_vol_up: key_vol_up {
			press-thresholds-mv = <400>;
			zephyr,code = <INPUT_KEY_3>;
		};
	};
};
```

`prj.conf`：
```conf
CONFIG_INPUT=y           # ← 还是要手动开（你踩过：不会因为 dts 有节点就自动开）
# CONFIG_INPUT_ADC_KEYS 不用写：Kconfig.adc_keys 里 default y，节点一进树就自动编
```
两个必须注意：
1. **`io-channels` 的通道号不能猜**——GPIO42 落在哪个 ADC 单元的哪个通道，按规格书 ADC 章节查；填错就是"永远读同一个数"。
2. **`press-thresholds-mv` 必须实测**——先烧一个只打印 `adc_raw_to_millivolts` 结果的版本，把四档电压量出来再填；Zephyr 这个驱动**不会**帮你算电阻分压。

> 这个对比很值得记住：`CONFIG_INPUT` 没有 `default y`（所以漏开就链接失败），`CONFIG_INPUT_ADC_KEYS` 有 `default y`（所以自动生效）。**判断"某个驱动要不要手动开"，就去 `drivers/*/Kconfig.<驱动>` 里看它有没有 `default y`**。

---

## 8. 更新后的总结

1. **"乐鑫芯片没跑通"这个说法不成立**：硬件和 ESP-IDF 都完整支持；卡住的是 Zephyr 对 S31 的 SoC 层移植。
2. **芯片无关的器件驱动，Zephyr 里基本都在**：屏幕 5 类、触摸 4 类、ADC 按键、WS2812 两种——**唯一缺的是 ES8389 codec**。
3. **你这块板"能跑"的清单不变**：UART/I2C/SPI/GPIO/ADC/RMT/PWM/CAN/USB-Device + 上述器件驱动；"不能跑"的四项（I2S 音频、SDIO 卡、RGB 屏、DVP 相机）**缺的是控制器，不是驱动**。
4. **判层口诀不变**：器件驱动管"器件"，控制器管"芯片"，overlay 管"你这块板"，SoC dtsi 管"这颗芯片有没有这个外设"。

---

## 9. "等官方" 还是 "自己上"：查完证据后的结论

### 9.1 三个关键事实（都来自本机实测）

| 事实 | 证据 | 含义 |
|---|---|---|
| **乐鑫 HAL 已经把 S31 的外设料全给了** | `west\modules\hal\espressif\components\` 下有 `esp_hal_i2s\esp32s31`、`esp_hal_sd\esp32s31`、`esp_hal_lcd\esp32s31`、`esp_hal_cam\esp32s31`、`esp_hal_parlio\esp32s31`、`esp_hal_jpeg\esp32s31`、`esp_hal_asrc\esp32s31` | 缺的不是"底层寄存器驱动"，只是 Zephyr 这层的**接线**（dts 节点 + 驱动里的 SoC 分支） |
| **你的 Zephyr 是"浅克隆 + pr-s31 分支"** | `git rev-parse --is-shallow-repository` = **true**；分支 `pr-s31`；remotes = `gh`(github.com/zephyrproject-rtos/zephyr) + `origin`(gitee.com/mirrors/zephyr)；`git rev-list --count HEAD` = 1（**depth=1 的浅克隆，所以只看到 1 条提交，不代表仓库只有一次提交**） | 分支名 `pr-s31` 说明这份树是按某个 **S31 相关 PR 分支**准备的，**S31 支持很可能还没进 main**。等官方 = `fetch` 新分支/新快照，不是随便 `git pull` |
| **本机到 GitHub 不通** | `git ls-remote gh` → `Failed to connect to github.com port 443 after 21184 ms` | 想跟进上游只能走 `origin`（gitee 镜像）或离线拿包；`gh` 这条路在你机器上目前是断的 |

结论一句话：**这件事不是"等不等"，而是"哪些值得你自己补"。**

### 9.2 三档分法（按"投入产出比"排序）

| 档 | 做什么 | 工作量 | 为什么这么排 |
|---|---|---|---|
| **① 现在就做（不用等任何人）** | overlay 层：SPI 屏（04）、`adc-keys` 按键（§7.2）、WS2812、PWM 背光、I2C 传感器、CAN | 几小时 | 全是第③层，改 2 个文件；**学的是"框架怎么用"，与芯片支不支持无关** |
| **② 值得自己补（最佳练手项目）** | **SD 卡**：`esp32s31_common.dtsi` 加 `sdhc` 节点 + `sdhc_esp32.c` 加 S31 分支 | 1~3 天 | HAL `esp_hal_sd/esp32s31` 已在；驱动里已有 **S3 分支可照抄**；成功标志明确（能挂载 FAT 读文件）。这是"第一次干上游级改造"的最佳标的 |
| **③ 暂缓（周级、风险高）** | 音频 I2S + ES8389；RGB 并口屏 + DVP 相机 | 1~3 周 | I2S 要动时钟树 + TDM 格式（ES8389 官方明确要求 DAC/ADC 都走 TDM）+ 还要**从零写 ES8389 codec 驱动**；LCD_CAM 要写 display 驱动 + 时序 + PSRAM 带宽。而且改完一换快照就没了 |

### 9.3 如果项目真需要音频/相机，别硬扛

**短期最优解**：控制面（按键、LED、屏、联网、CAN）用 Zephyr，音频/相机用 ESP-IDF 那套（出厂 demo 就是完整参考）。两块固件分工，比在 Zephyr 里补 SoC 层快一个数量级。

**长期才考虑**：等 §9.4 三个信号全绿，或者你自己按 ② 档起步、逐步把 ③ 档啃下来并提 PR——那才叫真正"干上游的活"（也是简历上最值钱的一段）。

### 9.4 自检：上游到底补没补（换新快照后跑这三条）

```powershell
# 信号 1：SoC 层有没有出现这三个节点（最硬的信号）
Select-String -LiteralPath west\zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi `
  -Pattern 'i2s:|sdhc:|lcd_cam:'

# 信号 2：驱动有没有加 S31 分支
Select-String -LiteralPath west\zephyr\drivers\sdhc\sdhc_esp32.c -Pattern 'ESP32S31'
Select-String -LiteralPath west\zephyr\drivers\i2s\i2s_esp32.c   -Pattern 'ESP32S31'

# 信号 3：上游有没有出 korvo 板级（有的话说明有人在用这块板）
Test-Path -LiteralPath west\zephyr\boards\espressif\esp32s31_korvo_1
```

**三个信号全绿 = 可以换过去白拿**；只有一个绿 = 还早，继续按 ① ② 档走。

### 9.5 自己改 Zephyr 树，怎么不被"换快照"冲掉

因为你的树没有 remote、只有 1 个提交，最实用的维护方式是**存 patch**：

```powershell
# 改完先落一份 diff（相对当前快照）
Set-Location -LiteralPath west\zephyr
git diff > ..\..\patches\s31-sdhc.patch      # 目录不存在就先 New-Item
# 换新快照后：
git apply ..\..\patches\s31-sdhc.patch
```

三条纪律：
1. **只改"接线"**（dts 节点 + `#if defined(CONFIG_SOC_SERIES_ESP32S31)` 分支），不要动通用逻辑——patch 越薄，越容易 apply，也越接近能被上游接受的样子。
2. **先做 ②档（SD 卡）**：它有 S3 先例、成功判据明确，改错了能立刻发现。
3. **别碰 `hal_espressif`**：HAL 已经支持 S31，动了它等于给自己挖坑。

### 9.6 一句话回答

**不用等，也不用一上来全自己上**：能立刻玩的（SPI 屏、ADC 按键、LED、PWM、CAN）今天就能做；真想要 SD 卡就自己补（1~3 天，有 S3 可抄，成功判据明确）；音频和相机别自己扛——要么等信号全绿，要么交给 ESP-IDF。

**而且"等"在你这里并不便宜**：树是 `depth=1` 的浅克隆、停在 `pr-s31` 分支，GitHub 又连不上（只能走 gitee 镜像或离线包）。等来的不确定性，比花 1~3 天把 SD 卡补通要大。

---

## 10. WiFi / 蓝牙属于哪一层？（不是"通用外设层"，也不是纯 SoC 层）

### 10.1 它是三层叠加，比 I2C/SPI 复杂

| 层 | 内容 | 位置 | 换芯片要动吗 |
|---|---|---|---|
| **协议栈（芯片无关）** | TCP/IP（`subsys/net`）、Bluetooth host（`subsys/bluetooth`） | Zephyr 通用 | 不动 |
| **胶水驱动（家族共享）** | `drivers/bluetooth/hci/hci_esp32.c`、`drivers/wifi/esp32/` | 一颗 `.c` 覆盖整个 ESP32 家族，靠 `#if CONFIG_SOC_SERIES_*` 分叉 | **只往白名单里加芯片** |
| **固件/PHY（严格按型号）** | `modules/hal/espressif/components/` 下的 `esp_wifi\esp32s31`、`esp_phy\esp32s31`、`bt\controller` | 乐鑫 HAL，每颗芯片独立固件与 RF 校准 | 每颗芯片各一份 |

节点本身放在**第①层 SoC dtsi**：

```dts
/* esp32s31_common.dtsi:28 */   zephyr,bt-hci = &esp32_bt_hci;
/* :72-76 */  esp32_bt_hci: esp32_bt_hci { compatible = "espressif,esp32-bt-hci"; status = "disabled"; };
/* :86-89 */  wifi: wifi { compatible = "espressif,esp32-wifi"; status = "disabled"; };
```

所以直觉上"算 SoC 层"大体对，但**能不能编、能不能通，取决于中间那层胶水的白名单**。

### 10.2 你这颗 S31 的实测结论：节点在、HAL 在、**胶水不在**

| 检查项 | 结果 | 证据 |
|---|---|---|
| SoC 层有节点 | ✅ | `esp32s31_common.dtsi:28 / 72-76 / 86-89`（默认 `disabled`，要板级/overlay 里 okay） |
| HAL 有 S31 固件/PHY | ✅ | `components/esp_wifi/esp32s31`、`components/esp_phy/esp32s31` |
| **BT 胶水支持 S31** | ❌ | `hci_esp32.c:24` 门禁 = `ESP32 / ESP32S3 / ESP32C3`；`:399-413` 的 SoC 分支只有 ESP32/S3/C2/C3/C5/C6/C61/H2，**没有 S31** |
| **WiFi 胶水支持 S31** | ❌ | `drivers/wifi/esp32/Kconfig.esp32:479-481`、`:503-505` 的依赖清单里**没有 SOC_SERIES_ESP32S31** → `CONFIG_WIFI` 对 S31 根本不出现 |

**结论：S31 的 WiFi / BLE 现在编不进 Zephyr。** 但注意——它缺的东西和 I2S/SDIO 不一样：

- I2S / SDIO：缺 **SoC 层节点**（连"有这么个外设"都没描述）
- WiFi / BLE：节点和固件都在，缺的是**驱动里的一行 `#if` / 一行 Kconfig 依赖**

### 10.3 所以"补"的难度排序完全不同（重要）

| 目标 | 缺什么 | 代码量 | 风险 |
|---|---|---|---|
| **SD 卡** | SoC 节点 + `sdhc_esp32.c` 加 S31 分支 | 中 | **低**：成功判据明确（能挂载 FAT 读文件） |
| **I2S 音频** | SoC 节点 + 时钟树 + ES8389 驱动 | 大 | 中：能听到声音就知道对不对 |
| **BLE** | `hci_esp32.c` 门禁加 S31 + 一个分支 + 板级 `&esp32_bt_hci { status="okay"; }` | **小** | **高**：射频/固件不匹配时表现为"能编译、能启动、但连不上或频繁断连"，几乎无法定位 |
| **WiFi** | `Kconfig.esp32` 依赖加 S31 + glue 分支 | 小 | **同样高**：PHY 校准比 BLE 更敏感 |

**一句话：WiFi/BT 的门槛表面最低（几行 `#if`），实际风险最高（射频问题不可调试）。** 这也是为什么"等"在这两项上反而合理。

### 10.4 想立刻用无线，有三条现成路

1. **S3 那块板先学**：S3 的 BT 是完整支持的（你已有 `build\samples\esp32s3\peripheral\default`），BLE 的 API/GATT/广播写法完全一样，等 S31 支持齐了再迁。
2. **走 ESP-IDF**：S31 的 `esp_wifi`/`esp_phy` 是乐鑫自家就完整的，出厂 demo 直接能连。
3. **外挂模组**：Zephyr 有 `wifi_esp_at` 驱动与 `esp_hosted` 方案，把无线丢给一颗独立模组/另一颗芯片，主控只管串口/SDIO——这样你的 Korvo-1 完全不用碰 S31 的 RF。

> 补充：板上模组还支持 IEEE 802.15.4（Thread/Zigbee），HAL 里同样有 `esp_hal_ieee802154/esp32s31`；Zephyr 侧对应驱动我这次没有逐项核对，用之前请按 §1 的四问再走一遍。
