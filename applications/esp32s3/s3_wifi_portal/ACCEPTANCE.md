# s3_wifi_portal 验收清单（一档 + 二档）

> 状态：**2026-11-14 22:11 已编译、烧录（761620 B，Hash verified）并完成 5.1–5.4 实机验收。**
> 本文档保留为"下次回归怎么验"的操作手册；实测结果见下表。

## 验收结果（2026-11-14）

| 项 | 结果 | 证据 |
|---|---|---|
| 3. 编译 | 通过 | `ninja rc=0`，`[270/270]`，镜像 761620 B（旧 760644） |
| 4. 烧录 | 通过 | `Wrote 761620 bytes ... Hash of data verified` |
| 5.1 上电重连 | 通过 | NVS 读到 `"501A"` → `connecting` → `Received: 10.168.1.226` → `connected`（约 11 s） |
| 5.2 运行期掉线重连 | 通过 | `disconnected` → `runtime reconnect 1/10, retry in 3000 ms` → `attempt 1/10` → `reconnected to "501A" (total 1)`，共 6.2 s |
| 5.3 网页/API | 通过 | `/` 200（3878 B，含「信号强度」「掉线重连」）；`/api/status` 返回 7 字段，`rssi=-40 reconnects=1` |
| 5.4 吞吐基线 | 通过 | status 112 B / 22.9 ms / ≈28 kbit/s；页面 4118 B / 200.8 ms / ≈177 kbit/s |
| 6. 绿灯呼吸 | 已定位 | `led: state=2 env=4..99 err=0 fails=0`——呼吸一直在跑，**之前"不呼吸"是亮度上限 80/255 太暗**，现已提到 160 |
| DHCP 16 槽 | 未验证 | PC 无法连 AP（`netsh wlan connect` 之前验证过无效），需手机端反复重连才测得出 |
| 10 次上限负向 | 未验证 | 需构造"一直连不上"，会覆盖 NVS 好凭据，暂不做 |

### 本轮新发现的问题（未修）

**`/api/scan` 在 STA 已连接时返回空列表**：实测响应仅 27 B（`{"scanning":false,"aps":[]}`），而 self-test 里同一路径是 200 + 2 KB 数据。怀疑是 Wi-Fi 驱动在 STA 处于 connected 状态时不再返回扫描缓存。影响：网页"Wi-Fi 搜索列表"在已连网时可能是空的，需要复现并确认是否要从 AP 侧扫描。

---

## 0. 前提

板子已插在 **COM11**（S3 原生 USB-Serial-JTAG）。串口被占用时先关掉占用进程：

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'python|putty|SecureCRT' } | Select-Object Id, ProcessName
```

（`probe_serial.py` / `usb_cmd.py` 跑过之后会占着口，烧录前必须停掉。）

打开串口会让板子复位——**这属于正常现象**，日志从 ESP-ROM 开始重来。

---

## 1. 先提交（10 秒）

```powershell
pwsh -File tools\commit_wifi_milestone.ps1
```

预期：3 个 `[完成]`，最后 `git log --oneline -3` 能看到 `feat(wifi)` / `tools:` / `docs:` 三条。

失败时：脚本用 `git add -- <明确路径>`，不会误加 `build-*`、`.venv`、`zephyr_k`。

---

## 2. 自测吞吐工具（3 秒，不需要板子）

```powershell
python tools\selftest_wifi_bench.py
```

预期输出里带三行 `kbit/s`，最后一行 `自测结果: 通过`。

这一步的意义：**先证明工具本身没毛病**。如果跳过它直接烧录，等真机跑出 0 kbit/s 时，你分不清是工具坏了还是链路有问题。

---

## 3. 编译（1–2 分钟）

```powershell
cd C:\Users\xiao1\Desktop\esp32\zephyr-learn\zephyr_k
. .\env.ps1
$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"
& "$root\.tools\ninja\ninja.exe" -C "$root\build\esp32s3\s3_wifi_portal\default" -j 6
```

预期：从 `[207/270]` 续跑，走完 `Linking` + 生成 `zephyr.elf` / `zephyr.bin`，末行是 `[270/270]`。

失败时：若又出现 `cc1.exe: out of memory`，说明上次没真正重启过（或重启后又跑满了），看第 0 节的诊断思路。

---

## 4. 烧录

```powershell
& "$root\.venv\Scripts\west.exe" flash -d "$root\build\esp32s3\s3_wifi_portal\default" --esp-device COM11
```

预期：`Writing at 0x...` 若干行，最后 `[100%] Done`。

---

## 5. 实机验收（四项，缺一不可）

### 5.1 上电自动重连（原有功能，确认没被改坏）

看启动日志，应依次出现：

```
portal: stored creds found, ssid="<你的路由器>"
portal: auto reconnect 1/3
net: connected
portal: got address 10.168.1.xxx
```

### 5.2 运行期掉线重连（**本轮新增，重点**）

串口里敲：

```
uart:~$ portal status
uart:~$ portal drop
```

`portal status` 预期：

```
state=connected ssid=501A ip=10.168.1.226 rssi=-5x reconnects=0 uptime=...
```

`portal drop` 之后**应在 3 秒内**看到：

```
portal: runtime reconnect 1/10, retry in 3000 ms
portal: runtime reconnect attempt 1/10
portal: reconnected to "501A" (total 1)
```

然后 `portal status` 里 `reconnects=1`。

判定要点：
- 日志里的次数**必须是 1/10，不能是 2/10**（这是复核时修掉的日志错位）；
- 灯从绿 → 红/蓝 → 绿，最终回绿。

### 5.3 网页新增字段

手机或 PC 打开 `http://s3portal.local`（或 `http://10.168.1.226`），页面上应多出两行：

```
信号强度：-5x dBm
掉线重连：N 次 · 运行 M 分钟
```

### 5.4 吞吐基线（二档的量产数据）

```powershell
python tools\wifi_bench.py 10.168.1.226 --rounds 20
```

预期三行数据 + 最后一句「按上面带宽估：传 760 KB 固件约需 N s」。

**先记下这个数**，它是后面做 OTA 时的基准。经验值：ESP32-S3 单 TCP 吞吐一般 1–3 Mbit/s，所以 760 KB 固件大约 3–6 秒（不含 flash 写入）。

---

## 6. 顺带验证的遗留问题

### 绿灯不呼吸到底为什么

看串口里每 2 秒一次的 LED 日志：

```
led: state=1 env=34 err=0 fails=0
```

- `fails` 持续增长、`err` 非 0 → 是 RMT 传输失败（底层 `pulse_io_transmit_sync` 超时），属于驱动/时序问题；
- `err=0`、`fails=0` 但灯看着不呼吸 → 是亮度不够（上限 160/255），属于观感问题，可以把 `breathe()` 的 160 调到 200 以上。

这两个结论会决定下一步往哪改，所以别跳过这行日志。

---

## 7. 已知未验证项（别当成已完成）

| 项 | 说明 |
|---|---|
| DHCP 池扩容 | `prj.conf` 已改 16 槽 / 租约 900 s，**从未编译进固件**，需本次烧录后才能验证"手机反复重连不再转圈" |
| 10 次上限负向验证 | 需要"让它连不上"（如改错密码或关路由器）才能触发，会覆盖 NVS 里的好凭据，做之前先记下原密码 |
| 30 s 稳定判定 | 逻辑已实现，但"连上 2 秒又掉"这个边缘场景难以人为构造，可能长期验证不到，属正常 |
