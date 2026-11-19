# s3_wifi_portal —— ESP32-S3 Wi-Fi 配网门户

一个可用的成品级配网工程：板子自己开热点，手机连上来打开网页看扫描结果、填 SSID/密码发起连接，
连上以后凭据写进 NVS，下次上电自动重连（最多 3 次）。状态用板载 WS2812 呼吸灯表达。

- 目标板：`esp32s3_devkitc/esp32s3/procpu`
- 构建目录：`build\esp32s3\s3_wifi_portal\default`
- 入口逻辑：`src/main.c`

---

## 1. 功能与现象对照

| 功能 | 实现 | 现象 |
|---|---|---|
| AP + STA 同时在线 | `CONFIG_WIFI_USAGE_MODE_STA_AP`，驱动内 `esp_wifi_set_mode(APSTA)` | 手机能扫到 `S3-Portal`，板子同时能连路由器 |
| AP 网段静态 IP | `192.168.4.1/24` | 打开 `http://192.168.4.1/` |
| DHCP | `net_dhcpv4_server_start()`，池 `192.168.4.10–13` | 手机自动拿到 `192.168.4.1x` |
| mDNS | `CONFIG_MDNS_RESPONDER` + `CONFIG_NET_HOSTNAME="s3portal"` | `http://s3portal.local/` |
| 网页配网 | 内嵌 HTML + 4 个 HTTP handler | 列表刷新、提交 SSID/PSK |
| 状态灯 | WS2812（IO48，RMT）2.4 s 三角波呼吸 | 空闲红 / 连接中蓝 / 成功绿 |
| 凭据持久化 | settings + NVS，键 `portal/wifi` | 断电重启自动重连 |
| 自动重连（上电） | 3 s 固定间隔，最多 3 次 | 全失败回红灯，等网页重填 |
| 自动重连（运行期掉线） | 3/6/12/24 s 退避，最多 10 次 | 路由器重启、走出范围后自己连回来，不用复位 |
| Captive Portal（自动弹网页） | DNS 劫持(UDP 53) + HTTP `/*` 302 + DHCP 114 | 手机连上 AP 自动弹出配网页，不用手输地址 |

### Captive Portal 为什么会「自动弹」

系统判断「这是个需要登录的网络」靠的是**探测失败**：连上后它会去请求一个已知地址，
拿到预期内容就当能上网，拿到别的（302/登录页/超时）就当是门户，于是弹出登录界面。
这条链上必须三件事同时成立，少一件手机就只显示「已连接但无法访问互联网」，不会弹：

| 环节 | 手机侧行为 | 本项目实现 |
|---|---|---|
| DNS | 解析 `captive.apple.com` / `connectivitycheck.gstatic.com` | DHCP 把 DNS 服务器地址发成 `192.168.4.1`（`NET_DHCPV4_SERVER_OPTION_DNS_ADDRESS`），客户端才会来问我们 |
| DNS 应答 | 请求必须有人回，否则直接超时 | `dns_hijack_thread`：UDP 53 上把所有 A 查询都回答成 `192.168.4.1` |
| HTTP | 探测请求（`/hotspot-detect.html`、`/generate_204`、`/connecttest.txt`）要给出非预期响应 | `HTTP_RESOURCE_DEFINE(..., "/*", ...)` 通配资源统一回 `302 → http://192.168.4.1/index.html` |
| DHCP 提示（可选加分） | Android / Windows 会读 option 114 | `NET_DHCPV4_SERVER_OPTION_CAPTIVE_PORTAL=y`，URI 自动生成为 `http://192.168.4.1/generate_204` |

精确资源（`/`、`/api/*`）先于通配资源匹配，所以配网页和接口不受影响；只有「没命中任何精确路径」的探测请求才会被 302。

**验证方法**：手机上「忘记」`S3-Portal` 再重连（探测只在首次接入时做，已连过不会重弹）；
iOS 会直接拉起一个迷你浏览器，Android 会在通知栏提示「登录网络」。串口对应日志：

```
<inf> portal: self-test: GET /generate_204 -> "HTTP/1.1 302"     ← 通配 302 生效
<inf> portal: dns self-test: captive.apple.com -> 192.168.4.1    ← DNS 劫持生效
```

**已知限制**：部分 Android 12+/iOS 的新版探测会走 HTTPS（`https://www.google.com/generate_204`）。
本项目只跑 HTTP（没上 TLS），这类探测会 TLS 握手失败，系统通常仍会判定「无互联网」并提示，
但不保证一定能拉起弹窗 —— 这种情况下手输 `192.168.4.1` 仍可用。

