# 16 · Zephyr 外设进阶（S3 按键 + 能力地图 + 硬件加密）

> **现状校准（2026-11-19）**：本组是 S3 专有外设的进阶，不直接迁移到 F411；
> 但“先查 dts 节点 → 再查驱动源 → 再看 Kconfig”的三层证据法通用，F411 的板级事实见
> [`tutorials/10`](../../../tutorials/10-Zephyr模式上手.md) 与芯片包
> `packs/official/zephyr/framework/chips/blackpill_f411ce.yaml`。

> 第 15 组解决的是"**同一份代码能不能换芯片跑**"，这一组解决的是"**换过去之后，那块新板子还有什么没被用起来**"。
> 三块内容：① 把 IO45 的物理按键接进 Zephyr 的 input 子系统（不是自己读引脚）；② 把 S3 剩下的外设逐个查清"能不能做、缺什么"；③ 挑最有价值的一个（硬件 AES/SHA）真正做到"算对了"，而不是"跑通了"。

| 页面 | 内容 | 状态 |
|---|---|---|
| **GPIO按键与输入子系统.md** | IO45 → `gpio-keys` → `input_report_key` → input 线程 → 你的回调，含四个真坑、极性实测、三层验证、串口原文 | ✅ 三层全通：设备就绪 + `btnsim` 软件链 + **实机短按切 RGB 确认** |
| **S3能力地图.md** | 三层证据法、S3 全外设能力矩阵、系统层在 S3 的实测、六步开工顺序 | ✅ 勘察 + 逐项实测（AES/SHA 已转 ✅） |
| **硬件加密加速.md** | 会话式 crypto API、`CONFIG_CRYPTO` 的 Kconfig 门、SHA/AES 的已知答案测试、会话复用的 2x 差距 | ✅ 编译 0 warning + 烧录 + 四组 KAT 全 PASS |
| README.md（本页） | 组内路线图、命令速查、产物清单 | — |

---

## 一、本组和前面 15 组的关系

```
13  应用：IO42 四键（ADC 分压梯）      ← 一块板、一根脚、四个键、模拟判定
14  系统：NVS 持久化 / 温度 / RNG / WDT  ← 系统层的四件事
15  跨芯片：同一份 apps 源码 → ESP32-S3  ← 板级差异集中在 overlay
16 ★外设进阶：IO45 物理按键（input 子系统）+ S3 能力地图 + 硬件加密 KAT
```

**为什么放在 15 之后**：input 子系统这套"驱动 → 核心 → 消费者"的分层，只有在你已经亲手写过"裸读 GPIO + 自己延时去抖"之后才有对比价值。第 13 组的 IO42 四键，就是那个"裸"的版本。

---

## 二、两条主线，两种学法

### 主线 A：按键（学"子系统"）

| 你会先想到的写法 | Zephyr 的标准答案 | 差别在哪 |
|---|---|---|
| `gpio_pin_get_dt()` + `k_msleep(20)` 两次采样 | `gpio-keys` 驱动：中断 → `k_work_delayable` 去抖 → `input_report_key` | 去抖在**工作队列**，不占你的线程；事件有统一格式 |
| 自己判断"按下多久" | 回调只收到 `pressed/released` 两个事件，**长短按由应用层自己算时间差** | 驱动不懂业务，业务不该进驱动 |
| 回调里直接干活 | 回调只 `k_msgq_put`，业务在独立线程 | input 线程栈 1024、队列 16，堵一下就丢事件 |

### 主线 B：能力（学"判断方法"）

不要问"Zephyr 支持不支持 X"，要问三层：

```
① dts 里有没有节点（status 是 okay 还是 disabled）
② 树里有没有对应 compatible 的驱动 .c（Kconfig 有没有门控）
③ 板子上有没有那段硬件（或能不能外接）
```

三层齐了才算"能做"。S3 的 Wi-Fi 就卡在第 ③ 层的"二进制 blob"上——节点、驱动、Kconfig 全都在，唯独没有 `libnet80211`。

---

## 三、命令速查（S3 环境，来自第 15 组）

