# 06 · S31 后续需求盘点：现在能做的全部 + 屏幕/LVGL 可行性

> **定位（2026-11-14 更新）**：本篇属**附录 A（S31 Korvo-1）**的后续规划，主线 S3 读者可跳过。主线要接屏请走 `04` 的方法论（把 overlay 换成本板 dts）。

> 回答两个问题：
> 1. **WiFi/蓝牙是不是等官方适配？** —— 是，等最省事；自己改白名单代码量小，但**射频风险高**，不建议现在动（理由见 §1）。
> 2. **"现在就做的全部" + 更新教程 + 跑通显示屏 + LVGL 点屏，能实现吗？** —— **"现在就做的全部"可以（7 项，全是应用层）**；**点屏要分两种**：外挂 SPI 屏 ✅ 能跑，板载 4.3″ RGB 并口屏 ❌ 现在不能跑；**LVGL 点屏在 SPI 屏上可行，但你的 workspace 缺一个模块**（见 §4）。

---

## 0. 一页结论

| 需求 | 可行性 | 阻塞点 | 结论 |
|---|---|---|---|
| 把"现在就做的"7 项全跑通 | ✅ 可以 | 无（全是第③层 overlay） | 按 §2 清单推进，逐项验收 |
| 外挂 SPI 小屏（ST7789V/ILI9341/GC9x01x） | ✅ 可以 | 需要买屏 + 接线 | 1 天量级 |
| **板载 4.3″ RGB 并口屏**（LCD 子板） | ❌ 现在不行 | S31 无 `lcd_cam` 节点、Zephyr 无 8080/RGB display 驱动 | 等上游 / 走 ESP-IDF |
| **LVGL 点屏**（在 SPI 屏上） | ⚠️ 可以，但有前置 | **`modules/lib/gui/lvgl` 没拉取**（本机 Test-Path = False）+ GitHub 不通 | 先解决模块来源，再谈点屏 |
| 中文字显示 | ⚠️ 要额外做 | LVGL 自带 Montserrat 无中文 | 需要用字库工具转 `.c`（见 §5.4） |
| WiFi / BLE | ❌ 现在不行 | 胶水驱动白名单不含 S31（§10 已证） | 等；或 S3 练手 / ESP-IDF / 外挂模组 |

---

## 1. WiFi / 蓝牙：是，等官方适配最划算

**为什么"等"比"自己改"划算**（不是懒，是风险结构决定的）：

- 代码量看起来很小：`hci_esp32.c:24` 的门禁加一个 `CONFIG_SOC_SERIES_ESP32S31`、`Kconfig.esp32` 的依赖清单加一项。
- 但**失败模式极难排查**：射频/PHY 不匹配时表现为"能编译、能启动、串口正常、就是连不上或几分钟断一次"，没有寄存器可看、没有日志可读。这跟 SPI 屏花屏（一眼能看出）完全不同。
- 而且 HAL 层的 `esp_wifi/esp32s31`、`esp_phy/esp32s31` 是乐鑫给的**二进制固件 + 校准数据**，胶水怎么初始化它们有芯片差异，改错了不一定报错。

**"等"的判定信号**（换版本后跑，三条全绿才值得动）：

```powershell
Select-String zephyr-workspace\zephyr\drivers\bluetooth\hci\hci_esp32.c -Pattern 'ESP32S31'
Select-String zephyr-workspace\zephyr\drivers\wifi\esp32\Kconfig.esp32 -Pattern 'ESP32S31'
Select-String zephyr-workspace\zephyr\dts\riscv\espressif\esp32s31\esp32s31_common.dtsi -Pattern 'wifi:|esp32_bt_hci:'
```

**等的时候不浪费**：
- S3 那块板 BT 是完整支持的（你有 `build\samples\esp32s3\peripheral\default`），BLE 的 GATT/广播/连接写法两边通用；
- 或走 `wifi_esp_at` / `esp_hosted`，把无线交给外挂模组，Korvo-1 完全不碰 S31 的 RF。

---

## 2. "现在就做的全部"：7 项清单（含验收标准）

