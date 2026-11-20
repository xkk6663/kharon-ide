# s3_wifi_portal 应用层分层改造方案

> 状态：**已执行（2026-11-16）**，编译通过　·　对象：`applications/esp32s3/s3_wifi_portal`
> 本文是"当初为什么这么定"的方案记录；**落地后的实际形态、复现步骤、实测数字见 [`应用层分层改造教程-s3_wifi_portal.md`](应用层分层改造教程-s3_wifi_portal.md)**。
> 原文以 `[待实测]` 标注的三处已在教程 §9/§10 回填（编译耗时对照、镜像体积、真机自检仍待验）。
> 与实测不符的两处已改正：`main.c` 实为 **4399 行 / 161192 字节 / 纯 LF**（非 4378 行 / CRLF）；`main()` 在 **4145–4264 行**。
> ⚠️ **本文 §3.1 建议的 `zephyr_library_named`（STATIC 库）在实施中出过事故**：自建静态库不在
> `--whole-archive` 组内，导致 HTTP 资源、页面数据、shell 线程被静默裁掉（`bin` 837124 B 的镜像缺功能）。
> 现状是改用 **OBJECT 库**，`bin` = 868916 B。复盘见教程 **§11**，读本文时请对照那一条，
> 不要把 §3.1 的写法原样抄到新工程。

---

## 0. 结论摘要

> **已定决策（2026-11-16 对齐）**：模块粒度 **15 个**（不合并）；**接受**"扫描缓存由调用方拿锁"；**先平铺 `src/` 做完整 S11**，目录按层收拢放到**单独一轮 S12**，两者 diff 不混。详见 §10。

1. **该拆，但只拆"文件+target"，不引入 Kconfig、不改 `prj.conf`。** 目标是把 1 个 4378 行的编译单元拆成 **4 个 target / 15 个 .c**，依赖方向变成单向三层。
2. **Zephyr 的三件工具里，本工程只用得上两件：`zephyr_library()`、iterable section。** `SYS_INIT` 在本工程**刻意不用**——`main()` 里存在硬件就绪顺序依赖（`k_msleep(2000)` 等 Wi-Fi 驱动、HTTP 起来后才自检），把 `start_dns_hijack()` 之类搬到 `SYS_INIT(APPLICATION)` 会在 AP 未启动时 `bind(udp/53)`，是把能跑的代码改坏。理由见 §3.2。
3. **真正的收益不是"文件变小"，而是三处状态所有权变清楚**：`g_creds` 现在被 4 个层直接改（凭据内部 46 处、HTTP 9 处、串口 10 处、main 3 处），`g_state` 在 6 个层被读写（30 处），`ap_iface/sta_iface` 被 6 个层用（26 处）。改造后三者都只留一个写入口。
4. **不涉及"跨项目组件"。** 这套分层是这个 app 自己的骨架，不进 `elab-app-common`（理由见 §9）。

---

## 1. 现状测绘

### 1.1 规模

| 文件 | 行数 | 字节 | 说明 |
|---|---|---|---|
| `src/main.c` | **4378**（非空 3666） | 164925 | 单体应用 |
| `src/ota.c` | 1143 | 44554 | 已独立 |
| `src/ops.c` | 556 | 19638 | 已独立 |
| `src/pages.h` | 269 | 10457 | `tools/gen_pages.py` 生成 |
| `src/user_page.html` | 234 | 8543 | 生成源 |
| `prj.conf` | 208 | 12488 | **改造中不动** |

> 口径说明：`Get-Content` 数出 4378 行（含空行），`Measure-Object -Line` 数出 3666（只数非空）。之前口头说的 3664/3665 是后者，以 **4378** 为准。
> 编码：全部 **UTF-8 无 BOM**（`ota.h` 带 BOM，其它无），行尾 **CRLF**。新文件必须保持这个组合，否则 diff 会整文件翻红。

### 1.2 构建现实

| 指标 | 实测值 |
|---|---|
| 全工程 `.obj` | **617** |
| `app` 目标编译单元 | **4**（`main.c` `ota.c` `ops.c` `breath_sync.c`） |
| 镜像 `zephyr.bin` | 753 KB（11-15 11:57 产物；`main.c` 已比它新，下次构建必然重编） |
| 链接方式 | `-Wl,--whole-archive app/libapp.a zephyr/libzephyr.a …（34 个 .a）… -Wl,--no-whole-archive` |

最后一行是本次调研**最关键的一条**：Zephyr 把应用库和系统库一起整库链接，**静态库里没有符号被引用的对象也会被收进镜像**。这直接决定 §3.3 的"自注册"方案成立与否。

### 1.3 main.c 里已经长出来的 15 个"隐性地层"

这些层现在靠 `static` 函数名和注释分区"假装"存在，实际共享同一块文件作用域。

