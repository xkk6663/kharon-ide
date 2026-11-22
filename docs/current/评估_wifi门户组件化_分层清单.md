# 评估：把 s3_wifi_portal 的门户能力做成跨芯片组件

> 只读盘点，未改任何代码。目的：判断"配网门户"能不能抽成 Zephyr module，
> 以及要抽哪些、留哪些、改哪些。
>
> 盘点范围：`applications/esp32s3/s3_wifi_portal/src`（约 10.2k 行）。
> 结论只对"同 Zephyr、换芯片"成立；跨出 Zephyr（ESP-IDF/裸机）不在本评估内。

---

## 一、结论先说

| 判断 | 结论 |
|---|---|
| 应用层是否被 ESP32 绑死 | **基本没有**。全树只有 1 处 ESP32 私有头（`ops.c:19` 的 `esp_system.h`，用于重启） |
| Wi-Fi 操作是否走标准接口 | **是**。AP/连接/扫描全走 `net_mgmt`（`NET_REQUEST_WIFI_AP_ENABLE/_AP_DISABLE/_CONNECT/_DISCONNECT/_SCAN/_IFACE_STATUS`） |
| 网络服务是否自研 | **不是**。DHCP server、HTTP server、mDNS、SNTP、NVS 全是 Zephyr 子系统 |
| 真正的耦合在哪 | **不在 API，在"驱动能力假设"**：单射频同信道、扫描阻塞、状态字段不可信、AP 客户端不可查 |
| 能不能做 | **能**。可复用约 1.6k 行；需要 shim/策略层约 1.7k 行；产品私有约 3.3k 行 |

一句话：这套东西已经是"标准件组合"，抽组件的工作量集中在**把散在注释里的驱动脾气变成代码结构**。

---

## 二、文件级分层清单

归属代号：

- **core** = 与芯片无关的纯逻辑（状态机、模型、凭据）
- **net** = Zephyr 标准网络子系统（DHCP/DNS/HTTP/mDNS/SNTP）
- **ui** = REST 契约 + 页面资源
- **shim** = 唯一需要 per-chip 复写的部分（net_mgmt 封装 + 驱动差异）
- **app** = 本产品私有，不进组件

### L2 服务层

| 文件 | 行数 | 对外符号（摘要） | 归属 | 依据 / 迁移动作 |
|---|---|---|---|---|
| `wifi_link.c` | 1021 | `link_view_get` 快照、`link_request_connect`、`link_ev_*` 三个事件入口、`wifi_rssi`、`link_rssi_hist_json` | **core + shim + policy** | 状态机本体是 core；`link_request_connect` 里的 CONNECT 请求是 shim；`align_*`（信道对齐）是 ESP32 单射频 policy，必须抽成钩子 |
| `portal_ap.c` | 334 | `enable_softap`、`ap_retarget`、`ap_current_channel`、`ap_client_at`、`ap_ev_*` | **core + net + shim + policy** | AP 生命周期=core；DHCP server 配置=net；`AP_ENABLE/DISABLE`=shim；`ap_retarget`=policy |
| `wifi_scan.c` | 166 | `scan_start`、`scan_cache_add`、`scan_lock/unlock`、`scan_entries` | **core + shim** | 缓存模型=core；发起扫描=shim。**`scan_lock` 把锁暴露给上层**，组件应改成 `portal_scan_snapshot()` |
| `cred.c` | 382 | `cred_upsert/remove/at/count`、`cred_mark_last_good`、`cred_migrate_legacy` | **core**（`migrate_legacy` 例外=app） | 存储走 settings/NVS，已经是可替换后端；`cred_migrate_legacy` 是旧固件表迁移，产品私有 |
| `dns_hijack.c` | 222 | `start_dns_hijack`、`dns_self_test` | **net** | 纯 socket（`dns_hijack.c:26` 起），只依赖 `AP_IP` 一个常量，可整块搬 |
| `netcheck.c` | 236 | `netcheck_view_get`、`ensure_default_route`、`dns_resolve_host`、`start_net_services` | **net + policy** | 出网自检=net；`ensure_default_route` 与"双 iface 时默认口可能是 AP"是通用 policy，见 A6 |
| `time_sync.c` | 292 | `time_start`、`clock_hms`、`time_apply_http_date` | **net**（可选） | SNTP + HTTP Date 兜底，与芯片无关；但对不需要联网的产品是纯负担，应做成可选 |
| `led_status.c` | 225 | `led_clock_start`、`led_clock_observe`、`led_status_test` | **app**（或独立扩展 `portal-indicator`） | 颜色由 `link_state()` 决定，已依赖 `elab-app-common` 的 breath_sync |
| `led_ctrl.c` | 130 | `led_ctrl_init/poll/set/toggle` | **app** | 板级 KEY GPIO，硬件私有 |

### L1 接口层

