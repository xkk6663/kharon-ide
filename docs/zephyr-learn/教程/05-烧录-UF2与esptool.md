# 05 · 烧录：UF2 拖放 vs esptool

> **主线约定**：本书主线是 **ESP32-S3**。S3 的烧录和 S31 有三处不同：串口是 **COM11**（原生 USB-Serial-JTAG，不是 CP2102 的 COM9）、镜像偏移是 **`0x0000`**（S31 是 `0x2000`）、命令要带 `-d <构建目录>`：
>
> ```powershell
> west flash -d <S3 构建目录> --esp-device COM11
> ```
>
> 本章讲的是"为什么 UF2 这条路不成立"，两块板都成立（ESP32 系出厂只有乐鑫自己的 ROM bootloader），所以原理部分通用。

## UF2 拖放为什么在这块板子上不成立

UF2 是微软为 **UF2 bootloader** 设计的一种"U 盘式"固件格式：板子跑起来后暴露成一个 U 盘，把 `.uf2` 文件拖进去，bootloader 自己把它写到 flash。

它成立的前提是**芯片里预先烧了 UF2 bootloader**（典型是 RP2040、SAMD21/SAMD51、nRF52 的 Adafruit 板）。ESP32 系列出厂只有乐鑫自己的 ROM bootloader，没有 UF2；Zephyr 也不为 ESP32 生成 `.uf2`。

所以对 Korvo-1：

| 方式 | 可用 | 原因 |
|---|---|---|
| UF2 拖放 | ❌ | 没有 UF2 bootloader，Zephyr 也不产出 uf2 |
| esptool 串口（`west flash`） | ✅ | ESP32 ROM bootloader 原生支持的协议 |
| USB-Serial-JTAG | ⚠️ | SoC 支持，但需要走 `esptool` 的 USB-JTAG 分支，本教程未用 |

**UF2 那一节在你的教程目录里可以跳过**，它不是这块板子的路径。

## UF2 在别的板子上是怎么成立的（值得看懂原理）

参考教程（YuzukiNeko / F101）那套 UF2 能拖放，靠的是**自己往 flash 里装了一个下载器程序**：

| 组成 | 作用 |
|---|---|
| 加载器（三份备份在 `0x0`/`0x10000`/`0x20000`） | 上电先跑，判断该进下载器还是进应用；按住键可强制进 |
| UF2 下载器（`0x30000`） | 跑起来后把自己枚举成 U 盘 `F101BOOT` |
| 应用（`0x200000`） | 真正的 Zephyr 程序 |

拷进 U 盘的 `.uf2` 不是"原始 bin"，每个 512 字节块里带**写入地址和 family id**，下载器解析后逐块写 NOR，写完自动重启。

```
mkuf2.py app    build/hello/zephyr/zephyr.bin -o app.uf2    # family 0x31303146，只能写应用区
mkuf2.py system build/uf2/zephyr/zephyr.bin   -o dl.uf2     # family 0x31303147，可写任意位置
```

对比一下就明白为什么 ESP32 不行：

| | F101（参考板） | ESP32-S31（Korvo-1） |
|---|---|---|
| 出厂启动 | FEL（USB 下载模式），可写任意 flash | ROM bootloader，只认串口/USB-JTAG 的 esptool 协议 |
| 有没有常驻下载器 | 有，自己装的，枚举成 U 盘 | 没有，ROM 不会枚举成 MSC |
| Zephyr 能不能产出 .uf2 | 能（fork 里带 `mkuf2.py` + 下载器） | 不能，没有这个 runner |
| 拖放烧录 | ✅ | ❌ |
| 变砖怎么救 | 按住 FEL 键，xfel 重写加载器 | `esptool erase_flash` 全片擦除，再重新烧 |

**所以要在 S31 上实现 UF2，得先有一个能枚举成 U 盘的常驻程序**（类似树莓派 Pico 的 tinyuf2，走 USB-OTG 或 USB-Serial-JTAG），还要改 Zephyr 的 flash 布局给它留出分区。投入产出比很低——`west flash` 一条命令已经够快。真想做，参考教程的下载器源码是现成的思路样板。

> 参考教程里那句警告对我们同样成立、甚至更重：能写任意位置的镜像（`system`/`loader`）写坏了，对方还能靠 FEL 救；**ESP32 没有 FEL，救砖只能整片擦除**。所以别手工往 `0x0` 写东西，永远从 `0x2000` 起。

## 实际烧录链路

```
PC ──USB(Type-C, 标 UART 那个)── CP2102N ──UART── ESP32-S31
                                     │
                             DTR/RTS 自动控制 EN 与 GPIO61
                             （进下载模式，不用手按 BOOT）
```