| 层 | 行号区间 | 代表符号 | 估算行数 |
|---|---|---|---|
| 头/宏/状态 | 1–203 | `enum link_state`、`g_*` 40 个、`AP_IP` 等宏 | 203 |
| 凭据 | 163–594 | `cred_index/upsert/remove/apply/advance/pick_default/migrate`、`SETTINGS_STATIC_HANDLER_DEFINE` | 432 |
| 扫描 | 595–668 | `g_aps`、`ap_lock`、`scan_cache_*`、`scan_start` | 74 |
| 工作项定义 | 669–698 | `conn_lock`、`connect_work`、`conn_timeout_work`、`ap_reopen_work`、`connect_defer_work` | 30 |
| 链路状态机 | 726–1014 | `portal_disconnect`、`schedule_runtime_retry`、`reason_text`、`connect_work_handler`、`retry_work_handler` | 289 |
| AP 客户端 | 1015–1102 | `ap_client_add/remove`、`g_ap_clients` | 88 |
| 时间/SNTP | 1067–1340 | `sntp_thread`、`clock_hms`、`ymd_to_epoch`、`apply_http_date` | 275 |
| 出网自检 | 1341–1500 | `dns_resolve_host`、`netcheck_thread`、`start_net_services`、`ensure_default_route` | 160 |
| 事件分派 | 1503–1707 | `ipv4_event_handler`、`wifi_event_handler` | 205 |
| 页面字符串 | 1709–1960 | `index_html` | 252 |
| HTTP 基础 | 1963–2193 | `json_escape`、`wifi_rssi`、`client_side_ap`、`auth_pass`、`AUTH_GUARD/DEV_GUARD` | 175 |
| HTTP 接口 | 2195–3584 | 19 个 handler + 19 个 `*_detail` + `HTTP_RESOURCE_DEFINE` | ~1390 |
| 呼吸灯 | 3619–3717 | `led_thread` | 100 |
| SoftAP | 3719–3782 | `enable_softap`、`ap_reopen_handler` | 64 |
| 自检/DNS | 3784–4123 | `http_self_test*`、`dns_hijack_thread` | 340 |
| 编排/串口 | 4125–4378 | `main`、`cmd_portal_*` | 254 |

### 1.4 共享状态耦合矩阵（脚本统计，出现次数）

用 15 个行号区间 × 29 个全局符号统计出现位置，关键几行：

| 符号 | 总计 | 分布（凭据/扫描/链路/时间/自检/事件/HTTP基础/HTTP接口/SoftAP/串口…） | 问题 |
|---|---|---|---|
| `g_creds` | **70** | 凭据 46 · HTTP接口 9 · 串口 10 · main 3 · 类型 2 | 4 个层直接改同一张表 |
| `g_state` | **30** | 链路 13 · HTTP基础 8 · 事件 3 · HTTP接口 2 · LED 2 · 串口 1 · 类型 1 | 6 个层读写 |
| `g_ssid` | **38** | 链路 10 · 事件 8 · 凭据 8 · HTTP基础 3 · HTTP接口 2 · 串口 3 | 目标名/实际名混用 |
| `ap_iface`+`sta_iface` | **26** | 类型 2 · 链路 5 · 事件 4 · HTTP基础 2 · SoftAP 5 · main 3 | 句柄随处取 |
| `g_msg` | **26** | 链路 13 · 事件 4 · HTTP基础 4 · main 1 · 串口 1 | 状态文案到处写 |
| `g_ap_params` | **15** | SoftAP 13 · 扫描 1 · 类型 1 | AP 参数被链路间接改信道 |
| `g_ap_clients` | **19** | 链路 11 · HTTP接口 7 | AP 表挂在链路层 |
| `g_breath_clk` | **6** | 时间 2 · LED 2 · 链路 1 · main 1 | 呼吸时钟被 SNTP 直接喂 |

结论：**耦合不是"文件太大"，是"没有一个符号只有一个人能写"。** 所以 §2 的设计重点不是切文件，而是给每个符号定一个所有者 + 一个写入口。

### 1.5 注册锚点（搬迁时必须原样保留的东西）

| 类型 | 位置 |
|---|---|
| `LOG_MODULE_REGISTER(portal, …)` | 54（**唯一**，拆文件后每文件各一个，名字必须唯一） |
| `K_WORK[_DELAYABLE]_DEFINE` | 201 `retry_work`、682 `connect_work`、684 `conn_timeout_work`、691 `ap_reopen_work`、694 `connect_defer_work`、2017 `rssi_sample_work` |
| `K_THREAD_STACK_DEFINE` | 1126 `sntp_stack`、1362 `netcheck_stack`、4044 `dns_stack` |
| `K_THREAD_DEFINE`（自启动） | 3484 `sh_cmd_tid`、3716 `led_tid` |
| `K_SEM_DEFINE` | 3415 `sh_req`、3416 `sh_done` |
| `K_MUTEX_DEFINE` | 608 `ap_lock`、677 `conn_lock` |
| `SETTINGS_STATIC_HANDLER_DEFINE(portal_cred, "portal", …)` | 421 |
| `HTTP_SERVICE_DEFINE(portal_service …)` | 3588 |
| `HTTP_RESOURCE_DEFINE(…)` | 3590–3616（19 条） |
| `SHELL_CMD_REGISTER(portal …)` | 4378 |
| `net_mgmt_init_event_callback` | 4137、4147 |

---

## 2. 目标架构

### 2.1 四层模型

```
L3 编排层   main.c                     事件分派 + 启动顺序（唯一知道"什么时候"的地方）
                    │
L2 服务层   cred / wifi_scan / wifi_link / portal_ap / time_sync / netcheck / led_status / dns_hijack
                    │                   （机制：状态机、重试、持久化、协议）
L1 接口层   http_core / api_wifi / api_dev / page / shell_cmds / selftest
                    │                   （表达：JSON 字段、页面、串口文案）
L0 引擎层   ota.c / ops.c / breath_sync.c（已有，本次不动）
                    │
              Zephyr API
```

**依赖规则（硬约束）**

