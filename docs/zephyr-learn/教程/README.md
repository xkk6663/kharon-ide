# Zephyr 开发上手教程 · 目录

> 场景：**学习线主线仍是 ESP32-S3（Zephyr 主干直接支持，零补丁）**，目标是让读者拿一块 S3 小板一步步复现；**ESP32-S31-Korvo-1 当附录 A**（主干没有这个 SoC，靠未合并 PR #119851），第 15 组用它练“同一份代码换芯片”。
> **仓库现状（2026-11-19）**：当前仓库的**操作主线已切到 Kharon（STM32F411/F103 + Zephyr 单链）**——先看 [`docs/tutorials/10–14`](../../tutorials/10-Zephyr模式上手.md)；本套 S3/S31 笔记继续作为学习线与历史实测证据保留。
> 全部章节基于**本机实测**（2026-11-13，Win11 + pwsh 7 + Zephyr 4.5.99 + Zephyr SDK 1.0.1：`xtensa-espressif_esp32s3_zephyr-elf` 与 `riscv64-zephyr-elf` 两个目标）；**2026-11-16 补装第三个目标 `arm-zephyr-eabi`，第 15 组第二篇的 STM32F411 已实机跑通**。
> 标的 ✅ 的章节有真实烧录 + 串口输出作为证据；标的 📖 的只有概念和代码，无法在本板验证（原因写在正文）。
> **三层结构**：学习主线 **ESP32-S3**（00–17 章）/ 跨芯片迁移 **第 15 组**（三篇：S31↔S3 同 SDK 换架构；ESP32 → STM32F411 换架构 + 换厂商 HAL + 换烧录探针；STM32F411 → STM32F103 同厂商换系列）/ 附录 A **ESP32-S31 Korvo-1**（未合并 SoC）。加新芯片放哪一层，见 `00-简介.md` 的“以后的芯片往哪一层放”。
>
> **路径口径**：可执行路径已统一为 `applications/<family>/<app>/`；west 工作区在仓库根 `west/`，`ZEPHYR_BASE=west/zephyr`。旧文里的 `apps/`、`zephyr_k` 只在 `变更记录/` 里按原文保留。学习地图见 [`../README.md`](../README.md)。

---

## 目录结构