| # | 项目 | 要改/要写 | 验收标准（能复现就算过） | 预计 |
|---|---|---|---|---|
| 1 | **ADC 四功能键**（`adc-keys`） | overlay 加 `adc-keys` 节点 + `CONFIG_INPUT=y`；删掉手写阈值 | 串口打印 `INPUT_KEY_0~3` 对应 PLAY/SET/VOL-/VOL+ | 1 h |
| 2 | **WS2812**（IO37） | 复用 `korvo_board.dtsi` 的 `rmt_korvo`，改 IO37 | 三色循环/呼吸 | 0.5 h |
| 3 | **PWM**（背光/蜂鸣） | `&ledc0` 通道 + `pwm_led_esp32.c` | 占空比 0→100% 亮度平滑 | 1 h |
| 4 | **SPI 屏点亮**（ST7789V） | 04 §3 那套（`mipi_dbi` + 面板节点 + pinctrl `SPIM2_*`） | 全屏红/绿/蓝纯色切换 | 2–4 h |
| 5 | **I2C 传感器**（任意，如 AHT20/MPU6050） | `&i2c0` 已有；加器件节点 | `whoami` 读到正确器件 ID | 1 h |
| 6 | **CAN/TWAI 自测** | `&twai0` 已 okay；loopback 模式 | 自发自收一帧 | 2 h |
| 7 | **SPI 屏 + LVGL Hello World** | 拉 lvgl 模块（§4）+ `CONFIG_LVGL=y` | 屏上出现 "Hello" 文字/按钮 | 0.5–1 d |

> 这 7 项全部落在第③层（overlay + prj.conf + 应用代码），**不需要动 Zephyr 树，也不会被换版本冲掉**。这就是"能立刻做"的定义。

---

## 3. 屏幕：两条路，别混为一谈

### 3.1 外挂 SPI 屏 —— ✅ 可以，走 04 的结构

关键差异（相对 04 的示例，落地时注意）：

| 项 | 要点 |
|---|---|
| 层级 | `st7789v@0` 必须挂在 `mipi_dbi { compatible="zephyr,mipi-dbi-spi"; spi-dev=<&spi2>; dc-gpios=...; }` **里面**，不能直接挂 `&spi2` |
| 必填 | `width/height`（display-controller.yaml）、`pixel-format`（lcd-controller.yaml，`PANEL_PIXEL_FORMAT_RGB_565`）、`x-offset/y-offset/mdac/mipi-mode` |
| pinctrl 宏 | SPI2 前缀是 **`SPIM2_*`**（`SPIM2_MOSI_GPIOxx`/`MISO`/`CLK`/`CSEL`），不是 `SPI2_*` |
| 总线 | S31 板级 `spi2/spi3` 默认 **disabled**，必须 overlay 里 `status="okay"` + `pinctrl-0` |
| Kconfig | `CONFIG_DISPLAY=y`（`menuconfig`）+ `CONFIG_ST7789V=y`（`depends on DT_HAS_SITRONIX_ST7789V_ENABLED`，**不会自动开**） |
| 频率 | 先 4 MHz 跑通，再往 40 MHz 提（花屏就降回来） |

### 3.2 板载 4.3″ RGB 并口屏 —— ❌ 现在不能跑

证据（本机实测）：

- `esp32s31_common.dtsi` 里 grep `lcd|cam` → 只命中 `mcpwm/pcnt`，**没有 `lcd_cam` 节点**；
- `zephyr\drivers\display` 里 ESP32 相关只有 `display_esp32_dsi.c`（P4 的 MIPI-DSI），**没有 8080/RGB 并口屏驱动**；
- 板子那块屏是 16 位并口（DB0~DB15 + PCLK/HS/VS/DE）+ SPI 初始化脚（IO60/IO61），属于 LCD_CAM/8080 通路，不是 SPI 屏。

三条替代（按性价比排序）：

1. **ESP-IDF + `esp_board_manager`**：乐鑫自家 BSP 直接点这块屏（官方 demo 就是它），学习"屏怎么工作"最快；
2. **等上游**：等 `lcd_cam` 节点 + display 驱动补上（周级不可控）；
3. **外挂一块 SPI 屏**：花几十块买 ST7789V/ILI9341 模块，立刻能在 Zephyr 上练 LVGL（推荐学习路径）。

---

## 4. LVGL 调研结果：能跑，但**模块没拉**

### 4.1 三条硬证据

| 事实 | 证据 |
|---|---|
| Zephyr 声明了 LVGL 模块 | `west.yml:324-326`：`name: lvgl`，`revision: c9ffd4692de246eb911c7ba49b78a331b2c63c74`，`path: modules/lib/gui/lvgl` |
| **你的 workspace 没拉** | `Test-Path modules\lib\gui\lvgl` = **False**；`modules\` 下只有 `crypto`、`hal` |
| Zephyr 有现成 LVGL 范例 | `samples/subsys/display/lvgl/`、`samples/modules/lvgl/demos/`、`.../multi_display/`、`.../accelerometer_chart/` |

`sample` 的 `prj.conf` 给出了必备符号（这是最可靠的"怎么配"参考）：

```conf
CONFIG_DISPLAY=y
CONFIG_LVGL=y
CONFIG_LV_Z_MEM_POOL_SIZE=16384     # LVGL 内存池
CONFIG_LV_USE_LABEL=y               # 你要用哪些控件就开哪些
CONFIG_LV_USE_ARC=y
CONFIG_LV_FONT_MONTSERRAT_14=y      # 字体（注意：没有中文）
CONFIG_MAIN_STACK_SIZE=4096
```

### 4.2 模块怎么拿到（GitHub 不通，三条路）

```powershell
# 路 A：gitee 镜像（先测通不通）
git clone https://gitee.com/mirrors/lvgl.git zephyr-workspace\modules\lib\gui\lvgl
# 拉完必须切到 west.yml 钉的 rev，否则 Zephyr 的 glue 对不上
git -C zephyr-workspace\modules\lib\gui\lvgl checkout c9ffd4692de246eb911c7ba49b78a331b2c63c74