- 只允许 `L3 → L2 → L1`？**不。** 正确的是：`L3` 可调 `L1/L2`，`L1` 可调 `L2/L0`，`L2` 可调 `L0`，`L0` 只调 Zephyr。
- `L1` **不允许**横向互相依赖（`api_wifi` 不 include `api_dev` 的头）。`http_core.h` 是 `L1` 的公共底座，允许被其它 `L1` include。
- `L2` **不允许** include 任何 `L1` 的头。这条是本次改造最想固化的边界。

### 2.2 模块清单（15 个 .c）

| # | 文件 | 层 | 职责 | 搬移来源（行号） | 估算行数 | 对外 API 要点 |
|---|---|---|---|---|---|---|
| 0 | `portal_cfg.h` | — | 共享常量：`AP_SSID/AP_PSK/AP_IP/AP_POOL`、`CONNECT_TIMEOUT_MS`、`AUTO_RETRY_*`、`RT_RETRY_*`、`CONN_STABLE_MS` | 57–73、1122、1346、1371、3919 | 40 | 纯宏，无 .c |
| 1 | `cred.c/h` | L2 | 凭据表持化 + 轮换 | 163–594 | 432 | `cred_count/cred_at`、`upsert`、`remove`、`apply`、`advance`、`migrate_legacy`、游标读写 |
| 2 | `wifi_scan.c/h` | L2 | 扫描缓存 | 595–609、610–668 | 75 | `scan_start`、`scan_is_active`、`scan_lock/unlock`、`scan_entries/count`、`scan_event_*` |
| 3 | `wifi_link.c/h` | L2 | STA 链路状态机（**`g_state` 唯一写者**） | 84–109、151–153、187–202、669–698、726–1014、2013–2049 | 400 | `link_view_get`、`link_request_connect`、`link_disconnect`、`link_ev_*`、`link_rssi_hist_json` |
| 4 | `portal_ap.c/h` | L2 | SoftAP + AP 客户端表 + 信道对齐 | 111–143、699–725、1015–1102、3719–3782 | 215 | `ap_enable`、`ap_restart_channel`、`ap_current_channel`、`ap_ev_client_*`、`ap_client_count/at` |
| 5 | `time_sync.c/h` | L2 | SNTP 对时 | 1067–1340 | 275 | `time_start`、`time_format_hms`、`time_get_status` |
| 6 | `netcheck.c/h` | L2 | 出网自检 + 默认路由 + 起对时/探测线程 | 1341–1500 | 160 | `net_svc_start`（幂等）、`netcheck_view_get` |
| 7 | `led_status.c/h` | L2 | 呼吸灯线程 + 呼吸时钟 | 3619–3717 | 100 | `led_clock_start`、`led_clock_sync`（被 `time_sync` 调） |
| 8 | `dns_hijack.c/h` | L2 | captive DNS 应答器 | 3913–4123 | 210 | `dns_hijack_start`、`dns_hijack_selftest` |
| 9 | `selftest.c/h` | L1 | 上电自检（HTTP / scope 双验） | 3784–3911 | 130 | `selftest_http`、`selftest_scope` |
| 10 | `http_core.c/h` | L1 | 鉴权、请求来源判定、JSON 转义、公共响应头、**服务定义** | 1963–1966、1969–1985、1993–2011、2059–2193、3586–3588 | 175 | `auth_pass`、`client_side_ap`、`json_escape`、`url_decode`、`parse_form_field`、`AUTH_GUARD/DEV_GUARD`、`json_header` |
| 11 | `api_wifi.c` | L1 | 用户侧 9 个接口 + 各自 `*_detail` + 注册 | 2195–2874 | 680 | 无对外 API（自注册） |
| 12 | `api_dev.c` | L1 | 开发者侧 8 个接口（含 :80 远程 shell）+ 注册 | 3072–3584 | 515 | 同上 |
| 13 | `page.c` | L1 | 三张页面 + captive 通配 + 页头常量 | 1709–1960、2876–2981、3027–3070 | 400 | 同上 |
| 14 | `shell_cmds.c` | L1 | 串口 `portal` 命令组 | 4241–4378 | 140 | 无（`SHELL_CMD_REGISTER`） |
| 15 | `main.c` | L3 | 事件分派 + 启动编排 + 模块接线 | 1–75、1503–1707、4125–4239 | ~430 | `main()` |

合计约 **4300 行**（含搬移时新增的头文件、getter 内联）。

### 2.3 CMake：4 个 target

```cmake
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(s3_wifi_portal)

set(KORVO_COMMON ${CMAKE_CURRENT_SOURCE_DIR}/../../common)

# ---- L0 引擎层：不认识任何上层 ----
zephyr_library_named(portal_eng)
zephyr_library_sources(
  src/ota.c  src/ops.c
  ${KORVO_COMMON}/src/breath_sync.c)
zephyr_library_include_directories(src ${KORVO_COMMON}/include)

# ---- L2 服务层：可调 L0，不可调 L1 ----
zephyr_library_named(portal_svc)
zephyr_library_sources(
  src/cred.c  src/wifi_scan.c  src/wifi_link.c  src/portal_ap.c
  src/time_sync.c  src/netcheck.c  src/led_status.c  src/dns_hijack.c)
zephyr_library_include_directories(src)
zephyr_library_link_libraries(portal_svc portal_eng)

# ---- L1 接口层：可调 L2/L0 ----
zephyr_library_named(portal_api)
zephyr_library_sources(
  src/http_core.c  src/api_wifi.c  src/api_dev.c  src/page.c
  src/shell_cmds.c  src/selftest.c)
zephyr_library_include_directories(src)
zephyr_library_link_libraries(portal_api portal_svc)

# ---- L3 编排：只剩 main.c ----
target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE src ${KORVO_COMMON}/include)
target_link_libraries(app PRIVATE portal_api)

zephyr_linker_sources(SECTIONS ${CMAKE_CURRENT_SOURCE_DIR}/sections-rom.ld)
```