```
Zephyr 开发
├── 00 简介 ............................... 00-简介.md           ✅
├── 01 当前使用的版本 ..................... 01-当前使用的版本.md    📖
├── 02 配置开发环境 ....................... 02-配置开发环境.md      ✅
├── 03 获取 Zephyr 代码 ................... 03-获取Zephyr代码.md     ✅
├── 04 编译与运行 ......................... 04-编译与运行.md        ✅
├── 05 UF2 拖放烧录 ....................... 05-烧录-UF2与esptool.md  ✅
├── 06 RISC-V 向量扩展（S31 视角）......... 06-RISC-V向量扩展.md     📖
├── 10 Zephyr 应用（阶段 0–4）............. 10-Zephyr应用-阶段0到4.md
│   ├── 阶段 0 · 拆一个 sample（blinky）
│   ├── 阶段 1 · hello_world
│   ├── 阶段 2 · shell 工程
│   ├── 阶段 3 · 用 shell 摸引脚
│   └── 阶段 4 · 线程 / 队列 / 定时器 / 日志
├── 11 Zephyr 驱动 ........................ 11-Zephyr驱动\
│   ├── 驱动模型入门（先读这页）............ 00-驱动模型入门.md  📖
│   ├── 总览 + korvo_lab 实验台 ........... README.md        ✅
│   ├── GPIO（12 个接口逐个实测）.......... GPIO.md          ✅
│   ├── I2C（扫到 0x10 / 0x14）............ I2C.md           ✅
│   ├── ADC（17 位、8 通道）............... ADC.md           ✅
│   ├── WS2812（RMT + pulse_io）........... WS2812.md        ✅
│   └── 没做到实机演示的部分 .............. 未实测的部分.md   📖
├── 12 Zephyr 组件 ........................ 12-Zephyr组件.md
│   ├── shell
│   ├── logging
│   ├── settings
│   └── 其它（watchdog / zbus / ring_buffer）
├── 13 Zephyr 应用层（能跑的功能）.......... 13-Zephyr应用\
│   ├── 总览 + 硬件资源清单 + 可跑性矩阵 ... README.md        ✅
│   ├── RGB（25 fps 动画引擎）............. RGB.md           ✅
│   └── 功能按键与功放（IO42 四键）......... 功能按键与功放.md  ⚠️ Zephyr 侧未跑通，见 06 §9.1
├── 14 Zephyr 系统层与进阶路线 ............. 14-Zephyr系统与进阶\
│   ├── 路线图（还剩哪些能开发）........... README.md        ✅
│   ├── 系统层（复位原因/温度/随机数/看门狗）. 系统层.md       ✅
│   └── NVS 持久化（写读删跨复位）.......... NVS持久化.md     ✅
├── 15 Zephyr 跨芯片（迁移练习）............ 15-Zephyr跨芯片\
│   ├── 组路线图 + 三板对照 + 命令速查 ..... README.md        ✅
│   ├── 换芯片实战（S31 RISC-V → S3 Xtensa）.. 换芯片实战.md   ✅
│   ├── 换芯片实战二（ESP32 → STM32F411）... 换芯片实战-STM32F411.md ✅
│   │            ↳ SDK 第三个目标 arm-zephyr-eabi、hal_stm32/cmsis_6 离线复原、
│   │              DAP-Link 覆盖板级 ST-Link cfg、PC13 无定时器通道 → 软件 PWM
│   └── 换芯片实战三（STM32F411 → STM32F103）.. 换芯片实战-STM32F103.md ✅
│                ↳ 同一支 DAP-Link 再换一代：补 stm32f1xx HAL / core_cm3.h / F1 pinctrl、
│                  两段式板目标、LL shim 双系列分支、64 KB flash 的真实容量
├── 16 Zephyr 外设进阶（S3 按键 + 能力地图 + 硬件加密）. 16-Zephyr外设进阶\
│   ├── 组路线图 + 命令速查 ................ README.md        ✅
│   ├── GPIO 按键与 input 子系统（IO45）..... GPIO按键与输入子系统.md ✅
│   ├── S3 能力地图与开工顺序 ............... S3能力地图.md    ✅
│   └── 硬件加密加速（KAT 自证）............ 硬件加密加速.md   ✅
└── 17 Zephyr 无线能力（S3 Wi-Fi / BLE）..... 17-Zephyr无线能力\
    ├── 组总览：blob 补齐 + wifi scan + BLE 广播 + 配网门户 ..... README.md ✅
    ├── 17.1 配网门户与状态监控 · 17.2 远程 Shell · 17.3 SNTP 对时与出网自检
    ├── 17.4 配网页鉴权与日志 · 17.5 OTA 升级链路 · 17.6 OTA 断点续传
    ├── 17.7 Flash 布局与 mcuboot 换槽 · 17.8 故障排查实录
    └── 17.9~17.11 回滚方案评估 / 页面受众分离 / 无 USB 远程运维

（附录，不占主线编号）
└── 附录 A ESP32-S31 / Korvo-1 ............. 附录A-ESP32-S31-Korvo-1.md 📖
    未合并 SoC 支持的真实样本：SoC 层要补哪几层、编号写错一基会怎样、驱动里的 SoC gate
```

---

## 对照参考：同一套结构的另一块板

这套目录结构参照了 YuzukiNeko 的 Zephyr 文档（<https://yuzukihd.github.io/YuzukiNeko/zh/dev/zephyr/>）。它的板子是 Allwinner F101（玄铁 C907，`rv32imacfdv`），**有 V 扩展**，所以它的 UF2 和 RVV 两章能真跑；Korvo-1 的 S31 两样都没有。