这正是为什么板子上有**两个 Type-C**：一个只供电（Power），一个带数据（UART）。烧录必须插后者。

## 一条命令

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon-ide\west
west flash --esp-device COM9
```

`COM9` 换成你机器上的实际端口（设备管理器里 `Silicon Labs CP210x USB to UART Bridge`）。省掉 `--esp-device` 时 esptool 会自己扫，多串口环境下容易选错。

实测输出：

```
MAC:                30:ed:a0:f3:fc:50
Uploading stub flasher...
Running stub flasher...
Stub flasher running.
Changing baud rate to 921600...
Configuring flash size...
Flash will be erased from 0x00002000 to 0x00023fff...
Wrote 135552 bytes at 0x00002000 in 2.2 seconds (499.1 kbit/s).
Verifying written data...
Hash of data verified.
Hard resetting via RTS pin...
```

看三行就够：

- `Flash will be erased from 0x00002000` —— **偏移是 0x2000**，因为 ESP32 的 bootloader 占前 8 KB；
- `Hash of data verified` —— 写进去的和编译出来的一致；
- `Hard resetting via RTS pin` —— 不用手按复位。

## 手动用 esptool（不依赖 west）

```powershell
C:\Users\xiao1\Desktop\ELAB\kharon-ide\.deps\venv\Scripts\python.exe -m esptool `
    --chip esp32s31 -p COM9 -b 921600 `
    --before default_reset --after hard_reset `
    write_flash 0x2000 C:\Users\xiao1\Desktop\ELAB\kharon-ide\build\samples\esp32s31\hello_world\default\zephyr\zephyr.bin
```

注意 `0x2000` 这个偏移，手写错成 `0x0` 会得到一个永远不进应用的板子（表现为反复打印 `Attempting to boot anyway...`）。

清空 flash（怀疑分区表被之前 ESP-IDF 的固件破坏时用）：

```powershell
python -m esptool --chip esp32s31 -p COM9 erase_flash
```

## 看串口输出

Windows 上没有现成的 `screen`，用 pyserial 抓一段最省事：

```python
import time, serial
ser = serial.Serial("COM9", 115200, timeout=1)
ser.setRTS(True);  time.sleep(0.15); ser.setRTS(False)   # 拉 EN 复位
end = time.time() + 8
while time.time() < end:
    line = ser.readline()
    if line:
        print(line.decode("utf-8", "replace").rstrip())
ser.close()
```

脚本已经放在 `zephyr-learn\tools\read_serial.py`，直接跑：

```powershell
C:\Users\xiao1\Desktop\ELAB\kharon-ide\.deps\venv\Scripts\python.exe C:\Users\xiao1\Desktop\ELAB\kharon-ide\tools\read_serial.py
```

**关键点：读串口前要先复位**，否则 `hello_world` 那行早就打印完、缓冲区里什么都没有，你会以为烧录失败。

## 两种"看起来像烧录失败"的假象

1. **串口没输出**：八成是插了 Power 那个 Type-C，或者没复位。用 `--esp-device` 显式指定端口能排除前者。
2. **烧完出一堆 `Attempting to boot anyway...`**：偏移不对（写成 0x0 了），重新用 `west flash`。

## 每次启动都会看到的 `SHA-256 comparison failed`

烧对了也会看到这一段，**它不是错误**：

```
SHA-256 comparison failed:
Calculated: b2502aab38c9a7bd841cc8e6d0f0f78226144594257c4bf1ce722355bbf8e1ea
Expected:   00000000e0e60000000000000000000000000000000000000000000000000000
Attempting to boot anyway...
entry 0x2f000380
```

原因在构建日志里那句 `Image has only RAM segments visible. ROM segments are hidden and SHA256 digest is not appended.`——Zephyr 出的镜像**没有附加 SHA256 摘要**，ESP32 的 bootloader 拿"全 0 的期望值"去比当然不相等，于是打印失败然后照样启动。

判断标准：**只要后面出现了 `*** Booting Zephyr OS build ... ***`，就是成功的**。真正烧坏的表现是这段之后什么都没有、或者反复复位。

## 自检

- 为什么 `west flash` 不需要你按 BOOT 键？（DTR/RTS 自动控制 EN/GPIO61）
- `0x2000` 这个偏移是谁决定的？（板级 cmake 里的 `Image partition address`，本质是 SoC 的 bootloader 占用）
- 如果板子只插了 Power 口，`west flash` 会报什么？（找不到设备 / 串口打开失败）