**`zephyr_library_link_libraries` 实测是 `PUBLIC`**（`extensions.cmake:791`），所以 `app → portal_api → portal_svc → portal_eng` 的传递依赖自动成立，不必逐层重写。

### 2.4 关键头文件草案

`wifi_link.h`（整份改造的重心：一个结构体替代 12 个全局）

```c
struct link_view {
    int         state;         /* LINK_IDLE / LINK_CONNECTING / LINK_CONNECTED */
    const char *target_ssid;   /* g_ssid   —— 本次点击的目标 */
    const char *link_ssid;     /* g_link_ssid —— 真的连上的那条 */
    const char *ip;
    const char *gw;
    const char *msg;
    const char *last_err;
    int         rssi;          /* 断开恒为 0，与 wifi_rssi() 口径一致 */
    uint32_t    reconnects;
    uint32_t    connected_at;
};
void link_view_get(struct link_view *v);   /* 一次性取走整份快照 */

bool link_is_connected(void);
int  link_state(void);
void link_ev_connect_result(int status);
void link_ev_disconnect_result(const struct wifi_status *st);
void link_ev_got_ip(struct net_if *iface, const char *ip);
void link_request_connect(const char *ssid, size_t slen, const char *psk, size_t plen);
int  link_disconnect(void);
void link_boot_autoconnect(void);
void link_rssi_sampling_start(void);
void link_rssi_hist_json(char *out, size_t len);
```

`cred.h`（把"直接读表"变成"问模块"）

```c
uint8_t cred_count(void);
const struct cred_item *cred_at(int idx);   /* NULL = 越界，调用方不再自己判 count */
int  cred_index(const char *ssid, size_t len);
int  cred_upsert(const char *ssid, size_t slen, const char *psk, size_t plen);
bool cred_remove(const char *ssid, size_t len);
void cred_apply(uint8_t idx);
bool cred_advance(void);
void cred_pick_default(void);
void cred_migrate_legacy(void);
void cred_mark_last_good(const char *ssid, size_t len);
void    cred_cursor_reset(uint8_t idx);
uint8_t cred_cursor(void);
uint8_t cred_rounds(void);
void    cred_rounds_reset(void);
```

`wifi_scan.h`

```c
int  scan_start(void);
bool scan_is_active(void);              /* 取代 3 处 g_scanning && uptime < deadline */
void scan_event_result(const struct wifi_scan_result *r);
void scan_event_done(void);
void scan_lock(void);   void scan_unlock(void);
const struct ap_entry *scan_entries(void);
int  scan_count(void);
```

`portal_ap.h`

```c
void    ap_enable(void);
int     ap_restart_channel(uint8_t ch);    /* 链路要换信道时唯一的入口 */
uint8_t ap_current_channel(void);
uint8_t ap_channel_of_ssid(const char *ssid);
void    ap_ev_enabled(void);
void    ap_ev_client_add(const struct wifi_ap_sta_info *info);
void    ap_ev_client_remove(const struct wifi_ap_sta_info *info);
int     ap_client_count(void);
const struct ap_client *ap_client_at(int i);
```

`http_core.h`（注意：宏依赖**形参名**，这是隐性契约，必须写死）

```c
extern const struct http_header json_header[];
bool   client_side_ap(struct http_client_ctx *client);
bool   auth_pass(struct http_client_ctx *client,
                 const struct http_request_ctx *req,
                 struct http_response_ctx *resp);
size_t json_escape(char *dst, size_t dst_len, const char *src);
void   url_decode(char *s);
bool   parse_form_field(const char *body, const char *key,
                        char *dst, size_t dst_len, size_t *dst_len_out);

/* 这两个宏要求所在函数的三个形参名必须叫 client / request_ctx / response_ctx */
#define AUTH_GUARD() ...
#define DEV_GUARD()  ...
```

其余 `time_sync.h` / `netcheck.h` / `led_status.h` / `dns_hijack.h` / `selftest.h` 各 1–6 个函数，按同规则（谁拥有状态谁出 getter）。

### 2.5 三处"隐性契约"（改坏了不报错、只跑飞）

| 契约 | 后果 | 处理 |
|---|---|---|
| `AUTH_GUARD()` 里的 `client/request_ctx/response_ctx` 形参名 | 改名即编译期报"未声明"，但若同名遮蔽会静默用错对象 | 宏放 `http_core.h`，并在注释里写死形参名 |
| `HTTP_RESOURCE_DEFINE(_name, portal_service, …)` 的 `portal_service` | **只是拼段名用，不引用变量**（`service.h:59–64` 实测展开）。拼错 → 链接期 `undefined reference to _http_resource_desc_portal_service_list_start` | 资源可在任意 .c 注册，但 token 必须一字不差 |
| `LOG_MODULE_REGISTER` 的模块名 | 两个文件用同名会 duplicate symbol | 每个 .c 一个独立名：`portal_cred / portal_scan / portal_link / portal_ap / portal_time / portal_netchk / portal_led / portal_dns / portal_http / portal_api_wifi / portal_api_dev / portal_page / portal_shell / portal_selftest` |

---

## 3. Zephyr 三件工具在本工程里到底起什么作用

### 3.1 `zephyr_library()` —— 把"拆文件"升级成"拆 target"