| 文件 | 行数 | 对外符号（摘要） | 归属 | 依据 / 迁移动作 |
|---|---|---|---|---|
| `http_core.c` | 195 | HTTP service 定义、`auth_pass`、`client_side_ap`、`portal_json_escape`、`AUTH_GUARD/DEV_GUARD` 宏 | **net + core** | 服务注册和鉴权=net；`client_side_ap` 用网段判定（`http_core.c:60,78`）=core，网段必须来自 cfg 而非硬编码 |
| `api_wifi.c` | 734 | 10 个端点：`/api/scan`、`/api/scan/start`、`/api/wifi`、`/api/connect`、`/api/saved`、`/api/forget`、`/api/disconnect`、`/api/apclients`、`/api/rssi_hist`、`/api/status` | **ui** | 这是组件的公开契约面。`/api/status` 聚合了 OTA/ops 字段，需 app 侧注入 hooks（现有 `cloud_ops_bind` 已是这个模式） |
| `page.c` | 429 | 页面 HTML（C 字符串）+ 302 Location | **ui**（可裁） | 应分 `MINIMAL`（只表单）与 `FULL`（状态/RSSI 曲线/OTA）两档 |
| `api_dev.c` | 538 | `/api/drops`、`/api/boot`、`/api/ota/slots`、`/api/ota/revert`、`/api/pw`、`/api/shell`、`/api/wlog` | **app** | OTA/诊断控制台，产品私有 |
| `selftest.c` | 158 | `http_self_test`、`http_self_test_scope/host` | **app**（可选编译） | 开发期自检，组件里建议保留为 `PORTAL_SELFTEST` 开关 |
| `shell_cmds.c` | 253 | shell 命令注册 | **app** | 产品私有 |
| `pages.h` | 271 | 页面字符串常量 | **ui** | 与 `page.c` 同源，只有一份 |

### L0 / 编排

| 文件 | 行数 | 归属 | 说明 |
|---|---|---|---|
| `ota.c` / `ota_cloud.c` / `ops.c` / `cloud_ops.c` | 1455+1011+686+233 | **app** | OTA 与云端命令，全部产品私有（`ota_engine` 抽象已在 `elab-app-common`，不在本组件范围） |
| `main.c` | 257 | **app → 组件出口** | 现在它是"唯一知道顺序的地方"，组件化后应缩成 `portal_start(&cfg)` + 保留产品自有顺序（OTA、自检、灯） |
| `portal_cfg.h` | 49 | **组件 cfg 的雏形** | 现在全是编译期宏，见第四节 |

---

## 三、ESP32 假设清单（这是组件的核心工作量）

每一条都是"在当前驱动上实测出来的脾气"，换芯片必须能声明、能降级。

| # | 假设 | 证据 | 现象 | 抽象动作 |
|---|---|---|---|---|
| A1 | 单射频：AP 与 STA 必须同信道 | `portal_ap.c:30-33`、`portal_cfg.h:31-41` | 连别的信道的路由器 → `status 4(ASSOC_EXPIRE)`，用户看到"连什么都连不上" | policy 钩子 `before_connect(target_ch)`，允许"搬 AP 信道"这一动作 |
| A2 | `NET_REQUEST_WIFI_SCAN` 阻塞约 10 s | `wifi_link.c:181-186` | 放系统队列会冻住 LED/RSSI/重连 | 组件内部固定"独立队列 + 超时"，对外只暴露异步 request/事件 |
| A3 | `wifi_iface_status` 对 AP 返回**配置值**而非真实信道 | `portal_ap.c:37-39`、`:59-62` | 页面显示 0 或错值 | 状态结构加 `confidence` 字段，别假设驱动说的是真话 |
| A4 | 没有"查询 AP 客户端"的 net_mgmt 请求 | `portal_ap.c:43-47` | 只能靠事件自维护，无 RSSI | 能力位 `CAP_AP_CLIENT_LIST`，不支持就返回空表而不是报错 |
| A5 | DHCP 租约不对外暴露 | `portal_ap.c:45-46` | 拿不到客户端 IP | 同上：能力位 + 降级 |
| A6 | 双 iface 时默认 net_if 可能是 AP | `main.c:125-129` | SNTP 出不去（-ETIMEDOUT）、HTTP -EINVAL 且耗时接近 0 | policy：连接成功后显式 `net_if_set_default(sta)` |
| A7 | 驱动初始化需要约 2 s 才能开 AP | `main.c:173-174` | 早开 AP 失败 | shim 提供 `wait_ready()`，或事件驱动（AP 可用事件）替代硬 sleep |
| A8 | RGB 是 RMT/WS2812、按键是板级 GPIO | `led_ctrl.c`、`CONFIG_LED_STRIP` | — | 不进组件；由 `portal-indicator` 扩展或产品自己实现 |
| A9 | 重启靠 `esp_restart()` | `ops.c:19` | — | 平台后端 `portal_reboot()`，组件内只留弱符号 |