| 章 | 参考教程 | 本教程（Korvo-1 / S31） |
|---|---|---|
| 配置开发环境 | 玄铁 900 GCC + `env.sh`，cross-compile，不用 SDK | 官方 Zephyr SDK 1.0.1（`riscv64-zephyr-elf` 目标）+ `kharon env` 生成的 `scripts\kharon-env.ps1`（旧 `tools\zephyr\env.ps1` 可 fallback；**现在是 SDK 路线**；早期用 ESP-IDF `riscv32-esp-elf` 的 cross-compile 过程保留在 02 章作历史记录） |
| 获取代码 | `west init/update` + fork | Gitee 镜像 + GitHub PR 增量（国内网络下的做法） |
| 编译与运行 | RAM 镜像 / XIP / FEL | 仅 flash 镜像，`west build` + esptool |
| UF2 拖放 | 自装 UF2 下载器 + `mkuf2.py` + 分区布局 | ❌ 没有常驻下载器，只能 esptool（第 05 篇讲清原理和代价） |
| RVV | `CONFIG_SUN252I_F101_RVV` → select `RISCV_ISA_EXT_V` | ❌ 上游无此开关、S31 无 V，只有 PIE（第 06 篇） |

**重要提醒**：参考教程里 `CONFIG_SUN252I_F101_*` 这类前缀都是**那个 fork 的 SoC 私有代码**，不是 Zephyr 通用能力。照抄配置项之前先确认它在你手上的树里存在。

## 建议的阅读顺序

1. **00 → 01**：先搞清"这本书用哪块板当主线、手上这套是什么版本"，别急着装环境。主线是 S3，所以 02 章起所有命令默认 `-b esp32s3_devkitc/esp32s3/procpu`。
2. **02 → 03 → 04**：装环境、拿代码、编出第一个镜像。这三章是连续的，中间任何一步失败都会卡住后面。
3. **05**：烧进去，看到 `*** Booting Zephyr OS ***`。**到这里你才算真的开始了。**
4. **10 的阶段 0–4**：这是本教程的主干，四个阶段对应四种能力（读 sample / 跑最小程序 / 配工程 / 摸硬件 / 写多线程）。
5. **11 → 12**：按需查。先读 `11-Zephyr驱动\00-驱动模型入门.md`（设备树→绑定→device→句柄四概念，不接线也能看懂），想点亮 WS2812 看 `11-Zephyr驱动\WS2812.md`；想加日志看 12。11 这一组建议按 GPIO → I2C → ADC → WS2812 的顺序做，每个页面都对应 `applications\esp32s31\korvo_lab` 的一条命令。
6. **13**：驱动都通了之后，来这里做「能跑的功能」。先看 README 的可跑性矩阵（哪些资源 Zephyr 侧真能用），再做 RGB，最后做四键 + 功放。这一组才算「板子真的在做事」。
7. **14**：这一组两条线并行读——`README.md` 是「还剩什么能做」的路线图（含 dts 节点清单和三档优先级），`系统层.md` 是接着练手的下一个工程 `korvo_sys`（不接线就能跑）。
8. **06**：随时可以读，它不依赖前面任何一步。
9. **15**：等你在 13/14 里已经有一份"确实能跑的代码"之后再读。它教的是"把板子差异从代码里摘出去"，顺序反了会分不清是板子的问题还是自己没学会。**再加一块板时，直接翻这一组的六步清单和坑表**（组内已有三篇：同架构换芯片、换厂商、同厂商换系列）。
10. **附录 A**：随时可读，不依赖前面任何一步。它讲的是"一块还没进主干的芯片（S31/Korvo-1）是怎么被补进 Zephyr 的"，以及我们在这条路上踩到的真实坑（编号写错一基、dtsi 缺节点、驱动 gate）。