**它做的三件事**（`extensions.cmake:646/682/769/779/790`）：

1. `add_library(<name> STATIC)`：在 app 内部造一个**真实 CMake target**，产物是 `libportal_svc.a` 这样的独立静态库。
2. `zephyr_library_include_directories()` 是 **`PRIVATE`**（`:780`）：头文件搜索路径只对**本库**生效。于是"这个库能看见哪些头"变成了构建参数，而不是自觉。
3. `zephyr_library_link_libraries()` 是 **`PUBLIC`**（`:791`）：依赖关系写进 CMake，链接顺序由构建系统推。

**和现在的 `target_sources(app PRIVATE …)` 的本质差别**

| | 现在（单 target） | 改后（多 target） |
|---|---|---|
| 谁是"一个编译单元" | 4 个 .c，都属 `app` | 15 个 .c，分属 4 个 target |
| 跨文件可见性 | 同 target 内互相可见，**任何反向调用都合法** | 只通过 include 暴露，**反向 include 编不过** |
| 依赖关系在哪 | 只存在代码里 | 代码 + `CMakeLists.txt` 各一份 |
| 加一条反向依赖的成本 | 写一行 `#include` | 要改 CMake，评审必然看见 |

**为什么不会"每次全量编译"**：搬移的文件在新路径下 ccache 必然 miss（预处理输出里 `#line` 的绝对路径变了），但**未搬移的 600+ 个 obj 命令一字不变，ninja 直接 no-op**。一次性成本 ≈ 15 个文件重编 + 一次 re-configure + 一次重链接。日常改一个文件仍是"编 1 个 + 链接"，而由于文件从 4378 行降到几百行，单个编译更快、并行度从 4 升到 15。`[待实测]` 改造前后各跑一次 no-op / 改一 .c 对照。

### 3.2 `SYS_INIT()` —— 初始化分层，但本工程**不用**

**作用**：`SYS_INIT(fn, level, prio)` 把"某层初始化"从 `main()` 里的手写调用链，变成模块自己声明的静态注册。四个级别 `PRE_KERNEL_1 → PRE_KERNEL_2 → POST_KERNEL → APPLICATION`。

**实测执行位置**（`kernel/init.c:264–342`）：`bg_thread_main()` 先跑 `APPLICATION` 级的全部 `SYS_INIT`，**然后才调 `main()`**。

**为什么这里不用**：本工程 `main()` 的顺序不是"依赖图"，是"硬件就绪时间线"：

```
breath_clock_init_freerun()
net_mgmt_add_event_callback(×2)
ap_iface / sta_iface 取句柄并判空      ← 失败必须 return，SYS_INIT 没有这个退出路径
k_msleep(2000)                         ← 等 Wi-Fi 驱动
enable_softap()
start_dns_hijack()                     ← 必须在 AP 起来后 bind(udp/53)
http_server_start()
k_msleep(500) → 6 条 http_self_test → k_msleep 间隔 → dns_self_test
settings_subsys_init / settings_load   ← 必须在自检之后
ops_boot_init()                        ← 必须读 settings 里的 dirty 标志
cred_migrate_legacy()
ota_srv_start()                        ← 必须读回上次 nonce
boot 自动重连 → rssi 采样起排
```

把其中任何一段搬到 `SYS_INIT(APPLICATION)`，都会在 `main()` **之前**执行：`bind 53` 会失败、`settings_load` 会提前、自检会打到还没起来的 HTTP。**这是把能跑的代码改坏**，不是分层。

**结论**：`SYS_INIT` 在这个工程里的正确用法是"纯静态注册、无时序依赖"的东西——而这类东西已经被 iterable section 覆盖了（见 §3.3）。因此本次**不引入 `SYS_INIT`**，并在教程里作为一个反例讲清楚：**分层不等于把 `main()` 掏空**，`main()` 是唯一该知道"先后"的地方。

### 3.3 iterable section —— 声明式注册，main 不必知道谁在

**机制**：`STRUCT_SECTION_ITERABLE` 把对象放进一个命名段，段首尾由链接脚本生成 `_<section>_list_start/_end`，遍历用 `STRUCT_SECTION_FOREACH`。

**本工程已经在用的三处**（这就是为什么 `main.c` 会从 4378 → ~430 行）：

| 用法 | 展开 | 谁遍历 |
|---|---|---|
| `HTTP_RESOURCE_DEFINE(portal_status, portal_service, "/api/status", &status_detail)` | `const struct http_resource_desc http_resource_desc_portal_service_portal_status` 放进 `http_resource_desc_portal_service` 段 | `subsys/net/http` 启动时按段遍历 |
| `SHELL_CMD_REGISTER(portal, &portal_cmds, …)` | 放进 `shell` 段 | shell 子系统 |
| `K_THREAD_DEFINE(led_tid, 4096, led_thread, …)` | 放进线程段，内核自启 | 内核 |
| `SETTINGS_STATIC_HANDLER_DEFINE(portal_cred, "portal", …)` | 放进 settings 段 | `settings_subsys_init` |

**成立的前提是 §1.2 那条实测**：`--whole-archive` 收了 34 个 `.a`。否则 `portal_api.a` 里没人引用的 HTTP 资源对象会被链接器丢掉，端口全 404——这是最容易"改完能编译、跑起来全错"的坑。

**因此改造后 `main.c` 里 HTTP 相关行数 = 0**：服务定义在 `http_core.c`，19 个资源分散在 `api_wifi.c / api_dev.c / page.c` 里各自的 handler 旁边，`main()` 只调一次 `http_server_start()`。这是"接口层完全不需要被编排层知道"的实证。