### 接口

| 路径 | 作用 | 返回 |
|---|---|---|
| `/`、`/index.html` | 配网页面 | HTML |
| `/api/scan` | 触发扫描并返回缓存列表 | `{"scanning":bool,"aps":[{"ssid","rssi","lock"}]}` |
| `/api/status` | 当前连接状态 | `{"state","ssid","ip","msg","rssi","reconnects","uptime"}` |
| `/api/connect` | POST `{"ssid":"...","psk":"..."}` 发起连接 | `{"ok":true}` |

`/api/scan` 有扫描缓存：`MAX_APS 20`，同名 AP 去重只留最强 RSSI，用 `ap_lock` 保护。

---

## 2. 构建与烧录

```powershell
# 环境（统一 SDK 1.0.1：riscv64 + xtensa-esp32s3 + arm-zephyr-eabi 三个 target）
cd C:\Users\xiao1\Desktop\esp32\zephyr-learn\zephyr_k
. ..\env.ps1

# 增量编译（首次要把整棵树编一遍）；本机缓存目录不带 `default`，与 build.ps1 的默认命名不同
& ..\.tools\ninja\ninja.exe -C ..\build\esp32s3\s3_wifi_portal -j 8

# 烧录（S3 原生 USB-Serial-JTAG = COM11，VID 303A / PID 1001）
& ..\.venv\Scripts\west.exe flash -d ..\build\esp32s3\s3_wifi_portal --esp-device COM11
```

改一个 `src\*.c` 只重编它自己 + 一次重新链接（实测 **10–11 s**，`-j 8`）；改 `prj.conf` / `.overlay` 会触发 configure + 全量（5–8 min），所以配置类改动最好攒成一批。

---

## 3. 用法

1. 手机连 Wi-Fi `S3-Portal`（密码 `<REDACTED-PSK>`）。
2. 浏览器打开 `http://s3portal.local/` 或 `http://192.168.4.1/`。
3. 点「扫描」看周围 AP；填 SSID + 密码，点「连接」。
4. 灯语：红 = 未连接，蓝 = 连接中，绿 = 连接成功（呼吸）。
5. 成功后凭据入 NVS；断电重启，板子会自己重连，3 次不成功就停下等网页操作。

串口 `COM11` 115200，可看日志；`net` shell 可用：

```
uart:~$ net iface                    # wlan0 是否 192.168.4.1
uart:~$ net dns resolve s3portal.local
```

自带两个调试命令（`src/l1/shell_cmds.c` 里注册的 `portal` 父命令）：

```
uart:~$ portal status
state=2 ssid="501A" ip=10.168.1.226 rssi=-52 reconnects=1 uptime=3100s

uart:~$ portal drop                  # 主动断开 STA，用来现场验证"掉线自动重连"
disconnect request -> 0
<inf> portal: disconnected
<inf> portal: runtime reconnect 1/10, retry in 3000 ms   ← 3 s 后重连
<inf> portal: reconnected to "501A" (total 2)
```

### 掉线重连的两套计数，别混

| 场景 | 变量 | 间隔 | 上限 | 超限后 |
|---|---|---|---|---|
| 上电，NVS 里有凭据 | `g_auto_reconnect` / `g_auto_attempts` | 固定 3 s | 3 次 | 红灯静置，等网页重新填 |
| 连上之后再掉线（路由器重启、走远） | `g_rt_reconnect` / `g_rt_attempts` | 3→6→12→24 s 退避 | 10 次 | 红灯静置，等网页重新填 |

两套的清零规则不一样，这是踩过坑之后定的：

- **上电那套**：连上即清零（`g_auto_attempts`），语义就是"试 3 次连不上就放弃"；
- **运行期那套**：连上**不清零**，要等这次已经连满 30 s（`g_connected_at`）才清零。
  否则信号边缘会出现"连上 2 秒又掉"的循环，每次连上都把计数归零，10 次上限永远触发不了；
- 用户从网页主动发起的连接，两套都不算，`g_rt_attempts` 直接归零（换了网络不该继承旧账）。

运行期重连成功会把 `g_reconnects` +1，网页和 `portal status` 都能看到——这是判断
"板子是不是偷偷掉过线"的唯一入口。