```powershell
$root = "C:\Users\xiao1\Desktop\ELAB\kharon-ide"
$env:PATH = "$root\.venv\Scripts;$root\.tools\cmake\cmake-3.30.2-windows-x86_64\bin;$root\.tools\ninja;C:\Users\xiao1\zephyr-sdk-1.0.1\gnu\xtensa-espressif_esp32s3_zephyr-elf\bin;$env:PATH"
$env:ZEPHYR_BASE = "$root\west\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR = "C:\Users\xiao1\zephyr-sdk-1.0.1"
Remove-Item Env:\CROSS_COMPILE -ErrorAction SilentlyContinue
Remove-Item Env:\TOOLCHAIN_HOME -ErrorAction SilentlyContinue
Set-Location -LiteralPath "$root\west"

# 带按键的应用（build\esp32s3\korvo_apps\s3-debug）
west build -p always -b esp32s3_devkitc/esp32s3/procpu "$root\applications\esp32s31\korvo_apps" -d build\esp32s3\korvo_apps\s3-debug
west flash -d build\esp32s3\korvo_apps\s3-debug --esp-device COM11

# 系统层（build\esp32s3\korvo_sys\s3-debug）
west build -p always -b esp32s3_devkitc/esp32s3/procpu "$root\applications\esp32s31\korvo_sys" -d build\esp32s3\korvo_sys\s3-debug
west flash -d build\esp32s3\korvo_sys\s3-debug --esp-device COM11

# 抓日志 / 发命令（原生 USB 口不要碰 RTS）
& "$root\.venv\Scripts\python.exe" "$root\tools\zephyr\probe_serial.py" COM11 12
& "$root\.venv\Scripts\python.exe" "$root\tools\zephyr\usb_cmd.py" COM11 --reset "app btn@3" "app btnsim 300@4" "app btn@3"
& "$root\.venv\Scripts\python.exe" "$root\tools\zephyr\usb_cmd.py" COM11 --reset "app btnsim 1200@4" "app btn@3" "app stats@3"
```

`app btnsim <ms>` 是本组新增的自测命令：直接往 input 子系统注入一次"按下 → 等 ms → 松开"，**不碰 GPIO**。它验证的是"回调 → 队列 → 线程 → 计数/模式/亮度/NVS"这条软件链；剩下的电气层（中断 + 去抖 + 触点）只能真按键。

构建产物核对：

```powershell
Select-String -Path build\esp32s3\korvo_apps\s3-debug\zephyr\.config -Pattern "CONFIG_INPUT"
Select-String -Path build\esp32s3\korvo_apps\s3-debug\zephyr\zephyr.dts -Pattern "buttons" -Context 0,12
Select-String -Path build\esp32s3\korvo_sys\s3-debug\CMakeCache.txt -Pattern "^DTC_OVERLAY_FILE"
```

最后一条命令是本组最重要的"体检项"：**它会直接告诉你这一次构建到底用了哪个 overlay**（`boards\<board>.overlay` 还是 `app.overlay`）。第 15 组那个"app.overlay 被顶掉"的坑，就是靠它定位的。

---

## 四、本组产出

