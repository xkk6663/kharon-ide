# 课程同款 · 第三、四课的"本工程改写版"（主线 S3）

> **现状校准（2026-11-19）**：本层是课程还原线（历史对照），**仓库当前操作主线已切到
> Kharon（F411/F103 + Zephyr 单链）**，操作教程见 [`tutorials/10–14`](../../tutorials/10-Zephyr模式上手.md)。
> 路径已校准为 `applications/<family>/<app>`；旧文里的 `applications/` 只在 `变更记录/` 保留原文。

> 用途：跟着 Misaki 的《Zephyr 入门》第三、四课学，但**每一步都落在你自己的工程上**。
> 他的课是按"一块自己画的 STM32H743 板"写的；**我们的主线是 ESP32-S3**，用的是 Zephyr 主干里就有的 SoC 支持 + 一块我们自己写的板级（`applications\esp32s3\blinky_s3\boards\xtensa\esp32s3_korvo\` 7 个文件）。
> 所以这两篇不是翻译，是**同构改写**：章节顺序、每个知识点的讲法都按他原文，只把"他的板/他的引脚"换成"我们的板/我们的引脚"。
> 教程三层：**主线 S3**（本篇）／**迁移练习 STM32**（同一套六步，见 `docs\zephyr-learn\教程\15-Zephyr跨芯片`）／**附录 A S31 Korvo-1**（`docs\zephyr-learn\教程\附录A-ESP32-S31-Korvo-1.md`）。

---

## 0. 十篇的顺序（00 → 09，顺着编号读就是一门课）

> **编号规则（2026-11-15 起）**：文件名前缀 = **阅读顺序**，连续、不留空号、不再用 `03b` / `03c` 这种字母后缀。
> **课程线（第三、四课）= 00 → 05**；**附录 A（S31 / Korvo-1）与元决策 = 06 → 09**，主线读者读完 05 就够。
> 旧名 → 新名对照：`03-项目结构…`→**02**、`03b-上游板级…`→**03**、`03c-给自己底板加外设…`→**04**、`03d-Korvo-1板载…`→**09**、`04-迁移…`→**05**、`05-S31后续…`→**06**、`06-七项…`→**07**、`07-教程主线选型…`→**08**、`课程对照-…`→**01**。

| # | 文档 | 回答什么 | 属于 | 什么时候读 |
|---|---|---|---|---|
| **00** | `00-知识点还原度对照.md` | **他的每个知识点，在你这里对应什么、能不能照做**（第三课 30 条 + 第四课 15 条逐条打标：✅ 1:1 / 🔁 换名字 / 🔄 换机制 / ❌ 不适用） | 课程线·总纲 | **最先读**：先判断"跟得上哪部分、哪部分只需看懂"，再决定动手 |
| **01** | `01-课程对照-第三课与本工程.md` | 他的第三课和你的工程**差在哪**（逐节对照 + 差异诊断） | 课程线·第三课（诊断） | 已经卡住、想知道"为什么对不上" |
| **02** | `02-项目结构与自定义板级.md` | **跟着做（主线）**：按他第三课原文的 14 节 dts 语法 + 七个板级文件 + CMakePresets 字段，在 S3 上从零写一块自己的板（KEY=IO45 / WS2812=IO48）并点亮板载 RGB。**§十 逐行注释七个文件；§十一 `compatible`→`DT_DRV_COMPAT`→`DEVICE_DT_INST_DEFINE` 全链路；§十二 `led_strip_update_rgb` 的四层代码路径** | **课程线·第三课（主线）** | 想动手走一遍第三课，或想读懂每一行代码 |
| **03** | `03-上游板级15文件逐个讲.md` | **读懂那层你改不了的**：他课里 8 类板级文件在你上游 15 个文件里分别是谁，主 dts 逐段拆，S3 为什么不一样，什么时候才需要自己写整块板级 | 课程线·第三课（补充） | 做完 02 之后、想彻底吃透板级这层时 |
| **04** | `04-给自己底板加外设-层面与步骤.md` | **加自己的外设（屏幕等）算谁的活**：五层职责 + 判定口诀，ST7789V 屏幕 7 步实操（含真实板级模板） | 课程线·第三课（延伸） | 要接屏/传感器/新器件时 |
| **05** | `05-迁移-新建-缓存-按键.md` | **跟着做（第四课）**：项目迁移与新建 → CMake 缓存 → 按键输入（Input 子系统）。**§七 逐行注释 dts / prj.conf / main.c；§八 `input_gpio_keys.c` 的"中断→20 ms 工作项→`input_report_key`"全路径；§九 `INPUT_CALLBACK_DEFINE` 的链接段注册与分发线程** | **课程线·第四课（主线）** | 02–04 走完之后，或想搞清"按键事件到底从哪来" |
| **06** | `06-S31后续需求盘点与屏幕LVGL可行性.md` | **后面怎么排期**：WiFi/BT 为什么等、"现在就做的 7 项"清单与验收、屏幕两条路（外挂 SPI 屏 ✅ / 板载 RGB ❌）、LVGL 模块缺失与中文字库 | 附录 A·规划 | 规划下一阶段、准备点屏时 |
| **07** | `07-七项外设落地实录.md` | **七项真机实录**：WS2812 / PWM / I2C 已在 COM9 跑通（附串口原文），**ADC 四键在 Zephyr 侧不成立**（§9.1 给了完整证据链与已试过的四种修法），CAN 环回卡在 `esp32-twaifd` 驱动完成路径，SPI 屏与 LVGL 缺什么，以及"改 .c 9 秒 / 改 conf 638 秒"的构建真相 | 附录 A·实录 | 想知道七项到哪一步、哪些已通、哪些卡在哪一层 |
| **08** | `08-教程主线选型-主干S3还是补丁S31.md` | **教程该拿哪块板当主线**：主干 Zephyr 对 S3 的支持矩阵（WiFi/BLE/I2S/SD/LCD_CAM+MIPI-DBI/DVP 全有，带证据行号）、S31 与 S3 的差别表、迁移三层（板目标 / overlay / 应用代码零改）、S31 改当"未合并 SoC"附录的教学价值 | 元决策 | 在犹豫教程要不要换成 S3、或准备迁移时 |
| **09** | `09-Korvo-1板载外设能跑清单.md` | **你这块板到底哪些能跑**：Korvo-1 V1.1 十四项外设逐项判（SoC 有没有控制器 / 驱动有没有 / 结论），三块硬骨头（I2S+ES8389、SDIO、RGB 屏+DVP）为什么卡住 | 附录 A·S31 专用 | 想上音频/SD 卡/摄像头/板载屏之前 |

> 另外：`docs\参考\工程结构与可迁移性.md` **不属于这十篇**（它讲"早期为什么没有 `boards\`、换芯片能搬多少"），按需查，不占编号。

---

## 0.5 目录结构对照（他就是这么一课一课学的）

| 他的工程（`blinky` / `input_rcc`） | 我们的工程（`applications\esp32s3\blinky-s3` / `applications\esp32s3\input_btn`） | 说明 |
|---|---|---|
| `CMakeLists.txt`（3 行，没有 `BOARD_ROOT`） | 同（`BOARD_ROOT` 放 presets，第四课做法） | 一个 app 就是一个小工程 |
| `prj.conf` | `prj.conf` | 应用级 Kconfig |
| `boards\arm\st_h743_board\`（7 文件 = 整块板的定义） | **`boards\xtensa\esp32s3_korvo\`（7 文件，我们自己写的）** | 这一层现在终于是"你的板"了，不再是只读的上游文件 |
| 板子差异直接写在板级 `.dts` 里 | 同（`esp32s3_korvo_procpu.dts` + `-pinctrl.dtsi`） | 一份 dts 管一块板 |
| `CMakePresets.json` | `CMakePresets.json`（`zephyr-debug` / `zephyr-release`） | IDE 打开就有树、命令行一条 `--preset` |
| `src\main.c` | `src\main.c` | 第三课点灯（`led_strip` 点亮板载 WS2812@IO48）/ 第四课按键（KEY@IO45）各一份 |

### 板级资源（我们这张板）

| 资源 | 引脚 | dts 写法 |
|---|---|---|
| 用户按键 KEY | **IO45**（按下接地） | `gpio-keys` 容器下 `gpios = <&gpio0 45 (GPIO_PULL_UP \| GPIO_ACTIVE_LOW)>`，键码 `INPUT_KEY_0` |
| 板载 RGB | **IO48**（WS2812 数据脚） | `worldsemi,ws2812-pulse-io` + `pulse-ios = <&rmt 0>` + `pinctrl = RMT_OUT0_GPIO48` |
| 控制台 | IO43 / IO44（USB-Serial-JTAG） | `&uart0` + `uart0_default` pinctrl |

现在一个工程长这样（以 `applications\esp32s3\blinky-s3` 为例）：

```
applications\esp32s3\blinky-s3\
├── boards\xtensa\esp32s3_korvo\
│   ├── board.yml
│   ├── Kconfig.esp32s3_korvo
│   ├── Kconfig.defconfig
│   ├── esp32s3_korvo_procpu_defconfig
│   ├── esp32s3_korvo-pinctrl.dtsi
│   ├── esp32s3_korvo_procpu.dts
│   └── board.cmake
├── CMakeLists.txt
├── CMakePresets.json
├── prj.conf
└── src\main.c
```

和 `input_rcc\` 已经**同构**了——这就是"还原 Up 主课程"能做到的极限：板不一样、引脚不一样，但文件种类、职责、写法一一对应。

---

## 1. 课次对照

| 他的课 | 他讲的内容 | 本文对应 | 本工程里已有的同类内容 |
|---|---|---|---|
| 第二课 | Devicetree / Kconfig 是什么 | 02 §1、§6 | `docs\教程\11-Zephyr驱动\README.md` |
| **第三课** | 项目结构、`boards\` 七文件、dts 语法、编译烧录 | **02 全篇**（S3 自定义板 `esp32s3_korvo` 七个文件全写一遍） | `docs\教程\10-Zephyr应用-阶段0到4.md` 阶段 0–2 |
| **第四课** | 项目迁移/新建、CMake 缓存、Input 子系统按键 | **05 全篇**（`blinky-s3` → `input_btn`，按键 KEY(IO45) 控制板载 RGB(IO48)） | `docs\教程\16-Zephyr外设进阶\GPIO按键与输入子系统.md` |
| 第五课（预告） | 断点调试、时钟配置 | 05 §3（S3 没有 `&rcc`，改成"验证时钟"） | — |

---

## 2. S31 时期的改造记录（历史证据，现属附录 A）

> **读法**：本节记录的是"主线还放在 S31 时"做的工程改造，保留它是因为它是附录 A 的实测证据（为什么 S31 需要 overlay、为什么 `CONFIG_INPUT` 只在 S3 板级开过会导致 S31 链接失败）。**主线读者可以跳过**，直接看 02 / 05。

| 改动 | 位置 | 为什么 |
|---|---|---|
| `store.c`/`store.h` 抽成公共模块 | `applications\common\src\store.c`、`applications\common\include\store.h`；删除 `korvo_apps`、`korvo_nvs` 里各一份 | 原来两份 **SHA256 完全相同**，改一处必漏另一处 |
| 板级公共片段抽成 `.dtsi` | `applications\common\korvo_board.dtsi`（PA_CTRL / WS2812@GPIO37 / adc0）、`applications\common\korvo_i2c.dtsi`（I2C0） | 原来 3–4 份 overlay 重复；现在每个 overlay 只剩"一行 include + 自己独有的节点" |
| 新增"新建工程"骨架 | `applications\_template\`（`CMakeLists.txt` / `prj.conf` / `boards\` / `src\main.c` / `README.md` / `CMakePresets.json`） | 第四课"新建项目"那一节的实物 |
| **overlay 迁入 `boards\`** | 5 个 app：`app.overlay` → `boards\esp32s31_function_coreboard_esp32s31_hpcore.overlay`（include 路径改成 `../../common\...`） | 和课程一样"板相关的东西都在 `boards\`"，且 S31/S3 两份 overlay 可共存、按 `-b` 自动挑 |
| **每个 app 补 `CMakePresets.json`** | `korvo_apps` / `korvo_lab` / `korvo_shell` / `korvo_sys` / `korvo_nvs`（`s31-debug` `s31-release` `s3-debug` `s3-release` 四个 configure preset） | 课程第三课的"IDE 入口"在每个 app 都能用了，不再是只有模板有 |
| **修复：`korvo_apps` 在 S31 上链接失败** | `applications\esp32s31\korvo_apps\prj.conf` 新增 `CONFIG_INPUT=y` | S31 上游板级已有 `gpio_keys/boot_button` → 代码里 `HAS_KEY` 为真、input 回调被编进去；但 `CONFIG_INPUT` 只写在 S3 的板级 `.conf` 里 → 链接期报 `undefined reference to __device_dts_ord_19` / `input_report`。详见 `05` §2.2 |

**验证结果**（原始输出：`logs\route1-refactor-check.txt`）：

| 工程 | 改后配置 | `zephyr.dts`（去注释后比对） | `.config` |
|---|---|---|---|
| `korvo_apps` | ✅ 通过 | **完全一致** | 比改造前多 3 行 `CONFIG_TAINT*`（期间跑过 `west blobs fetch`，与改动无关）+ `CONFIG_INPUT*`（第 4 处改动，属于有意为之） |
| `korvo_lab` | ✅ 通过 | 内容一致，**仅 `rmt_korvo` 与 `i2c0_korvo` 两个同级节点的出现顺序互换**（属性值、pinmux、phandle 全部相同） | 同 `korvo_apps`（不含 `INPUT`） |
| `korvo_shell` | ✅ 通过 | 同 `korvo_lab`（同样只是顺序） | 同上 |
| `korvo_nvs` | ✅ 通过 | **完全一致** | 同上 |

> 顺序差异的原因：overlay 现在先 `#include` 板级片段、后写 I2C 片段，两个兄弟节点进树的先后就变了。**devicetree 里兄弟节点的顺序不影响编译结果**（phandle 按节点身份分配，不按顺序），所以这不算行为变化——但既然发现了，就照实写，不写成"零差异"。