为什么退避：路由器重启一般要 10–30 s 才恢复，固定 3 s 猛打会把 10 次机会 30 秒内烧完；退避到 24 s 时，10 次能覆盖 3 分钟。

上电自检会自动打四行，用来在没有手机的情况下判断链路是否通（下面是实机抓到的真实日志）：

```
<inf> portal: dns hijack ready on udp/53
<inf> portal: self-test: GET /api/status -> "HTTP/1.1 200"
<inf> portal: self-test: GET /api/scan -> "HTTP/1.1 200"
<inf> portal: self-test: GET /generate_204 -> "HTTP/1.1 302"   ← 通配 302
<inf> portal: dns self-test: captive.apple.com -> 192.168.4.1  ← DNS 劫持
<inf> portal: NVS credential loaded: "501A"
<inf> portal: auto reconnect to saved network "501A"
<inf> portal: connecting to "501A" ...
<inf> net_dhcpv4: Received: 10.168.1.226
<inf> portal: STA got IP: 10.168.1.226
<inf> portal: connected to "501A"                              ← 自动重连成功
```

---

## 4. 关键配置（prj.conf）

```conf
# socket service 的 poll 数组，默认 3 太小：DHCP + HTTP 等服务共 4 个 fd
CONFIG_ZVFS_POLL_MAX=16
# net_context 池（socket 对象）默认 6 会被占满 → 自检 connect 报 111
CONFIG_NET_MAX_CONTEXTS=16
# TCP 连接表：HTTP(4 clients) + DHCP(2) + mDNS + 自检
CONFIG_NET_MAX_CONN=16
# Wi-Fi 连接跑在系统工作队列上，1024 会栈溢出 → 6144
CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE=6144
CONFIG_STACK_SENTINEL=y

CONFIG_WIFI_NM=y
CONFIG_WIFI_USAGE_MODE_STA_AP=y
CONFIG_WIFI_MGMT_AP_MAX_NUM_STA=4

CONFIG_HTTP_SERVER=y
CONFIG_HTTP_SERVER_VERSION_1=y
CONFIG_HTTP_SERVER_MAX_CLIENTS=4
# Captive Portal 需要通配资源（"/*" 一律 302）
CONFIG_HTTP_SERVER_RESOURCE_WILDCARD=y

# Captive Portal 的另外两半：DHCP 把 DNS 指向自己 + option 114
CONFIG_NET_DHCPV4_SERVER_OPTION_DNS_ADDRESS="192.168.4.1"
CONFIG_NET_DHCPV4_SERVER_OPTION_CAPTIVE_PORTAL=y

CONFIG_MDNS_RESPONDER=y
CONFIG_NET_HOSTNAME="s3portal"

CONFIG_SETTINGS=y
CONFIG_NVS=y
CONFIG_FLASH=y
CONFIG_FLASH_MAP=y
CONFIG_FLASH_PAGE_LAYOUT=y
```

---

## 5. 踩过的坑（都已修复，别回头）

