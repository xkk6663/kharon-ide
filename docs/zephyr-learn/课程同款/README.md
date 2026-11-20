# 课程同款 · 第三、四课的“本工程改写版”（历史对照线）

> **现状校准（2026-11-20）**：本层是**课程还原线**，保留 Up 主章节顺序与当时的实机证据；
> 仓库当前操作主线已切到 `kharon-ide` + `zephyr-workspace` 双根，**结构性做法以
> [`tutorials/15`](../../tutorials/15-Zephyr工作区双根与板级APP产品.md) 为准**。
>
> 关键差异只有一句：**板级真相不再放在 APP 里**。`applications/**/boards/**` 的整块板级
> 已迁到 `zephyr-workspace/kharon-boards/`，APP 只保留自己的 `boards/<board>.overlay`；
> `CMakePresets.json` 已全部删除，构建参数由产品 yaml + provider + host 解析。
>
> 旧文里的 `applications/` 一律相对 `ZEPHYR_ROOT=...\zephyr-workspace`；产品登记在
> `kharon-manifest/products/kharon/*.yaml`，不再用 `projects/kharon/`。

> 用途：跟着 Misaki 的《Zephyr 入门》第三、四课学，但**每一步都落在你自己的工程上**。
> 他的课按“一块自己画的 STM32H743 板”写；我们的主线是 ESP32-S3，用 Zephyr 主干 SoC 支持
> + 一块自研板 `esp32s3_korvo`。所以这两篇不是翻译，是**同构改写**：章节顺序、知识点讲法
> 按原文，只把“他的板/他的引脚”换成“我们的板/我们的引脚”。
>
> 教程三层：**主线 S3**（本层）／**迁移练习 STM32**（`docs/zephyr-learn/教程/15-Zephyr跨芯片`）／
> **附录 A S31 Korvo-1**（`docs/zephyr-learn/教程/附录A-ESP32-S31-Korvo-1.md`）。

---

## 0. 十篇的顺序（00 → 09，顺着编号读就是一门课）

> **编号规则（2026-11-15 起）**：文件名前缀 = 阅读顺序，连续、不留空号。
> **课程线（第三、四课）= 00 → 05**；**附录 A（S31 / Korvo-1）与元决策 = 06 → 09**。

| # | 文档 | 回答什么 | 属于 | 怎么读 |
|---|---|---|---|---|
| **00** | `00-知识点还原度对照.md` | 他的每个知识点在你这里对应什么、能不能照做 | 课程线·总纲 | 最先读，先判断“跟得上哪部分” |
| **01** | `01-课程对照-第三课与本工程.md` | 他的第三课和你的工程差在哪 | 课程线·第三课（诊断） | 卡住时看“为什么对不上” |
| **02** | `02-项目结构与自定义板级.md` | 板级七个文件、dts 语法、Kconfig 合并、WS2812 点亮 | 课程线·第三课（主线） | **只读原理与 dts 段落**；落点按 15 篇 |
| **03** | `03-上游板级15文件逐个讲.md` | 上游 15 个板级文件分别是谁 | 课程线·第三课（补充） | 想彻底吃透板级层 |
| **04** | `04-给自己底板加外设-层面与步骤.md` | 接屏/传感器算谁的活、七步实操 | 课程线·第三课（延伸） | 要接新器件时 |
| **05** | `05-迁移-新建-缓存-按键.md` | CMake 缓存、Input 子系统按键全链路 | 课程线·第四课（主线） | **只读缓存/按键段落**；新建 APP 按 15 篇 |
| **06** | `06-S31后续需求盘点与屏幕LVGL可行性.md` | WiFi/BT 排期、屏幕两条路 | 附录 A·规划 | 规划下一阶段 |
| **07** | `07-七项外设落地实录.md` | WS2812/PWM/I2C/ADC/CAN 真机证据 | 附录 A·实录 | 想知道七项到哪一步 |
| **08** | `08-教程主线选型-主干S3还是补丁S31.md` | 教程主线为什么选 S3 | 元决策 | 犹豫要不要换板时 |
| **09** | `09-Korvo-1板载外设能跑清单.md` | Korvo-1 V1.1 十四项外设逐项判 | 附录 A·S31 专用 | 上音频/SD/摄像头前 |

---

## 1. 现行结构：一张表替换所有旧目录树