---

## 本机已经验证过的事实（可复现的基准）

> **读这一节先记住一件事**：下面是**附录 A（S31/Korvo-1）**的实测记录，留着是因为它是第一手证据；**主线（S3）的实测基准在后面的"第二块板"一节**。两套记录的板目标、串口、镜像偏移都不同，别把 COM9 的日志当成 S3 的。

| 项 | 实测值 |
|---|---|
| Zephyr 版本 | `4.5.99`，commit `34e025569cbb` |
| 板目标 | `esp32s31_function_coreboard/esp32s31/hpcore` |
| 工具链 | `riscv64-zephyr-elf-gcc` 14.3.0（Zephyr SDK 1.0.1，`VARIANT=zephyr`）；早期章节用的是 `riscv32-esp-elf-gcc` 15.2.0（cross-compile） |
| hello_world 镜像 | 132.38 KB，烧录偏移 `0x2000` |
| shell 镜像 | 235.50 KB（多出的 ~100 KB 是 shell + logging + 各外设驱动） |
| 烧录 | `west flash --esp-device COM9`，MAC `30:ed:a0:f3:fc:50` |
| 串口 | COM9 / 115200 8N1（CP210x） |

**hello_world 的串口输出（原始）**：

```
*** Booting Zephyr OS build 34e025569cbb ***
Hello World! esp32s31_function_coreboard/esp32s31/hpcore
```

**shell 工程的实测命令**（`applications/esp32s31/korvo_shell`）：

```
uart:~$ korvo pins            → 打印 Korvo-1 官方管脚表
uart:~$ device list           → i2c0 / adc0 / gpio0 / gpio1 / uart0 全 READY
uart:~$ i2c scan i2c0         → 扫到 0x10、0x14 两颗器件
uart:~$ adc adc@20815000 resolution 17
uart:~$ adc adc@20815000 read 1   → read: 2247
uart:~$ korvo pa              → PA_CTRL(GPIO7) 拉高
uart:~$ kernel thread list    → shell_uart 4096 B 用了 29%
```