1. **`CONFIG_ESP32_WIFI_SOFTAP_SUPPORT` 无提示符** —— 写进 `prj.conf` 会 configure 失败，删掉即可（AP 能力由别的符号间接推导）。
2. **链接报 `_http_resource_desc_portal_service_list_start` undefined** —— Zephyr 的 HTTP server 资源段是 iterable section，应用侧必须自己声明。`zephyr_iterable_section()` 单独用**不生效**，正解是工程内加 `sections-rom.ld`（`ITERABLE_SECTION_ROM(http_resource_desc_portal_service, 4)`）+ `zephyr_linker_sources(SECTIONS .../sections-rom.ld)`。
3. **`net_sock_svc: You have 4 services to monitor but 3 poll entries configured`** —— 这条日志出现就意味着 socket service 线程**直接 FAILED**，之后所有注册（DHCP、其它服务）都返回 `-EIO(-5)`。根因是 `CONFIG_ZVFS_POLL_MAX` 默认 3，必须 ≥ 服务数 + 1。
4. **`net_conn: Not enough connection contexts` + `connect` 返回 111** —— `CONFIG_NET_MAX_CONN` 不够（HTTP 4 客户端就把默认值吃光了），本项目设 16。
5. **`CONFIG_NET_*_LOG_LEVEL=数字` 会被 Kconfig 拒绝** —— 这类符号由 `Kconfig.template.log_config.net` 生成，没有提示符，只能写 `CONFIG_XXX_LOG_LEVEL_DBG=y` 的 choice 形式。
6. **`CONFIG_NET_TCP_MAX_RX_PKTS=32` 超 range** —— 直接 `Aborting due to Kconfig warnings`，别乱加。
7. **`K_WORK_DELAYABLE_DEFINE(x, NULL)` 是死代码** —— handler 没绑定就永远不执行；必须绑定真实回调（本项目连接超时就是这么失效过）。
8. **`SETTINGS_STATIC_HANDLER_DEFINE` 只有 6 个参数**，写成 7 个会编译报错。
9. **DHCP 必须在 `NET_EVENT_WIFI_AP_ENABLE_RESULT` 里起**，提前调返回失败；而这个事件可能来多次，要用 `ap_dhcp_started` 挡一下，否则第二次会 `-EALREADY(-120)`。
10. **ninja 路径是 `.tools\ninja\ninja.exe`**，不是 `.tools\ninja.exe`；`-j18` 会 `cc1.exe: out of memory`，`-j 8` 稳定。
11. **只给 IP 不给 DNS，Captive Portal 永远不会弹** —— 手机解析不了探测域名就直接超时，系统只会说「无法访问互联网」。必须 `NET_DHCPV4_SERVER_OPTION_DNS_ADDRESS` 指向 AP 自己，再在 UDP 53 应答。
12. **通配资源顺序**：`http_server_find_resource` 先做全等匹配、后走 `fnmatch` 通配，所以 `"/*"` 不会抢 `/api/*`；但通配资源里别再写第二套 `/api` 的兜底，容易互相遮蔽。
13. **`CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE=1024` 会在连 Wi-Fi 时直接崩** —— 连接是 `k_work_submit` 到系统工作队列里执行的，调用链 `net_mgmt → wifi_mgmt → esp_wifi → wpa_supplicant` 很深，1024 字节栈溢出后表现是 **切换线程时上下文损坏**：

    ```
    ** CPU 0 EXCCAUSE 28 (load prohibited)   PC 0x... (xtensa_restore_high_regs)
    >>> ZEPHYR FATAL ERROR 0: CPU exception on CPU 0
    ```

    PC 落在 `_restore_context` 这种地方就是典型「栈写炸了」，本项目设 6144，并加 `CONFIG_STACK_SENTINEL=y` 让真溢出时报得明确。
14. **`CONFIG_NET_MAX_CONTEXTS` 默认 6，会被正好占满** —— 监听 socket(1) + DHCP(2) + mDNS(1) + DNS 劫持(1) + 自检(1) = 6，再加一个就 `net_tcp: net_context_get(): -2` / `Cannot allocate a new TCP connection`，自检的 `connect` 变成 `111`。注意它和 `NET_MAX_CONN` 是两个池：context 是 socket 对象池，conn 是 TCP 连接表，两个都要放宽。
15. **`K_THREAD_DEFINE(..., delay)` 里起线程要挑时机** —— 把 DNS 劫持线程设成 1500 ms 自动启动，正好撞上 Wi-Fi 驱动初始化，表现为日志停在那一行、系统像死掉。改成 AP 起来后由 main 用 `k_thread_create` 显式启动就正常了。
16. **手机连不上、一直转圈 = DHCP 地址池被历史租约占满**（这一条是事后才查出来的，代价最大）

    现象：手机点 `S3-Portal` 之后一直在「正在获取 IP 地址」转圈，而板子串口看起来一切正常（AP up、DHCP up、自检全绿）。

    根因是两个默认值叠加：

    | 默认值 | 后果 |
    |---|---|
    | `CONFIG_NET_DHCPV4_SERVER_ADDR_COUNT=4` | 池里只有 `192.168.4.10` ~ `.13` 四个地址 |
    | `CONFIG_NET_DHCPV4_SERVER_ADDR_LEASE_TIME=86400` | 每个地址一旦分出去，**24 小时**内不回收 |

    而 iOS/Android 的「私有 Wi-Fi 地址」会在**忘记网络重连时换一个随机 MAC**。每换一次 MAC，DHCP server 就认为是一个新客户端、占掉一个新槽。
    为了验证 Captive Portal，提示用户「忘记网络再连」几次之后，4 个槽就满了，之后任何设备都拿不到 IP，而且复位前永远恢复不了。

    怎么快速确认（三个证据任一即可）：

    - 串口出现 `net_dhcpv4_server: No free address found in address pool`；
    - 板子 shell 里 `net dhcpv4 server status 2` 看到 4 个槽全是 `ALLOCATED`；
    - 断电重启后手机立刻能连上（因为池在 RAM 里，复位即清零）—— 这一条最容易验证，也最容易被误判成「Wi-Fi 驱动不稳」。

    修法（本项目已改）：

    ```conf
    CONFIG_NET_DHCPV4_SERVER_ADDR_COUNT=16        # 池 192.168.4.10 ~ .25
    CONFIG_NET_DHCPV4_SERVER_ADDR_LEASE_TIME=900  # 15 分钟就回收
    CONFIG_NET_DHCPV4_SERVER_ADDR_DECLINE_TIME=120
    CONFIG_NET_DHCPV4_SERVER_ICMP_PROBE_TIMEOUT=200  # 分配前探测 1s → 0.2s
    ```

    另外加了 `NET_EVENT_WIFI_AP_STA_CONNECTED` 日志（打印客户端 MAC），下次一眼就能看出「手机到底有没有关联上来」。