| 旧（课程/旧仓） | 现在 | 说明 |
|---|---|---|
| `applications\esp32s3\blinky-s3\boards\xtensa\esp32s3_korvo\` | `zephyr-workspace\kharon-boards\boards\espressif\esp32s3_korvo\` | 整块板级上移，APP 不再自带 |
| `applications\<family>\<app>\CMakePresets.json` | 无 | 构建参数由 `kharon run --dry-run` 解析 |
| `elab-app-common\` | `zephyr-workspace\elab-app-common\` | 公共层 module |
| `projects\kharon\<app>.yaml` | `zephyr-workspace\kharon-manifest\products\kharon\<app>.yaml` | 产品登记 |
| `west\`（IDE 仓内） | `zephyr-workspace\` | west topdir 独立 |
| `applications\esp32s3\blinky-s3` | `zephyr-workspace\applications\esp32s3\blinky_s3` | 目录名 snake_case，与产品 `name:` 逐字相同 |

**课程里“复制整个 `boards/` 到新工程”的动作，现在不要做。** 新 APP 只复制源码骨架，
板级差异用 `boards/<board>.overlay`；换板只改产品 target。

### 1.1 板级资源（S3 自研板）

| 资源 | 引脚 | dts 写法 |
|---|---|---|
| 用户按键 KEY | **IO45**（按下接地） | `gpio-keys` 下 `gpios = <&gpio0 45 (GPIO_PULL_UP \| GPIO_ACTIVE_LOW)>`，键码 `INPUT_KEY_0` |
| 板载 RGB | **IO48**（WS2812 数据脚） | `worldsemi,ws2812-pulse-io` + `pulse-ios = <&rmt 0>` + `pinctrl = RMT_OUT0_GPIO48` |
| 控制台 | IO43 / IO44（USB-Serial-JTAG） | `&uart0` + `uart0_default` pinctrl |

> 这些事实只写一次，位置是 `kharon-boards/boards/espressif/esp32s3_korvo/`；
> 不要在 `blinky_s3` / `input_btn` 里各写一遍。

---

## 2. 课程知识点在今天怎么落地

### 2.1 第三课：项目结构 + 自定义板级

| 课程动作 | 今天的做法 |
|---|---|
| 认识 `CMakeLists.txt` / `prj.conf` / `src/` / `boards/` | 前三项仍在 APP；`boards/` 只保留 overlay |
| 写 `board.yml` + 主 DTS + pinctrl + defconfig | 全部写进 `kharon-boards/boards/<vendor>/<board>/` |
| 用 `CMakePresets.json` 固定 `BOARD` / `ZEPHYR_BASE` | 用产品 yaml 的 `chip` / `targets[]`；命令看 `kharon run --dry-run` |
| 编译烧录 | `.\kharon.cmd run -p blinky_s3 --steps build,flash,monitor` |

原理段落（dts 语法、`compatible` → 驱动绑定、`led_strip_update_rgb` 调用链）**仍然有效**，
只是文件落点从 APP 换到 `kharon-boards`。逐行注释见 `02` §十–§十二。

### 2.2 第四课：迁移 / 新建 / 缓存 / 按键

| 课程动作 | 今天的做法 |
|---|---|
| 复制旧工程的 `boards/` | 不复制；板级已在 `kharon-boards` |
| 复制 `CMakePresets.json` | 不做；新 APP 从 `applications/_template` 复制 |
| 清 CMake 缓存 | `kharon run -p <app> --steps build`；需要 pristine 时删对应 `work_dir` |
| 按键输入链路 | 代码/dts/驱动分析与今天一致；板级节点仍在 `kharon-boards` |

新建 APP 的现行顺序：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace
Copy-Item -Recurse applications\_template applications\esp32s3\my_app
# 改 app.yaml / CMakeLists.txt 的 project() / src\main.c
# 在 kharon-manifest\products\kharon\ 下登记 my_app.yaml
cd ..\kharon-ide
.\kharon.cmd run -p my_app --steps doctor,build --dry-run
```

完整契约见 [15 · 双根与 Board / APP / Product](../../tutorials/15-Zephyr工作区双根与板级APP产品.md)。

---

## 3. 历史证据怎么读

`02` / `05` 里保留的编译数字、`zephyr.dts` 对比、串口原文，都是**当时布局**的实测，
用于证明“为什么这么改”，不再作为今天的操作步骤：

- `elab-app-common` 的 `.dtsi` 已迁到板级/公共层，旧 include 路径失效；
- 五个工程的 `CMakePresets.json` 已删除，旧 `cmake --preset` 命令不再可用；
- `build\esp32s3\...\s3-debug` 等旧产物目录已清理，现行 work_dir 是
  `zephyr-workspace\build\<chip>\<product>\<target>`；
- `esp32s3_devkitc` 只作上游对照，S3 实机是自定义 `esp32s3_korvo`。

要复现今天的构建，一律从 `kharon run --dry-run` 拿命令，不要从旧文里抄 `west build` 长串。

---

## 4. 建议的走法

1. 先读 [15 · 双根与 Board / APP / Product](../../tutorials/15-Zephyr工作区双根与板级APP产品.md)，建立现行目录与三层事实。
2. 读 `01` 第 1、2 节，知道自己站在课程哪一段。
3. 读 `02` 的 dts/驱动原理段（§十–§十二），**跳过**“把七个板级文件放进 APP”的动作。
4. 读 `05` 的缓存与 Input 子系统段，新建 APP 时用 `_template` + 产品登记，不复制板级。
5. 每节末尾的验收命令改跑 `kharon run`，看到的现象比“编过了”重要。

---

## 5. 与主教程的关系

| 你要做的事 | 去哪 |
|---|---|
| 第一次跑 F411 六步闭环 | [`tutorials/10`](../../tutorials/10-Zephyr模式上手.md) |
| 看懂双根 / 板级 / APP / 产品 | [`tutorials/15`](../../tutorials/15-Zephyr工作区双根与板级APP产品.md) |
| 工作区三形态 | [`tutorials/12`](../../tutorials/12-Zephyr工作区三形态.md) |
| 调试 / 串口 / OTA | [`tutorials/14`](../../tutorials/14-Zephyr调试区操作.md) / [`07`](../../tutorials/07-串口面板使用.md) / [`13`](../../tutorials/13-Zephyr_OTA局域网升级.md) |
| 课程原章节对照 | 本层 `00`–`09`（历史证据 + 原理） |
