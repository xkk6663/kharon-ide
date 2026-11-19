# tools\ · 脚本索引

> **一句话**：这个目录只放"能直接跑、且被本索引收进来"的命令。**2026-11-16 精简：69 → 36 个**（删掉的 34 个见 §10）；当天又新增 `bootstrap.ps1`（换机自举），现为 **37 个**（14 个 `.ps1` + 23 个 `.py`）。
> 路径基准都是**仓库根**；统一用 **pwsh 7**（`build.ps1`、`defender_exclude.ps1` 刻意写成 ASCII，PowerShell 5.1 也能跑；其余含中文输出）。
> 产物不写仓库别处：构建进 `build\`，实验临时目录进 `build\_scratch\`，串口/报告进 `logs\`（`build\`、`logs\` 都已 gitignore）。

---

## 0. 日常只用这三条

```powershell
# 0) 刚 clone 下来、换机器：一条命令补齐上游依赖（幂等；先加 -DryRun 看清单）
pwsh -File .\tools\bootstrap.ps1

# 1) 编 + 烧（默认 S3；产物自动落 build\<芯片>\<工程>\<配置>）
.\tools\build.ps1 -App .\applications\esp32s3\s3_wifi_portal -Board esp32s3_devkitc/esp32s3/procpu -Flash -Port COM11

# 2) 改完目录 / 新增工程后自查（只读，违规 exit 1）
pwsh -File .\tools\check-layout.ps1