**代价**：注册是"隐式"的。排查"某个 URL 为什么 404"时，不能靠 `grep main.c`，要靠 `grep HTTP_RESOURCE_DEFINE`。这是拿"main 简洁"换"索引分散"，必须接受，并在文档里写清。

---

## 4. 改造前 vs 改造后的依赖区别（逐条可验证）

| 维度 | 改造前 | 改造后 | 怎么验 |
|---|---|---|---|
| 编译单元 | 4（全在 `app`） | 17（`portal_eng` 3 + `portal_svc` 8 + `portal_api` 5 + `app` 1） | `build.ninja` 里 `CMakeFiles/*.obj` 分组 |
| 跨文件可见性 | 同 target 全可见，反向调用合法 | 由 include 决定，反向 include **编不过** | 故意在 `cred.c` 里 `#include "api_wifi.h"` → 报错即边界生效 |
| 状态所有权 | 40 个文件级 `g_*`，8 个函数写 `g_state` | 每模块 `static`，`= LINK_` 只出现在 `wifi_link.c` | `grep -c "= LINK_" src/*.c` |
| `main.c` | 4378 行 / 66 个函数 | ~430 行 / `main` + 2 个事件回调 | `wc -l` |
| HTTP 与启动 | `main.c` 内 19 行 `HTTP_RESOURCE_DEFINE` | `grep portal_service src/main.c` = 0 | `grep` |
| 依赖方向 | 无约束（双向都通） | 单向三层，写进 CMake | `CMakeLists.txt` + 反向 include 报错 |
| 取数路径（例：`/api/status`） | handler 直接读 `g_state/g_ssid/g_ip/g_msg/g_last_err/wifi_rssi()/g_netcheck_*/g_dns_*/g_sntp_*/g_reconnects`（10 个全局 + 2 个函数） | `struct link_view v; link_view_get(&v);` + `netcheck_view_get(&n)` + `time_get_status(&t)`（3 次调用） | 对比 handler 函数体 |
| LED 配色 | 直接读 `g_state` | `link_state()` | `grep g_state src/led_status.c` = 0 |
| 凭据读取 | `g_creds.count` / `g_creds.items[i].psk` 直接访问（HTTP 9 处、串口 10 处） | `cred_count()` / `cred_at(i)` | `grep "g_creds" src/api_*.c src/shell_cmds.c` = 0 |

**一处必须接受的"没变"**：`ap_lock` 保护的 `g_aps` 会被 `api_wifi.c` 遍历。改造后它变成 `scan_lock(); scan_entries(); scan_count(); scan_unlock();`——**锁还是要调用方拿**。要彻底消除，得把 JSON 生成挪进 `wifi_scan`（让模块吐出字符串），那会让 L2 知道"JSON 字段名"这种表达层的事，违反分层。**这里刻意选择"暴露锁"而不是"暴露 JSON"**，是本方案唯一一处为了分层而保留的不优雅，写在这里备查。

---

## 5. 分步迁移计划（12 步：S0–S11）

原则：**每步只搬"当前依赖已就绪"的模块**；每步结束必须 `west build` 通过。**编译是我能验证的上限**——涉及链路/网络行为的步骤（S7/S8/S10/S11）需要你在真机上做一次 §8 的自检，烧录由你手动执行，我不碰。任一步失败，只回滚这一步。

| 步 | 动作 | 产出 | 验证 | 预估 diff |
|---|---|---|---|---|
| **S0** | 测基线：no-op、改一 `.c` 增量、`zephyr.bin` 体积 | 3 个数字 | `tools\build.ps1` 计时 | 0 |
| **S1** | 新建 `portal_cfg.h`，把 57–73 等共享宏搬进去；`main.c` 改为 include 它 | 1 header | 编译通过、行为不变 | +40 / −20 |
| **S2** | `main.c` 内先加"取数接口"：`link_view_get`、`cred_at`、`scan_*`、`ap_client_*` 等，**全部先用 static 实现包住原全局**，调用方逐个改为走 getter | 调用点变更 | 编译通过；`g_*` 引用数应显著下降 | ~200 行改写 |
| **S3** | 建 CMake 骨架：`portal_eng` 收录 `ota.c/ops.c/breath_sync.c`，`app` 只留 `main.c` | CMake | 编译通过（此时搬迁 0 个业务文件） | +25 / −5 |
| **S4** | 搬 `cred.c/h`（163–594），`g_creds` 等改 `static`，getter/setter 落地 | 1 文件 | 编译；`portal saved` 仍列出网络 | 移 432 行 |
| **S5** | 搬 `wifi_scan.c/h`（595–668），`ap_lock` 内部化 | 1 文件 | 编译；`/api/scan/start` 仍能扫到 | 移 75 行 |
| **S6** | 搬 `portal_ap.c/h`（含 AP 客户端表、信道） | 1 文件 | 编译；AP 起来后 DHCP 仍给地址 | 移 215 行 |
| **S7** | 搬 `wifi_link.c/h`（最大一步：状态块 + 状态机 + works + RSSI 采样），事件逻辑下沉为 `link_ev_*` | 1 文件 | 编译；连接/掉线/重连日志与改前一致 | 移 ~400 行 |
| **S8** | 搬 `time_sync.c/h` + `netcheck.c/h` + `led_status.c/h` + `dns_hijack.c/h` | 4 文件 | 编译；SNTP/netcheck/DNS/LED 日志仍在 | 移 ~745 行 |
| **S9** | 搬 `http_core.c/h` + `selftest.c/h`；`json_header` 等改 `extern` | 2 文件 | 编译；401/404 语义与改前一致 | 移 ~305 行 |
| **S10** | 搬 `api_wifi.c` → `api_dev.c` → `page.c`，资源注册分散到各自文件；`main.c` 删除全部 `HTTP_RESOURCE_DEFINE` | 3 文件 | 编译；19 个 URL 逐个 `http_self_test` | 移 ~1595 行 |
| **S11** | 搬 `shell_cmds.c`；收尾：`main.c` 只剩事件分派 + 编排；重跑 S0 同一套测量 | 1 文件 + 文档 | 全套验收（§8） | 移 140 行 |