**驱动实验台 `applications/esp32s31/korvo_lab` 的实测命令**（烧一次四个外设全测，详见 `11-Zephyr驱动\`）：

```
uart:~$ lab gpio    → 12 个 GPIO 接口逐个过，含 3 处平台偏差
uart:~$ lab key     → 直接读 IN/IN1/ENABLE1 原始寄存器
uart:~$ lab i2c     → configure/get_config/scan/write_read/transfer，2 颗器件
uart:~$ lab adc     → 8 个通道，17 位，ch1=2246
uart:~$ lab led     → WS2812 红绿蓝白灭
```

镜像是 147.58 KB（`build\esp32s31\korvo_lab\s31-debug`），烧录写入 244096 字节。

**应用层工程 `applications/esp32s31/korvo_apps` 的实测命令**（RGB 动画 + 四键识别 + 功放 + **NVS 记忆**，详见 `13-Zephyr应用\` 与 `14-Zephyr系统与进阶\NVS持久化.md` 第 8 节）：

```
[boot] 恢复配置：模式=solid 基色=14 00 00 亮度=15% PA=0 第 17 次开机
uart:~$ app stats          → 模式=solid 亮度=15% 帧数=74 单帧耗时=2 ms 最后颜色=03 00 00
uart:~$ app rgb solid 20 0 0   → 基色 = 14 00 00（已落盘）
uart:~$ app bright 15      → 下一帧生效，最后颜色 03 00 00（已落盘）
uart:~$ app rgb breath 0 0 60  → 三角波 2.4 s 一周期
uart:~$ app rgb rainbow    → 3.6 s 转一圈色相，最后颜色 00 3c 28
uart:~$ app rgb status     → 心跳灯：亮200/灭200/亮200/灭1400 ms
uart:~$ app pa on          → PA_CTRL(GPIO7) = 1，同时亮常绿提示（已落盘）
uart:~$ app adc 16         → 双单元 16 通道全扫，看清哪个通道在按键时跳
uart:~$ app key 100        → raw=284 -> SET / 830 -> PLAY / 1359 -> VOL- / 1810 -> VOL+
uart:~$ app saved          → 列出 NVS 里的模式/基色/亮度/功放/开机次数
uart:~$ app clear          → 清空 4 个键，下次上电回默认
```

镜像是 236.13 KB（`build\esp32s31\korvo_apps\s31-debug`），FLASH 213504 B（1.27%，加 NVS 后从 145664 B 涨上来）。

**系统层工程 `applications/esp32s31/korvo_sys` 的实测命令**（复位原因 / 温度 / 随机数 / 看门狗就绪，详见 `14-Zephyr系统与进阶\系统层.md`）：

```
uart:~$ sys info     → 复位原因位图=0x00000008 → POWER_ON，器件 ID=30:ed:a0:f3:fc:50，版本 4.5.99
uart:~$ sys temp 5   → 22.00 °C ×5（附 W: No calibration param in eFuse）
uart:~$ sys rand 3   → 0x30c65add 0x940cd77d / 0x8020ab2b 0xfdc2ff28 / 0xabffdd7f 0xb2c16760
uart:~$ sys dev      → coretemp ready=1，watchdog0 ready=1
```

镜像是 142 KB（`build\esp32s31\korvo_sys\s31-debug`），FLASH 145280 B（0.87%）。看门狗只读就绪状态，**不安装超时**（安装后停止喂狗会复位板子，属本机禁做操作）。

**持久化工程 `applications/esp32s31/korvo_nvs` 的实测命令**（NVS 写读删跨复位，详见 `14-Zephyr系统与进阶\NVS持久化.md`）：

```
uart:~$ nvs info     → storage 分区 offset=0x00fb0000，扇区 4096 B×3 = 12288 B，可用约 8192 B
uart:~$ nvs set 10 123   → id=10 ← u32 123 已写入（4 B）
uart:~$ nvs get 10       → id=10 → u32 123 (0x0000007b)
uart:~$ nvs tag korvo-1  → 字符串 8 B
uart:~$ nvs rgb 20 30 40 → blob 3 B：14 1e 28
uart:~$ nvs bright 60    → u32 4 B
uart:~$ nvs dump         → 1 boot_cnt=4 / 2 tag="korvo-1" / 3 rgb=14 1e 28 / 4 bright=60%
uart:~$ nvs del 10       → 删除后跨复位仍报"不存在"
```

三轮复位连测：开机计数 2 → 3 → 4，`id=10` 跨复位保留，删除也跨复位生效。镜像是 146048 B（`build\esp32s31\korvo_nvs\s31-debug`）。

---

## 第二块板：ESP32-S3（第 15 组接入，第 16 组继续用，已实测）

从第 15 组开始多了一块板，**同一份 `applications\esp32s31\korvo_apps` 源码编出两份固件**：

| 项 | ESP32-S3 实测值 |
|---|---|
| 板目标 | `esp32s3_devkitc/esp32s3/procpu`（Xtensa，单核 `procpu`） |
| 工具链 | **Zephyr SDK 1.0.1**：`xtensa-espressif_esp32s3_zephyr-elf`（**不是** ESP-IDF 的 `xtensa-esp-elf`） |
| 环境 | `ZEPHYR_TOOLCHAIN_VARIANT=zephyr` + `ZEPHYR_SDK_INSTALL_DIR=C:\Users\xiao1\zephyr-sdk-1.0.1` |
| chip revision | `v0.2`；dts 写 8 MB flash，芯片实际 16 MB（有警告，不影响） |
| `hello_world` 镜像 | 135044 B，烧录偏移 **`0x0000`** |
| `korvo_apps` 镜像 | 214708 B（FLASH 2.56%）；第 16 组加按键后 **215716 B（2.57%）** |
| `korvo_sys` 镜像 | 145908 B（1.74%，第 16 组）→ 加 `sys sha`/`sys aes` 后 **147876 B（1.76%）** |
| 串口 | **`COM11`**，芯片内置 USB-Serial-JTAG（VID_303A&PID_1001），**不是 CP2102** |
| 控制台 | 必须 overlay 切 `usb_serial`（板级 dts 默认 `uart0`，只插 USB 口会一片空白） |
| ADC | **12 位**（S31 是 17 位，应用里要运行时探测） |
| `storage` 分区 | `0x3b0000` / 192 KB（`partitions_0x0_amp_4M.dtsi`），`store.c` 不用改 |
| 板载资源（用户实测告知） | **IO45 = 按键 KEY**（按下接地，→ `&gpio1 13`）、**IO48 = 板载 RGB**（WS2812 走 RMT） |
| 没有的东西 | `PA_CTRL`（`app pa` 返回 `-ENOTSUP`）、IO42 四键分压梯；~~Wi-Fi/BLE 缺 blob 编不出来~~ → **第 17 组已补齐 blob 并实测**（`wifi scan` 11 个 AP、BLE 广播被 PC 扫到、`applications\esp32s3\s3_wifi_portal` 的 APSTA 配网门户连真路由器拿到 `10.168.1.226`） |

**hello_world 串口原文**：

```
*** Booting Zephyr OS build 34e025569cbb ***
Hello World! esp32s3_devkitc/esp32s3/procpu
```

**同一份 `korvo_apps` 在 S3 上的串口原文**：

```
[boot] 恢复配置：模式=solid 基色=14 00 00 亮度=15% 第 3 次开机（本板无 PA_CTRL）