**五个工程完整编译结果（本次实测）**：

| 工程 | 链接 | FLASH |
|---|---|---|
| `korvo_apps` | ✅ | 214528 B（1.28%） |
| `korvo_lab` | ✅ | 148096 B（0.88%） |
| `korvo_shell` | ✅ | 150016 B（0.89%） |
| `korvo_nvs` | ✅ | 146048 B（0.87%） |
| `korvo_sys` | ✅ | 144 KB（0.88%） |
| `_template` | ✅（west 与 presets 两条入口都试了） | 134016 B（0.80%） |

**布局迁移验证**（原始输出：`logs\course-layout-check.txt`）：5 个 app 的 `DTC_OVERLAY_FILE` 都自动指向了 `boards\esp32s31_..._hpcore.overlay`；其中 4 个（`korvo_apps` / `korvo_lab` / `korvo_shell` / `korvo_sys`）的最终 `zephyr.dts` 与迁移前**完全一致**。

**五个工程完整编译结果（本次实测）**：

| 工程 | 链接 | FLASH（west + SDK 工具链） |
|---|---|---|
| `korvo_apps` | ✅ | 214528 B（1.28%） |
| `korvo_lab` | ✅ | 148096 B（0.88%） |
| `korvo_shell` | ✅ | 150016 B（0.89%） |
| `korvo_nvs` | ✅ | 146048 B（0.87%） |
| `korvo_sys` | ✅ | 144 KB（0.88%） |
| `_template` | ✅（west 与 presets 两条入口都试了） | 134016 B（0.80%） |