**关于 S12（另轮，不属于本次）**：S11 结束时 `src/` **保持平铺**（15 个 `.c` + 15 个 `.h` 同目录）。按层收拢目录（`src/core/`、`src/api/`、`src/ui/`）作为**单独一轮**执行：前提是 S11 编译通过且 §8 验收结果已留存；那一轮**只允许"移动文件 + 改 include 路径"**，不许顺带改任何逻辑，这样 `git mv` 的 diff 可以整体复核。

**建议在 S7 之后先烧一版真机**——链路状态机是全工程最容易"编译过但行为变"的地方，早验证一次比全拆完再查便宜得多。（按你的习惯，烧录由你手动执行，我不碰。）

---

## 6. 风险清单

| # | 风险 | 后果 | 规避 |
|---|---|---|---|
| 1 | `LOG_MODULE_REGISTER` 重名 | 链接期 duplicate symbol | 每文件独立名（§2.5 表） |
| 2 | `AUTH_GUARD` 形参名不符 | 编译报错或静默用错对象 | 宏随 `http_core.h` 走，注释写死 |
| 3 | `portal_service` token 拼错 | `undefined reference to …_list_start` | 独占一份注释说明；构建报错时先查这里 |
| 4 | 库没被 `--whole-archive` 收（配置变化） | HTTP 全 404、LED 不亮、shell 无命令 | §8 用 `nm` 查 `portal_service`/`led_tid`/`portal_cmds` |
| 5 | `body_buf/forget_buf` 共享缓冲被误搬成两个 | 并发请求串味（历史上出现过 `AlphaTest?ssid=BetaTest2`） | 两把缓冲各自跟 handler 走，保持 static 且**同文件** |
| 6 | `body_len` 在 ABORTED 分支清零的逻辑被拆散 | 分片 body 残留 → 下一次请求解析错 | 搬移时连 `status` 判断三连一起搬 |
| 7 | `g_state` 出现第二个写者 | 状态互相覆盖，页面显示错乱 | `grep -c "= LINK_\(IDLE\|CONNECTING\|CONNECTED\)" src/*.c` 必须 = 1 文件 |
| 8 | `ensure_default_route` 依赖 `g_ip` | 搬进 netcheck 后取不到 IP | 改成参数 `const char *ip`，由 link 传 |
| 9 | `ota_confirm_if_healthy(g_state == CONNECTED)` | 传错布尔 → 新固件永不自证转正 | 改为 `link_is_connected()` |
| 10 | `breath_clock_*` 跨层 | LED 与 SNTP 互相依赖 | `time_sync → led_status` 单向；`g_breath_clk` static 在 LED |
| 11 | `settings` handler 被挪出 `cred.c` | 上电读不到凭据，自动重连失效 | handler 与表同文件；`SETTINGS_STATIC_HANDLER_DEFINE` 保持 |
| 12 | `K_THREAD_DEFINE` 放进库 | 若被裁剪则线程不启（LED 不亮） | 已确认整库链接；验收查 `led_tid` |
| 13 | 编码混用 | 中文注释变乱码 | 新文件 **UTF-8 无 BOM + CRLF**；`ota.h` 现有 BOM 不动 |
| 14 | 顺手"优化"逻辑 | 行为漂移，OTA 回滚判据失效 | **纯搬移**：只动声明/可见性，不动分支与常量 |
| 15 | 动 `prj.conf` | 一次全量重编 | 本次**零改动**；不加 `CONFIG_*`、不加 `*_ifdef` |
| 16 | `sections-rom.ld` 被误删 | 链接期段符号缺失 | 保持 `zephyr_linker_sources(SECTIONS …)` 原样 |
| 17 | `pages.h` 生成路径 | 改 `page.c` 后生成脚本失效 | 生成脚本与输出路径不动，仅 include 位置变 |
| 18 | 工作区已有 15 个未提交改动 | 出问题无法 diff 定位 | **不提交 git**；S0 先记 `git stash list` / `git status` 快照 |

---

## 7. 编译时间核算

| 场景 | 改前 | 改后 | 依据 |
|---|---|---|---|
| no-op 构建 | 1.64 s（实测，257 步工程口径） | 同量级 | ninja 全 no-op |
| 改一个 `.c` | 6.59 s（实测；其中大头是编 4378 行的 `main.c`） | 更低（编几百行的小文件 + 同一次链接） | 文件变小、并行度 4→15 |
| 改 `main.c` | 编 4378 行 | 编 ~430 行 | 行数比 |
| 一次性改造成本 | — | ≈ 15 个文件重编 + re-configure + 重链接 | `[待实测]`，预估 30–60 s |
| 全量 617 obj | 分钟级 | **不变** | 只由 `prj.conf`/overlay 触发 |
| 链接次数 | 3 次 | **不变** | 多几个 `.a` 只多几次 `ar` |