=== Korvo apps（同一份代码，esp32s3_devkitc/esp32s3/procpu）===

uart:~$ app stats      → 模式=solid 亮度=15% 帧数=47 单帧耗时=1 ms 最后颜色=03 00 00
uart:~$ app saved      → 模式 solid(1) / 基色 14 00 00 / 亮度 15% / 功放 off / 开机次数 3
uart:~$ app pa on      → [pa] 本板 dts 里没有 pa_ctrl 别名（S3 最小板没有功放使能脚），此命令不生效
uart:~$ app adc 2      → [adc] ADC 分辨率=12 位，adc0/adc1 各 8 通道全部读出数值
```

三个关键证据：`第 3 次开机`（NVS 在 S3 上也落盘）、`（本板无 PA_CTRL）`（能力探测生效）、横幅里的板名（同一份源码）。

**第 16 组：接上 IO45 按键 + 系统层在 S3 上的原文**：

```
[boot] 恢复配置：模式=off 基色=14 00 00 亮度=15% 第 9 次开机（本板无 PA_CTRL）
=== Korvo apps（同一份代码，esp32s3_devkitc/esp32s3/procpu）===
app btn（按键：短按切模式 / 长按切亮度）

uart:~$ app btn    → [btn] 设备=buttons 脚=gpio@60004800
                     [btn] 逻辑电平=0 物理电平=0（本板空闲实测=物理 0，0=松开 / 1=按下）
                     [btn] 按下=0 次，短按=0 次（切模式），长按=0 次（切亮度 15/30/60/100）
uart:~$ app btnsim 300  → 注入 300 ms → [btn] 短按 1 次，模式 off→solid
uart:~$ app btnsim 1200 → 注入 1200 ms → [btn] 长按 1 次，亮度 15%→30%（存进 NVS）