**presets 入口验证**：`korvo_lab` 用 `cmake --preset s31-debug` + `--build --preset s31-debug-build` 编出 `FLASH 149120 B`（比 west 那版差 1024 B，是 SDK riscv64 与乐鑫 riscv32-esp-elf 两套工具链的差异，不是代码差异）。

**S3 真机验证**（原始输出：`logs\s3-flash-and-key.txt`）：

| 步骤 | 结果 |
|---|---|
| `cmake --preset s3-debug` + build | ✅ `FLASH 215716 B（2.57%）`，overlay 自动挑到 `boards\esp32s3_devkitc_esp32s3_procpu.overlay` |
| `west flash --esp-device COM11` | ✅ `Wrote 215716 bytes at 0x00000000`，`Hash of data verified` |
| 串口注入 `app btnsim` 后读 `app btn` | ✅ `按下=1 次，短按=1 次` → 再注入一次 → `按下=3 次，短按=3 次`，**input 链路真机通** |

**没做的事**（如实记录）：

- **S31（Korvo-1）没有烧录**——你手上插的是 S3，我只烧了 S3；`CONFIG_INPUT=y` 那一行在 S31 上同样生效，但需要你插上 Korvo-1 才能验证 BOOT 键（`west flash -d build\esp32s31\korvo_apps\s31-debug --esp-device COM9`）；
- 没有改任何驱动/业务源码（唯一的代码性改动是 `prj.conf` 加 `CONFIG_INPUT=y`）；
- 原有的 `west\build-*\` 曾归档到 `build\_legacy\`（改造验证 `west-build-check-*`、布局验证 `west-build-course-*`、presets 产物 `apps-<app>-build`）；**2026-11-15 该归档已整目录删除**，这些目录不再存在，需要时按 05 §5 末尾的命令重建。

---

## 3. 建议的走法

1. 先读 `docs\课程同款\01-课程对照-第三课与本工程.md` 第 1、2 节（10 分钟），知道自己站在哪。
2. 打开 **02**，从 §2「工程入口」开始动手——**先让 IDE 能跳转**，再谈结构；§2.5 现在讲的是"每个 app 都有的 presets"。
3. 02 §3 讲板级与 overlay，讲完拿 `applications\common\korvo_board.dtsi` 当模板，把某个工程的 overlay 自己再拆一次（练习在 §7）。
4. 打开 **05**，先做 §1「新建一个工程」（用 `applications\_template` 复制），再做 §2「缓存」和 §3「按键输入」（§3.5 有真机串口证据）。
5. 每节末尾的"验收"命令都要真的跑一遍；**看到的现象**比"编过了"重要。

**两条入口的日常用法**（现在每个 app 都有 presets）：

```powershell
# A. 命令行（west）
west build -b esp32s31_function_coreboard/esp32s31/hpcore applications\esp32s31\korvo_apps -d build\esp32s31\korvo_apps\s31-debug

# B. presets（IDE 或命令行都行）
cd applications\esp32s31\korvo_apps
cmake --preset s31-debug            # 或 s3-debug（板不同，overlay 自动换）
cmake --build --preset s31-debug-build
```

**可以随手删的东西**：验证时产生的构建目录（当时叫 `west-build-check-*` / `-course-*` / `apps-<app>-build`，现已随 `build\_legacy\` 一起删除）。要重新验证就按 05 §5 末尾的命令，产物会落到 `build\<芯片>\<工程>\<配置>`。