---

## 6. 代码结构（分层）

`src\main.c` 只剩 **217 行**：硬件就绪时间线 + 两个事件回调。真正的代码在 `src\l0| l1| l2` 三层里，
依赖只能往下走（L2 不认识 HTTP，L1 不认识 flash）：

```
src\main.c              217 行   编排：AP 起来 → 配网页 → 自检（刻意不用 SYS_INIT）
src\portal_cfg.h                 跨层共享的引脚/宏（AP_SSID、AP_PSK 在这里）
src\user_page.html               配网页源文件（由 tools\gen_pages.py 生成 l1\pages.h）
│
├── l2  服务层（不认识 HTTP，不认识 flash）
│     cred        NVS 凭据读写
│     wifi_scan   扫描缓存（接口是 scan_lock/scan_entries/scan_count/scan_unlock）
│     wifi_link   连接状态机、超时、退避重连、运行期掉线
│     portal_ap   AP 启动与 STA 计数
│     time_sync   SNTP
│     netcheck    联网判定
│     led_status  灯语线程
│     dns_hijack  captive portal
│
├── l1  接口层（认识 http，不认识 flash）
│     http_core   HTTP 服务与路由、来源（AP/STA）判定
│     api_wifi    scan/connect/forget/saved/api/rssi 等 JSON 接口
│     api_dev     status/ota/wlog/drops/boot 等开发者接口
│     page        三张页面 + captive 跳转（HTML 在 page.c，用户页由 pages.h 注入）
│     shell_cmds  portal 命令组
│     selftest    上电自检四行
│
└── l0  引擎层（认识 flash / mcuboot，不认识 http）
      ota         OTA 后端表（begin/write/finish/abort）+ 四条 OTA HTTP 入口
      ops         访问口令、日志环、OTAS 快照等设备侧机制
```

OTA 的**协议部分**已经下沉到跨工程组件 `applications\common`：
`include\ota_engine.h` / `src\ota_engine.c` 管头解析、收包节奏、断点偏移、摘要结束时机、
失败回码；本工程只提供 `ota_backend` 四个回调。换芯片时重写的是回调，不是协议
（见 `docs\参考\应用层分层改造教程-s3_wifi_portal.md` §6）。

四个 handler 原来共用一块 `g_json[1600]`，4 客户端并发会互相踩，现已拆成 `g_scan_json` / `g_status_json` / `g_conn_json`；
`status` 里的 SSID/msg 走 `json_escape()`。

---

## 7. NVS 布局

`storage_partition` @ `0x3b0000`，192 KB（板级 `partitions_0x0_amp_4M.dtsi`）。
settings 键前缀 `portal`，子键 `wifi`，值结构：

```c
struct cred_blob {
    uint8_t ssid_len;
    uint8_t psk_len;
    char ssid[WIFI_SSID_MAX_LEN + 1];
    char psk[65];
};
```

只在 `NET_EVENT_WIFI_CONNECT_RESULT` 且 `status == 0` 时写，并且与内存里的 `g_cred` 比较，避免重复擦写 flash。

---

## 8. 已知取舍

- AP 的 SSID/PSK 是编译期常量（`S3-Portal` / `<REDACTED-PSK>`），要改就改 `src\portal_cfg.h` 的 `AP_SSID` / `AP_PSK`。
- 扫描时 AP 会短暂抖动（Wi-Fi 单射频分时），属正常。
- 连接成功但拿不到 DHCP 租约时，状态停在 `CONNECTING` 直到超时。