（korvo_sys，S3）
uart:~$ sys temp 3 → [temp] 31.29 °C（×3）
uart:~$ sys rand 4 → 4 组 32 位随机数（trng0）
uart:~$ sys dev    → coretemp ready=1 / watchdog0 ready=1
uart:~$ sys sha 4  → [sha] KAT("abc") PASS / KAT("") PASS / 吞吐 33.5 MB/s
uart:~$ sys aes 2000 → [aes] KAT(encrypt) PASS / KAT(decrypt) PASS / 复用会话 5.6 MB/s
```

**验证状态**：按键三层全通——设备就绪（`buttons`）、软件链（`app btnsim` 注入后短按切模式、长按切亮度、NVS 跨复位保留）、**电气层（2026-11-13 实机短按切换 RGB 状态确认）**。硬件加密四条 KAT 全 PASS。补齐命令与"二分法排查表"见 `16-Zephyr外设进阶\GPIO按键与输入子系统.md` 第六节，加密细节见 `硬件加密加速.md`。

> 关键发现：按"按键接地"的惯性开 `GPIO_PULL_UP | GPIO_ACTIVE_LOW`，实测空闲物理电平仍是 0——IO45 是 S3 的 strapping 脚，板上外部下拉压过了内部上拉。改成 `GPIO_PULL_DOWN`（ACTIVE_HIGH）后逻辑/物理电平都为 0，与"松开"一致。

> 如实说明：这一轮回烧 Korvo-1 时 `COM9` 已从设备管理器消失（板子被拔下换成 S3），所以 Korvo 侧只做了**编译回归**（`BUILD_EXIT=0`、213504 B），没有再实机跑。Korvo 的实机数据仍是上一轮的 `logs\serial-apps-nvs-A~E.txt`。


---

## 环境速查（当前仓库）

当前仓库的默认入口是 `kharon env`；旧 `tools\zephyr\env.ps1` 仍可作 fallback，环境事实由 host yaml + provider 生成。每开一个新终端：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide
.\kharon.cmd env --out scripts\kharon-env.ps1   # 只在 host/provider 变更后重跑
. .\scripts\kharon-env.ps1
```

脚本会设置 `ZEPHYR_BASE=west/zephyr`、`ZEPHYR_SDK_INSTALL_DIR`，并把
`.deps/venv`、CMake、Ninja、ccache、`arm-zephyr-eabi` 前置到 PATH。
新机器先 `Copy-Item config\host.local.yaml.example host.local.yaml`，填本机 SDK / 工具链路径。

如果手工拼 PATH，记住当前目录名：

| 旧名（历史） | 当前路径 |
|---|---|
| `.venv` | `.deps/venv` |
| `.tools/cmake/...` | `.deps/tools/cmake/cmake-3.30.2-windows-x86_64/bin` |
| `.tools/ninja` | `.deps/tools/ninja` |
| `zephyr_k` | `west` |

**两个关键点**：`ZEPHYR_SDK_INSTALL_DIR` 指向 SDK 根目录（不是 `gnu/`），CMake 按板子
`arch` 自动选目标；`CROSS_COMPILE` / `TOOLCHAIN_HOME` **必须清空**，否则会退回
`cross-compile` 拿错编译器。

> 历史状态：第 02–04 组教程写的 `VARIANT=cross-compile` + ESP-IDF `riscv32-esp-elf` 是当时本机没装 SDK 的权宜做法，**仍然能编**（旧构建目录 `build\esp32s31\korvo_sys\s31-debug` 就是它编的），但已经不是现在的默认路线。统一过程与实测见第 15 组 3 节。

常用命令：

```powershell
# 编译
west build -p -b esp32s31_function_coreboard/esp32s31/hpcore <应用绝对路径>
# 编译到指定目录
west build -b esp32s31_function_coreboard/esp32s31/hpcore <应用绝对路径> -d <构建目录>
# 烧录
west flash --esp-device COM9
# 看串口（自带复位）
python tools\read_serial.py
# 发命令
python tools\serial_cmd.py COM9 "device list"

# ---- S3（第 15 / 16 组）----
west build -p always -b esp32s3_devkitc/esp32s3/procpu <应用绝对路径> -d <构建目录>
west flash -d <构建目录> --esp-device COM11          # 镜像偏移 0x0000
python tools\probe_serial.py COM11 8 --reset         # 被动抓，不碰 DTR/RTS
python tools\usb_cmd.py COM11 "app stats@3"          # 发命令，不主动复位
```