**唯一会让它变慢的做法**：给每个模块加 `CONFIG_APP_xxx` + `zephyr_library_sources_ifdef`。本次明确不做（理由同 §9）。

---

## 8. 验收清单

**构建**
- `west build` 零错误；新增 warning 数 = 0
- 编译单元数 = 17；`libportal_eng.a / libportal_svc.a / libportal_api.a / libapp.a` 四个产物存在

**符号**（`nm`/map 查，防止被裁剪）
- `portal_service`（HTTP 服务）、`portal_cmds`（shell 命令组）、`led_tid`（线程）、`sh_cmd_tid` 均在
- `link_` 前缀符号无重名冲突

**体积**
- `zephyr.bin` 变化 `< 2%`（`[待实测]`，预期 ±0，可能因不再跨函数内联而略变）

**功能（真机，你手动烧）**
- 串口 6 条自检日志齐全：`GET /api/wifi`、`scope user`、`/api/shell` 404、`/api/ota/slots` 404、`/api/drops` 404、`GET /api/saved`、`/generate_204` 302、`dns self-test`
- 19 个 URL 状态码与改前一致（AP 侧 404 / LAN 侧 200 / captive 302）
- 串口 `portal status / drop / saved / conn <ssid> [psk]` 四条命令输出与改前一致
- STA 连接、掉线重连、AP 客户端列表、RSSI 折线、OTA :8080 上传、`/api/ota/slots` 版本号
- LED 三色呼吸（含未授时半亮）

**回归底线**：改前 `git diff` 快照 vs 改后，**逻辑分支与常量零差异**，diff 只应出现"声明位置/可见性/调用点"。

---

## 9. 明确不做的事

1. **不把这套骨架放 `elab-app-common`**。`elab-app-common` 的准入是"不绑 SoC、≥2 消费者、不含策略"；`wifi_link` 绑 ESP32 的 APSTA 与信道行为，只有一个消费者，全是这个产品的策略 → 留在 app 内。
2. **不升级成 Zephyr module**。没有第二个仓库/第二个人要用，也没有 Kconfig 需求；现在做 module 只会把"改一行 CMake"变成"维护版本与 west.yml"。
3. **不引入 Kconfig 开关**（会让"改 prj.conf = 全量"变成常态）。
4. **不改 `prj.conf` / `overlay` / `sections-rom.ld` / `tools/gen_pages.py`**。
5. **本轮不做目录分层**（`src/` 保持平铺）；目录收拢见 §5 的 S12 说明与 §10 决策 ③。
6. **不动 `ota.c` / `ops.c` 的内部**，只把它们从 `app` 目标挪进 `portal_eng` 库。
7. **不提交 git**。

---

## 10. 决策记录（2026-11-16 已定）

三项已在方案层面对齐，**代码未动**。执行期间若实际情况推翻其中任一项，**先改本节、再改代码**，不要边做边改口径。

| # | 决策点 | 结论 | 理由 | 推翻条件 |
|---|---|---|---|---|
| ① | 模块粒度 | **15 个 `.c`，不合并**（否决 9 个的合并方案） | `api_wifi` 与 `api_dev` 是**两个受众**（用户配网页 vs 开发者调试台）、**两套鉴权**（AP 侧一律 404 / LAN 侧要 `k`）；合并后必然回到"一个文件里两拨人互相改"。拆分后最大文件 `api_wifi.c` ≈680 行，仍在可读范围 | S4–S8 搬移过程中若出现**双向调用**（违反 §2.1），就地合并该对模块，不许加中间接口绕 |
| ② | 扫描缓存的锁 | **接受"调用方拿锁"**：`scan_lock(); scan_entries(); scan_count(); scan_unlock();` | 替代方案是把"缓存 → JSON"下沉进 `wifi_scan`，代价是 L2 知道 `{"ssid":…,"rssi":…}` 字段名，直接违反 §2.1（L2 不含表达层知识）。**用一处不优雅换掉一条层级违规** | 若出现第三个调用方也要遍历缓存，改为 `scan_foreach(cb, ctx)`（锁收进模块内部） |
| ③ | 目录分层 | **先平铺 `src/` 做完 S11；目录收拢放单独一轮 S12** | 混在同一次 diff 里，出问题无法二分：是"搬代码错了"还是"改路径错了"。平铺版先建立可编译基线，S12 只做 `git mv` + include 修正，异常时一眼判定 | S11 验收（§8）未全绿则 S12 无限期推迟 |

**本次改造的交付边界**

- **交付**：4 个 target / 15 个 `.c` / `main.c` ≈430 行 / 单向三层依赖 / §8 验收清单全绿
- **不交付**：目录分层（S12 另轮）、Kconfig 开关、`elab-app-common` 沉淀、Zephyr module 化
- **不做**：任何 git 提交

---

## 附：这份方案是怎么得出的（可复现）

```powershell
# 编译单元 / 镜像
(Get-ChildItem build\esp32s3\s3_wifi_portal -Recurse -Filter *.obj).Count
# 链接是否整库
Select-String build\esp32s3\s3_wifi_portal\build.ninja -Pattern "whole-archive"
# main.c 结构
(Get-Content applications\esp32s3\s3_wifi_portal\src\main.c).Count          # 4378
# 符号耦合分布：按 15 个行号区间 × 29 个 g_* 统计（见 §1.4）
```

<!-- 调研完成于 2026-11-16；未修改任何 .c / CMakeLists / prj.conf。 -->