**注意 A1/A3/A6 都不是 API 问题，是策略问题**。组件如果只提供一个 `net_mgmt` 封装，换芯片后行为会悄悄跑偏——这是最容易低估的地方。

---

## 四、硬编码与"第二来源"清单

这些现在散在多处，组件化必须收敛到 `struct portal_cfg`（运行期注入），否则"随便拿个芯片就能用"不成立。

| 常量 | 定义处 | 第二来源（重复写死的地方） |
|---|---|---|
| `AP_SSID` = "S3-Portal" | `portal_cfg.h:14` | `main.c:73,239` 日志文案 |
| `AP_PSK` = "<REDACTED-PSK>" | `portal_cfg.h:15` | — |
| `AP_IP` = 192.168.4.1 | `portal_cfg.h:16` | `dns_hijack.c:48,200`、`http_core.c:78`、`page.c:45,386`、`pages.h:131`、`selftest.c:74/81`、`ota.c:40-41`（`OTA_AP_SIDE_IP`）、`user_page.html:125` — **共 8 处** |
| `AP_POOL` = 192.168.4.10 | `portal_cfg.h:18` | `portal_ap.c:196` |
| `AUTO_RETRY_MAX` = 3 | `portal_cfg.h:24` | `page.c:45` 的文案"最多 3 次" |
| 主机名 | `prj.conf CONFIG_NET_HOSTNAME` | `main.c:74,239`、`selftest.c:48` |

改一次网段要翻 8 个文件，这是组件化最实际的收益点之一（另一个是"别人 clone 就能改品牌"）。

---

## 五、建议的迁移顺序（每步都能验收）

| 步骤 | 内容 | 验收判据 | 估时 |
|---|---|---|---|
| S1 | 抽 `portal_wifi_ops{scan,connect,disconnect,ap_on,ap_off,status,clients,retarget,wait_ready}` + 能力位；ESP32 实现就是现有代码，行为不变 | `west build` 通过，产物与原版只有符号别名差异；`/api/status` 字段不变 | 3–5 天 |
| S2 | `portal_cfg.h` 常量 → `struct portal_cfg` 运行期注入（含品牌、网段、重试策略） | 改 SSID/网段只改一处；第二节的 8 处硬编码清零 | 1–2 天 |
| S3 | ui 分档：`PORTAL_UI_MINIMAL` / `FULL` | MINIMAL 版本 flash 占用有实测下降（记进 README） | 1 天 |
| S4 | native_sim + 假 Wi-Fi 驱动回归：把 core/net/ui 跑在无射频环境 | 无射频下 DNS 劫持 + 302 + 凭据落盘 + `/api/wifi` 全通 | 2–3 天 |

做完 S1–S4，第二颗芯片（工作区现成的 `esp32s31`/korvo1）接后端才有意义；顺序反了会返工。

---

## 六、已识别的四个风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | `wifi_link.c` 1021 行是最大单体，状态机同时挂着 RSSI 采样/退避/信道对齐三条定时器 | 抽 core 时容易把 policy 一起搬走，换芯片即失效 | S1 先只抽"出口"，定时器留在 policy 层 |
| R2 | `wifi_scan.c` 的锁是暴露给上层的（`scan_lock/unlock`） | 改成快照 API 会同时改 3 个 handler | S1 一起改，别留两套 |
| R3 | `http_core.h` 的 `AUTH_GUARD/DEV_GUARD` 依赖**形参名**（`client/request_ctx/response_ctx`） | 隐性契约，改名不报错只会静默用错对象 | 组件化时改成 inline 函数，编译期就报 |
| R4 | 与 `elab-app-common` 的边界 | `led_status.c` 已依赖其 `breath_sync`，容易形成反向依赖 | 定死方向：`elab-app-common` 只被产品层引用，组件不依赖它 |

---

## 七、和已有工作区路线的关系

- 组件落位建议 `elab-wifi-portal`（Zephyr module），与 `elab-app-common`、`kharon-boards` 同构，走同一套 `EXTRA_ZEPHYR_MODULES` / 清单注入。
- 这一步同时解决"别人 clone 就能上手、不依赖 IDE"的问题：module 自带 `zephyr/module.yml` + Kconfig 前缀 `PORTAL_*`，产品侧只写 3 行初始化。
- 但**不要和"模型 C 私有 module"路线同时开工**：那条线还在等远端仓地址（见 `待办_模型C工作区入库.md`）。组件可以先在本地 module 目录里长，等 `kharon-manifest` 的清单项启用后再挪成独立仓。

---

## 附：本评估的取证方式

全部结论来自只读阅读与 grep，未构建、未改文件：

- 符号来源：各 `*.h` 的公开声明；
- ESP32 假设：源码注释 + 实际行号（上表"证据"列）；
- 规模：`Get-ChildItem -Recurse` 的行数统计；
- 路由契约：`http_core.c:167-193` 的 `HTTP_RESOURCE_DEFINE` 列表。