| 产物 | 说明 |
|---|---|
| `applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.overlay` | **改**：新增 `&button0`（IO45 → `&gpio1 13`）、`&{/buttons}` 的 `debounce-interval-ms=20` |
| `applications\esp32s31\korvo_apps\boards\esp32s3_devkitc_esp32s3_procpu.conf` | **新增**：`CONFIG_INPUT=y`（Korvo 不受影响） |
| `applications\esp32s31\korvo_apps\src\main.c` | **改**：`HAS_KEY` 能力探测、`KEY_DEV_NODE`、`btn_cb`/`btn_q`/`btn_thread`、`cmd_btn`（逻辑+物理电平）、`cmd_btnsim`（注入自测）、`app btn` 横幅 |
| `applications\esp32s31\korvo_sys\boards\esp32s3_devkitc_esp32s3_procpu.overlay` | **新增并修正**：控制台切 USB + 把 `app.overlay` 的 coretemp/wdt0 搬进来（否则 `__device_dts_ord_48`） |
| `applications\esp32s31\korvo_sys\src\main.c` | **改**：新增 `sys sha` / `sys aes`（`hex_dump`、`sha256_once`、`aes_ecb_block` + 两条 KAT 命令） |
| `applications\esp32s31\korvo_sys\prj.conf` | **改**：新增 `CONFIG_CRYPTO=y`（`drivers/crypto/Kconfig` 整个被 `if CRYPTO` 包住） |
| `build\esp32s3\korvo_sys\s3-debug` | **新增构建目录**（145908 B → 加加密后 **147876 B**，1.76%） |
| `logs\serial-s3-btn-1.txt` ~ `-4.txt`、`logs\serial-s3-sys.txt`、`logs\serial-s3-sha.txt`、`logs\s3-build-*.txt` | 本轮串口/构建原文（`-3` 注入自测、`-4` 极性修正后、`sha` 是四组 KAT） |

### 4.1 镜像尺寸的变化轨迹（能看出"每一层功能值多少字节"）

| 版本 | FLASH | 增量 | 加的是什么 |
|---|---|---|---|
| 第 15 组无按键版 | 214708 B | — | — |
| 第 16 组加 input + 按键 | 215476 B | +768 B | `input` 子系统 + `gpio-keys` 驱动 + 回调/线程 |
| 第 16 组加极性与注入自测 | **215716 B** | +240 B | `gpio_pin_get_raw` 读物理电平 + `app btnsim` 命令 |

---

## 五、验证状态（三层都通了）

| 层 | 验到了什么 | 证据 |
|---|---|---|
| 设备就绪 | `buttons` 实例化、`gpio@60004800`、逻辑/物理电平可读 | `logs\serial-s3-btn-4.txt` |
| 软件链 | `app btnsim` 注入 → 短按切模式、长按切亮度、`app stats` 状态同步、亮度跨复位留在 NVS | `logs\serial-s3-btn-3.txt` / `-4.txt` |
| 电气层 | **2026-11-13 实机：短按 IO45 按键即切换 RGB 状态** | 用户实机确认（`off ↔ solid`） |
| 硬件加密 | SHA-256 两条 KAT PASS（33.5 MB/s）；AES-128 ECB 加/解密 KAT PASS（复用会话 5.6 MB/s） | `logs\serial-s3-sha.txt` |

结论：`GPIO_PULL_DOWN`（ACTIVE_HIGH）这个极性判断成立——空闲物理 0、按下 1。若沿用"按键接地"的 `PULL_UP | ACTIVE_LOW`，空闲会被当成按下、按键反而当松开，短按切模式就永远不触发。

> 还没做：S3 能力地图里剩下的 LEDC / PCNT / I2S / CAN / SDHC / USB-OTG / PM / touch / lcd_cam / PSRAM 都是"节点+驱动齐了、没写示例"或"要外接器件"的状态；Wi-Fi/BLE 卡在缺 `libnet80211`/`libbt` blob。

---

## 六、组内自检

1. IO45 为什么落在 `&gpio1 13` 而不是 `&gpio0 45`？
2. `gpio-keys` 的驱动实例是父节点还是子节点？为什么 `DT_HAS_ALIAS(sw0)` 会导致链接错误？
3. 去抖和回调分别跑在什么上下文？为什么这个区别决定了"回调里不能写 NVS"？
4. 判断一个外设能不能做，要查哪三层？S3 的 Wi-Fi 卡在哪一层？
5. S3 相比 Korvo-1，**多出来**的那个音频能力是什么？为什么说它是"新增"而不是"平移"？
6. 硬件加密这一块，`CONFIG_CRYPTO` 不加会怎样？为什么 `CRYPTO_ESP32` 的 `default y` 救不了你？
7. 为什么说 KAT 是"没有仪器也能验收"的唯一办法？它和"跑通不报错"的差别在哪？
8. AES 会话复用比每块开关快约一倍，说明 `begin_session` 里最贵的是什么？这对"逐包加密"有什么启示？