# 3) 看板子（S3 原生 USB 口：被动抓，不复位）
python .\tools\probe_serial.py COM11 8
```

`bootstrap.ps1` 的四步与故障排查见 `docs\参考\环境快速搭建.md`：建 `.venv` + west → 按钉住 commit 拉 zephyr（无 blob 浅克隆）→ `west update` 只拉 6 个模块 → 把 `boards_local\` 与 `patches\zephyr\` 复原到位，末尾自检关键路径。它不编译、不烧录。

---

## 1. 构建

| 脚本 | 干什么 | 用法要点 | 产物 / 副作用 |
|---|---|---|---|
| **`build.ps1`** | **唯一构建入口**：source `env.ps1` → `west build --cmake-only` → `ninja -j8` → 可选烧录；默认带 ccache | `-App`（必填）、`-Board`（默认 S3 procpu）、`-BuildDir`、`-Jobs`(8)、`-ExtraDtcOverlay`、`-ExtraConfFile`、`-NoCcache`、`-Pristine`、`-Flash`、`-Port`(COM11)、`-Runner`、`-OpenocdConfig`、`-AllowOutsideBuild` | 镜像与中间件全在 `build\<芯片>\<工程>\<配置>`；终端打印 `OK configure=…s build=…s total=…s bin=… B` |
| `build_ota.ps1` | sysbuild 构建（mcuboot + app）+ 用 imgtool 按 `OTA_LOCAL_VERSION` 重签，再自校验镜像头版本 | `pwsh -File .\tools\build_ota.ps1 [-Version n] [-Fresh]`；版本从 `src\l0\ota.h` 读 | `build\esp32s3\s3ota\fw_v<N>.signed.bin`；`logs\build-<N>.log`、`logs\sign-<N>.log` |
| **`clean-builds.ps1`** | 回收 `build\` 空间：**先 rename 到 `%TEMP%` 再删**过期构建目录（本机 Desktop 下的删除会被拦截进回收站，直接 `Delete` 也拦不住） | `pwsh -File .\tools\zephyr\clean-builds.ps1`（默认干跑只列清单）/ `-Delete` / `-KeepDays 3` / `-All` / `-Protect <正在用的目录>` / `-TriggerFreeGB 25`；`build.ps1` 成功后会自己调一次（`-NoPrune` 可关） | 只删 `<root>\build` 内的目录；打印删除数 + 磁盘剩余变化；staged/direct 两种删除路径分开计数 |

两条硬规则（脚本里是 throw，不是建议）：

1. **`-BuildDir` 必须在 `build\` 内**；一次性实验才用 `-AllowOutsideBuild`。
2. **相对路径以"你运行脚本时所在的目录"为基准**（`env.ps1` 会把 cwd 切进 west 工作区（`west\`），脚本切之前先把 `-App` / `-BuildDir` 解析成绝对路径）。

**删构建目录别用资源管理器 / VS Code 的「删除」**：那是 shell 删除，整棵
`build\<芯片>\<工程>\<配置>`（几十万个小文件）会进回收站，条数一多回收站自己就打不开了
（2026-11-18 的 113 万条目 / 52 GB 就是这么来的，见 `docs\zephyr-learn\变更记录\构建产物清理-2026-11-18.md`）。
要回收空间用 `clean-builds.ps1`，或 `Shift+Delete`；`.vscode\settings.json` 已把 `files.enableTrash` 关掉。

> 2026-11-19 实测：本机对 `Desktop\` 下的删除做了拦截，`Directory::Delete`、`cmd rd /s /q`、
> `cmd del` 都会生成回收站条目（空间不释放）。`clean-builds.ps1` 因此改为先把目标 rename 进
> `%TEMP%`（同卷纯改名，不算删除），再在 `%TEMP%` 里删——实测回收站条目零增长。

---

## 2. 巡检

| 脚本 | 干什么 | 用法 | 输出 |
|---|---|---|---|
| **`check-layout.ps1`** | 五项只读检查，不编译不删除：[1] `applications\**\build` 不存在 [2] 根目录无 `build-*` [3] `zephyr_k\build-*` 不存在 [4] 每个 `CMakeCache.txt` 的源码路径在 `applications\` 或 west 工作区内（sysbuild 子缓存合法） [5] 每个 `CMakePresets.json` 的 `binaryDir` 落在 `build\` 内（另有 `_unsorted\` 告警） | `pwsh -File .\tools\check-layout.ps1` | `checks passed: N/5   caches: …   presets: …`；有硬违规 exit 1 |

规则出处：`docs\参考\构建入口与产物规则.md`。**当前 5/5**（2026-11-16 清掉 `applications\...\build` 残留、并把 sysbuild 子缓存判为合法）。

---

## 3. 串口 / 板子

两块板的串口不是同一种，工具也分两套，用错会得到"一片空白"或"板子被反复复位"：

| 脚本 | 适用的口 | 干什么 | 用法 | 注意 |
|---|---|---|---|---|
| `probe_serial.py` | **S3 原生 USB-Serial-JTAG（COM11）** | **被动**抓串口，不动 DTR/RTS | `python tools\probe_serial.py COM11 8` / `… 8 --reset` | 原生 USB 一复位就重新枚举，所以默认不复位；**DAP-Link CDC（COM10）上 `--reset` 无效** |
| `usb_cmd.py` | 同上 | 发 shell 命令并回收显，默认不复位 | `python tools\usb_cmd.py COM11 "app stats@3"` | 先设 DTR/RTS 再 `open()`，避免开串口时打出复位脉冲 |
| `read_serial.py` | **S31 / Korvo-1（CP2102，COM9）** | 打 RTS 复位脉冲后抓 **8 秒** | `python tools\read_serial.py` | **COM9 硬编码** |
| `serial_cmd.py` | 同上 | 发一串命令，`cmd@秒数` 单独给等待窗口 | `python tools\serial_cmd.py COM9 "lab i2c" "lab gpio@12"` | 默认每命令 2.5 s |
| `serial_monitor.py` | 任意 | 长时间把串口日志原样落盘（对齐"telnet 连上之后发生了什么"） | `python tools\serial_monitor.py COM11 120 logs\serial-telnet.log` | 打开 S3 原生口会复位一次，前几行必是启动日志 |
| `ble_scan.py` | PC 侧 | 扫 BLE 广播，高亮含 `Zephyr` / `ESP` 的条目 | `python tools\ble_scan.py 8` | 需要 `bleak` |
| `telnet_hold.py` | PC → 板子 | 连 telnet 后保持并抽干，用来分离"accept 就崩 / 发数据才崩 / 都不崩" | `python tools\telnet_hold.py <ip> [port]` | 诊断用，不写文件 |

---

## 4. Wi-Fi 吞吐与 OTA 上位机

| 脚本 | 干什么 | 用法 |
|---|---|---|
| `wifi_bench.py` | STA 连到同一 LAN 后测①TCP 建连②`/` 整页字节/秒（**必须根路径**，`/index.html` 会被 captive 302 掉）③`/api/status` 频率④`/api/scan` 字节/秒 | `python tools\wifi_bench.py 10.168.1.226 [--rounds 50]` |
| `selftest_wifi_bench.py` | 起本地假 HTTP 服务模拟三个端点，验证 **bench 脚本本身**算得对（板子没烧也能跑） | `python tools\selftest_wifi_bench.py` |
| `ota_push.py` | 主推手：`X-Version`/`X-Nonce`/`X-Sha256`/`X-Key` 全套 + 断点续传 + 轮询 stage/pct | `python tools\ota_push.py --ip … --key … --bin build\esp32s3\s3ota\fw_v137.signed.bin --version 137` |
| `ota_drop.py` | 推送到一半主动断链，验证"断电/断连续传" | `python tools\ota_drop.py …` |
| `ota_reboot.py` | 升级后触发重启并确认切槽生效 | `python tools\ota_reboot.py …` |
| `ota_resume_probe.py` | 只发头不发 body，看设备回 206（断点记录还在，异常）还是 200 `resume:0`（已清空，正确） | `python tools\ota_resume_probe.py …` |
| `remote_ota.py` | 纯 HTTP 远程升级：`GET /api/status` 取基线 → `POST :8080/ota` → 轮询 | `python tools\remote_ota.py --ip … --key … --bin … --version … [--no-reboot]` |
| `page_ota_cdp.py` | 真 Chrome + CDP 走"浏览器页面 OTA"验收：文件选择、进度、截图全走协议，可断言 | `python tools\page_ota_cdp.py --ip … --key … --bin … --version … --shot …` |

---

## 5. 页面、镜像与语法

| 脚本 | 干什么 | 用法 / 注意 |
|---|---|---|
| **`gen_pages.py`** | `src\user_page.html` → `src\l1\pages.h`（每行一个 C 字面量，转义全由机器做） | `python tools\gen_pages.py` / `--check`（只校验）。**输出固定在 l1**，别放回 `src\` 根（会遮住正式副本） |
| `page_check.py` | 页面内容边界（不许出现开发者字样/接口）+ JS 过 `node --check` | `python tools\page_check.py --node <node.exe>`；`build_ota.ps1` 编译前强制跑 |
| `img_hdr.py` | 读 mcuboot 镜像头里的版本，不依赖 imgtool | `python tools\img_hdr.py <xxx.signed.bin>` |
| `sha_golden_test.py` | 把页面里的 C/JS 摘要实现与 Python `hashlib` 对拍（空串 / `abc` / 真实固件三例） | `python tools\sha_golden_test.py`；固件样本自动取 `build\esp32s3\s3ota\fw_v*.signed.bin` 里最新的一份 |
| `syntax_check_main.py` | 只对 `s3_wifi_portal\src\main.c` 做 `-fsyntax-only`（参数从 `compile_commands.json` 取），不产出镜像、内存占用很小 | `python tools\syntax_check_main.py`；构建目录硬编码 |

---

## 6. 编译速度实验（测量工具，不加速编译本身）

真正让编译变快的是 `env.ps1` 里的 ccache + `-j8` + Defender 排除；这一组的意义是**换机器 / 换 SDK / 换 `-j` 时能用同一口径重测**。结论见 `docs\参考\编译速度优化.md`。

| 脚本 | 测什么 | 构建目录 / 报告 |
|---|---|---|
| **`speed_full.ps1`** | 最正式的一次：`ccache -C/-z` 清零 → 冷缓存全量 → 热缓存全量 | `build\_scratch\s1`、`s2` → `logs\speed-full.txt` |
| `speed_bench.ps1` / `2` / `3` | 同一实验的三代草稿（默认并行/`-j8`/缓存首编/二编）；**要重测用 `speed_full.ps1`**，这三份只作"当时怎么测"的对照 | `build\_scratch\speed-*`、`r1…r3` |
| `ccache_micro.py` | 单文件微基准（裸编 / 冷 / 热 / 再热 + 加速比），自动找第一个 `compile_commands.json` | `build\_scratch\ccbench` |
| `defender_exclude.ps1` | 给 `build\*`、`.ccache\`、`.tools\ccache\`、`zephyr_k\`、SDK 加 Defender 排除（**要管理员**；Defender 不支持通配符，逐个枚举） | 打印 added / skipped |

> 跑完想回收空间：整目录删 `build\_scratch`（不在巡检约束内，也不进 git）。

---

## 7. 模块获取与一次性维护

| 脚本 | 干什么 | 前提 / 副作用 |
|---|---|---|
| `pull-stm32-modules.ps1` | 把 `hal_stm32` / `cmsis_6` / `mbedtls` / `tf-psa-crypto` 从"手工离线复原"换成**标准上游 git 仓**：读 `west.yml` 的 revision（含 `defaults: remote upstream` 推出的 URL）→ 大仓走无 blob 克隆 + 稀疏检出（分批补 blob），小仓整仓浅克隆 → 校验 `HEAD == pin` 与关键路径 | 旧目录先挪到 `zephyr_k\_offline_bak_<时间戳>\`，失败自动挪回；默认走代理 `127.0.0.1:7890`（`-Direct` 关）、分批取（`-Full` 强制整仓）。先 `-DryRun` 看清单。**四个模块都已实跑成功**（STM32 两枚：F103/F411 复编 + 烧录 + 串口全过；crypto 两枚：整仓直下）。crypto 的仓在 `github.com/zephyrproject-rtos/…` 镜像里，不是 `Mbed-TLS/…` 原厂，用错会报 `upload-pack: not our ref`。稀疏仓补齐用 `git sparse-checkout disable`；crypto 换完后必须复编 ESP32（`zephyr` 里有 `modules/mbedtls/legacy_support.cmake` 本地补丁） |
| `fetch_s3_blobs.ps1` | 下载 S3 的 9 个 Wi-Fi/BLE blob 到 `modules\hal\espressif\lib\esp32s3`，逐个 SHA256 校验 | ⚠️ **多半不用跑**：blob 现在随 `hal_espressif` 模块自带（`…\zephyr\blobs\lib\esp32s3\`） |
| `fetch_tfpsa.ps1` | 补 `tf-psa-crypto` 模块（S3 硬件加密 / BLE 需要）：下载 zip → 校验 → 解到 `zephyr_k` | 5 个镜像 × 最多 12 次重试；失败会留 `.tmp-tfpsa.zip` |
| `commit_wifi_milestone.ps1` | **历史一次性**：把 S3 配网门户那轮成果按"固件 / 工具 / 文档"分成 3 个提交 | **会真的 `git commit`**；只在复刻那轮时用 |
| `_rollback-layout-2026-11-14.ps1` | 回滚 2026-11-14 的 apps 目录搬迁 | ⚠️ **部分失效**：`build\_legacy\` 已删，脚本里 `From = 'build\_legacy\…'` 的条目找不到源；回滚 `applications\` 的条目仍可用 |

---

## 8. 呼吸灯同步规范

规则正文见 `docs\参考\同步呼吸灯规则.md`；这两个文件是它的**生成器**和**一致性验证器**，都不碰硬件、不写 `build\`、不联网。

| 文件 | 干什么 | 用法 |
|---|---|---|
| **`gen_breath_table.py`** | 规范的**唯一源头**：由周期/gamma/表长算 256 项亮度表、FNV-1a 校验和、12 条验收向量 | `python tools\gen_breath_table.py`（打印）/ `--inplace`（就地改写 `applications\common\src\breath_sync.c` 的 GENERATED 区段）/ `--diagram` / `--json` |
| **`test_breath_sync.c`** | PC 上 11 组验证（校验和、验收向量、双机同刻、相位环形、校时三路径、漂移学习、单色与 RGB 同相、freerun 降级…） | `gcc -std=c99 -Wall -Wextra -I applications\common\include tools\test_breath_sync.c applications\common\src\breath_sync.c -o $env:TEMP\breath.exe`，末行须为 `RESULT: PASS (0 failure(s))` |

改顺序：改 `gen_breath_table.py` 顶部常数 → `--inplace` 重新生成 → 跑 `test_breath_sync.c` → 期望值（向量、校验和、文档 §5.4/§11）跟着改。

---

## 9. 加新脚本的约定

1. 文件头写三行：**干什么 / 用法 / 产物落哪**。
2. **产物不许落仓库根**：构建进 `build\`，实验进 `build\_scratch\<实验名>`，日志进 `logs\`。
3. **一次性脚本用完就删**；确需短期保留才加 `_` 前缀，并在本表 §7 写明"失效 / 一次性"。
4. 碰网络 / 要管理员 / 会写 git 的脚本，必须在本表写清前提（防止半年后被随手跑一遍）。
5. **索引完整性**：新脚本必须在本表出现；本表没有的脚本，视为可删。

---

## 10. 2026-11-16 精简：删掉的 34 个脚本

判定口径只有一条：**除自己外，仓库里（源码 + 文档 + 其它脚本）没有任何引用，且不在本索引里**。
全部先确认 `git ls-files` 有记录（删得回），再逐个 `git rm`。恢复某个：`git show HEAD~1:tools/<名> > tools/<名>`。

| 类别 | 脚本 | 为什么算死 |
|---|---|---|
| 一次性改码脚本（12） | `_patch_delay` `_patch_otah_v2` `_patch_otaui` `_patch_psa` `_patch_quickcmd` `_patch_shcard` `_patch_shell` `_patch_ver107` `_patch_ver109` `_fix_nonce_file` `_nonce_debug` `_q` | 往源码里拼字面量的一次性动作；路径还是改造前的 `src\ota.h` / `src\main.c`，**再跑会改错文件** |
| 一次性探针（19） | `_boot_capture` `_boot_watch` `_drop_probe` `_hang_probe` `_probe_8080` `_reason_probe` `_reason_probe2` `_reboot_probe` `_remote_probe` `_serial_nors` `auth_probe` `ota_auth_probe` `ota_gate_probe` `ota_reboot_auth_probe` `port_probe` `shell_probe` `shell_quick_probe` `telnet_probe` `wlog_probe` | 当初为定位某个具体 bug 临时写的；对应的结论已进 `docs\教程\17-…\17.8-故障排查实录.md` |
| 连带失效（3） | `window_poll` `lan_find_s3` `serial_capture` | 只被上面已删的脚本引用；`serial_capture` 同时被 `_serial_nors` / `_boot_capture` 引用，删完即孤儿 |

同时清掉的数据件与临时产物：

| 物 | 原因 |
|---|---|
| `tools\_shcard_js.txt`、`tools\_shblock.txt` | 只被 `_patch_shcard` 等已删脚本读取；`_*.txt` 本就在 gitignore 里 |
| `build\page_check.js`、`build\user_page.{html,js,lines.js,pretty.js}` | `page_check` 的中间文件落在了 `build` 根，违反"实验文件进 `build\_scratch\`" |
| `applications\esp32s3\s3_wifi_portal\build\` | 裸 `west build` 落点（巡检项 [1] 的唯一违规源） |
| `build\esp32s31\`、`build\samples\` | 空目录 |

**保留但没删的**：`_otaauth_js.txt`、`_sha_test.js`（+ 三个 `_sha_test.js.*.bin`）被 `sha_golden_test.py` 读取；
`tools\.ota_nonce` 被 `ota_push` / `ota_drop` / `ota_resume_probe` 与两篇教程用；
`_rollback-layout-2026-11-14.ps1` 被两篇变更记录引用（是证据，不是工具）。