---

## 工程与工具位置

| 路径 | 内容 |
|---|---|
| `applications\esp32s31\korvo_shell\` | 阶段 2 的 shell 工程（`prj.conf` + `app.overlay` + `src\main.c`） |
| `applications\esp32s31\korvo_lab\` | 驱动实验台（`lab gpio / key / i2c / adc / led`） |
| `applications\esp32s31\korvo_apps\` | 应用层（RGB 动画 + 四键 + 功放，`app ...` 命令集） |
| `applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.overlay` | 第 15 组新增、第 16 组扩充：S3 板级差异（删 `pa-ctrl`、WS2812@GPIO48、控制台切 `usb_serial`、**IO45 按键 + 20 ms 去抖**） |
| `applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.conf` | 第 16 组新增：`CONFIG_INPUT=y`（只对 S3 生效） |
| `applications\esp32s31\korvo_sys\` | 系统层（`sys info / temp / rand / dev`） |
| `applications\esp32s31\korvo_sys\boards\esp32s3_devkitc_esp32s3_procpu.overlay` | 第 16 组新增：S3 控制台切 USB + coretemp/wdt0 打开（**必须搬，否则 app.overlay 不生效**） |
| `applications\esp32s31\korvo_nvs\` | 持久化实验台（`nvs info / dump / set / get / del / tag / rgb / bright`） |
| `west\zephyr\` | Zephyr 源码（`pr-s31` 分支，Gitee 主干 + PR 增量） |
| `west\modules\hal\espressif\` | HAL（`hal-657` 分支，含 `esp32s31` 目录） |
| `C:\Users\xiao1\zephyr-sdk-1.0.1\` | 第 15 组安装：官方 Zephyr SDK 1.0.1（`gnu\xtensa-espressif_esp32s3_zephyr-elf`，CMake 包已注册） |
| `build\samples\esp32s31\hello_world\default\` | hello_world 构建目录 |
| `build\esp32s31\korvo_shell\s31-debug\` | shell 工程构建目录 |
| `build\esp32s31\korvo_lab\s31-debug\` | 驱动实验台构建目录 |
| `build\esp32s31\korvo_apps\s31-debug\` | 应用层构建目录（Korvo-1 / S31） |
| `build\esp32s31\korvo_sys\s31-debug\` | 系统层构建目录 |
| `build\esp32s31\korvo_nvs\s31-debug\` | 持久化构建目录 |
| `build\samples\esp32s3\hello_world\default\` | 第 15 组：S3 的 hello_world（135044 B，偏移 0x0） |
| `build\esp32s3\korvo_apps\s3-debug\` | 第 15 组：S3 的同源码 `korvo_apps`（无按键版 214708 B → 第 16 组带按键 215716 B） |
| `build\esp32s3\korvo_sys\s3-debug\` | 第 16 组：S3 的 `korvo_sys`（145908 B → 加加密命令 **147876 B**，1.76%） |
| `tools\read_serial.py` | 抓 8 秒串口输出（自动 RTS 复位，**硬编码 COM9**） |
| `tools\serial_cmd.py` | 往串口发一串 shell 命令并回收显（**仅 CP2102 那一路可靠**） |
| `tools\probe_serial.py` | 第 15 组新增：被动抓串口（`PORT SECONDS [--reset]`），原生 USB 口适用 |
| `tools\usb_cmd.py` | 第 15 组新增：不主动复位的命令工具（`PORT [--reset] "cmd@secs"`） |
| `docs\` | 平台现状、管脚表、IDF→Zephyr 概念对照 |