# 路 B：离线包（在能上网的机器 clone 后打包，拷回来解压到同一路径）
# 路 C：不用 LVGL，自己写最小绘图（不推荐，失去学习价值）
```

**版本纪律**：LVGL v8 与 v9 的 API 差异很大（`lv_disp_drv_register` vs `lv_display_create`）。你只能跟 `west.yml` 钉的那个 rev，写代码照 `samples/subsys/display/lvgl` 的写法抄，否则会出现"Kconfig 认了、编译报一堆未定义"。

### 4.3 性能预期（别按 PC 的直觉想）

- 240×320、RGB565、全屏一帧 = **153 KB**；
- SPI @ 4 MHz ≈ 300 ms/帧（只能做静态 UI）；
- SPI @ 40 MHz ≈ 30 ms/帧（约 30 fps，够做简单动画）；
- 因此：**必须用 LVGL 的局部刷新（partial buffer）**，别开全屏双缓冲；缓冲尺寸的 Kconfig 名以拉下来的模块 `Kconfig` 为准（拉完 `Select-String ... -Pattern 'VDB|BUF'` 查）。

### 4.4 中文显示（你这块屏大概率要中文）

LVGL 自带 Montserrat 只有 ASCII。要显示中文必须：

1. 准备一个中文 TTF（思源黑体/文泉驿等，注意授权）；
2. 用 `lv_font_conv`（Node 工具）生成 `.c`：只挑用到的字（或用 `--symbols` 指定常用字），否则一个全字库几 MB 放不下；
3. 把生成的 `.c` 加入工程，`CONFIG_LV_FONT_CUSTOM_DECLARE` 声明后 `lv_obj_set_style_text_font`。

这一步是"点屏能跑"和"点屏能用"的分界线，建议放在能显示英文之后再做。

---

## 5. 坑清单（按踩中概率）

| # | 坑 | 症状 | 处理 |
|---|---|---|---|
| 1 | lvgl 模块没拉就开 `CONFIG_LVGL=y` | Kconfig 报 unknown symbol / 编译找不到 `lvgl.h` | 先按 §4.2 拉模块 |
| 2 | 屏没 ready 就初始化 LVGL | 花屏/黑屏/卡死 | 先 `device_is_ready()`，再 `lv_init()` |
| 3 | `st7789v` 挂错层级（直接挂 `&spi2`） | 编译过、运行拿不到父设备 | 按 04 §3 的 `mipi_dbi` 两层结构 |
| 4 | SPI 频率一上来就 40 MHz | 花屏/闪 | 先 4 MHz，再逐步提 |
| 5 | LVGL 内存池太小 | 创建控件时 assert/崩溃 | `CONFIG_LV_Z_MEM_POOL_SIZE` 从 16 KB 起，逐级加 |
| 6 | 以为板载 4.3″ 屏能直接点 | 无节点/无驱动，卡死 | 板载屏走 ESP-IDF 或换 SPI 屏 |
| 7 | 中文没字库 | 显示方块 | §4.4 转字库 |
| 8 | 触摸交互没有 | 只能看不能点 | Korvo-1 无触摸：用四功能键当输入（正好接 `adc-keys`） |

---

## 6. 建议推进顺序（带验收门）

| 阶段 | 内容 | 通过标准 | 预计 |
|---|---|---|---|
| **P1 按键闭环** | §2 的 1/2/3 项（ADC 键 + WS2812 + PWM） | 串口与灯都能按预期响应 | 0.5–1 d |
| **P2 屏点亮** | §2 第 4 项（SPI 纯色） | 三色切换不花屏 | 1 d |
| **P3 LVGL** | 拉模块 + Hello World + 中文字库 | 屏上显示中文并响应按键 | 1–2 d |
| **P4 教程更新** | 把 04/09/06 串成"从按键到点屏"的学习路径，补截图与验收命令 | 文档能从零复现 | 0.5 d |
| **P5（可选）SD 卡** | §9.2 的 ② 档（补 SoC 层） | 能挂载 FAT 读文件 | 1–3 d |
| **P6（等）** | 板载屏 / 音频 / 相机 / WiFi / BLE | 三个信号全绿 | — |

---

## 7. 一句话总结

- **WiFi/BT：等**。不是技术做不到，是射频失败的排查成本远超收益；三条现成替代（S3 练手、ESP-IDF、外挂模组）。
- **"现在就做的全部"：能做**，7 项全是第③层，不碰 Zephyr 树。
- **点屏：外挂 SPI 屏可以，板载 4.3″ 并口屏不行**（缺 `lcd_cam` 与 display 驱动）。
- **LVGL：能跑，但你得先解决一个前置——`modules/lib/gui/lvgl` 没拉取，且 GitHub 不通**，走 gitee 镜像或离线包，并把版本钉到 `west.yml` 那个 rev。
- **中文显示要单独做字库**，这决定"点亮"和"能用"的差距。

---

## 11. 板载 4.3″ 屏的问题到底出在哪几层（逐层拆）

> 一句话：**"等官方补 SoC 层的 lcd_cam 节点"只能解决一半，另一半是"驱动要从零写"**——所以它比 I2S、SDIO 都更远一层。

| 层 | 板载 RGB 屏需要什么 | 现状（本机实测） | 谁来做 |
|---|---|---|---|
| ① SoC 节点 | `lcd_cam` 节点（基址、中断、时钟、`#address-cells`） | ❌ `esp32s31_common.dtsi` 里 grep `lcd\|cam` 只命中 `mcpwm/pcnt` | 上游 |
| ① SoC pinmux | `LCD_DB0..DB15`、`LCD_PCLK`、`HS/VS/DE` 等信号的 pinmux 宏 | ❌ `esp32s31-pinctrl.h` 里 **`LCD` 零命中、`CAM` 零命中**（只有 GPIO/UART/I2C/SPI/RMT/TWAI/LEDC/ADC 等） | 上游（要从乐鑫 pinmux 表生成） |
| ④ binding | 并口屏 / `esp32-lcd-cam` display 的 yaml 契约 | ❌ `dts/bindings/display/` 里 grep `esp32` **零命中** | 自己写 |
| ⑤ 驱动 | LCD_CAM 并口屏驱动（RGB 时序 + DMA 刷帧 + 像素格式） | ❌ 只有 `display_esp32_dsi.c`（P4 的 **DSI**，不能复用） | 自己写（周级） |
| ② 板级 | 屏的引脚、分辨率、时序参数 | ❌ 上游 board dts 没描述（屏本身还是另一块子板） | 你的 overlay |
| ③ 应用层 | `struct device *disp` + `device_is_ready` | — | 你 |

**结论**：`lcd_cam` 节点只是第 1 行。真正卡死的是 **第 2 行（pinmux 宏全缺）** 和 **第 5 行（Zephyr 里没有可抄的并口屏驱动）**。

### 11.1 顺带一个重要对比：摄像头比屏"近一层"

| | SoC 节点 | pinmux 宏 | Zephyr 驱动 | 距离能跑 |
|---|---|---|---|---|
| **DVP 摄像头** | ❌ 缺 | ❌ 缺 | ✅ **有**：`video_esp32_dvp.c`，compatible `espressif,esp32-lcd-cam-dvp`，**无 SoC 限制** | 补 SoC 层就够 |
| **RGB 并口屏** | ❌ 缺 | ❌ 缺 | ❌ **没有**（`display_esp32_dsi.c` 是 DSI，不是并口） | 还差一个驱动 |

也就是说：
- 摄像头 = "**接线级**"工作（补节点 + 宏，驱动现成）；
- RGB 屏 = "**造轮子级**"工作（补节点 + 宏 + **从零写一个 display 驱动**，还要懂 8080/RGB 时序与 DMA）。

### 11.2 所以你只有三个现实选择

| 选择 | 代价 | 适合谁 |
|---|---|---|
| **外挂 SPI 屏做 LVGL** | 几十块屏 + 1 天 | 想学 LVGL / 想做 UI 项目演示 |
| **板载屏走 ESP-IDF**（`esp_board_manager` + `esp_lcd` RGB panel） | 学一套 IDF API；Zephyr 侧只做控制面 | 必须在产品上用这块屏 |
| **自己补 Zephyr 的 LCD_CAM** | 周级 + 需要读乐鑫 TRM、会写 DMA/时序；pinmux 宏还要自己生成 | 想深入 SoC 移植、或准备提 PR |

**不建议**把"点板载 4.3″ 屏"当成学习 Zephyr 的第一步——它同时踩中三个上游空白（节点、pinmux、驱动），学不到"Zephyr 怎么用"，只会学到"Zephyr 怎么缺"。
