# 应用层分层改造（s3_wifi_portal）落地 — 2026-11-16

> 变更类型：**架构改造收口（OTA 抽象层迁移 + 目录收拢 + 自注册对象裁剪事故修复）**。
> 验证口径：编译 + ELF 段内符号计数 + 主机侧 45 条断言。**真机项未做**（设备连不上 WiFi，用户已说明先不验）。

## 背景

`docs\参考\应用层分层改造教程-s3_wifi_portal.md` §10 列了三件"还没做完的事"，本轮把它们做完：

1. 把 `ota_do_upgrade()`（381 行）迁到 `struct ota_backend`；平台无关部分进 `apps\common`；
2. 把平铺的 `src\*.c|*.h` 按层收拢到 `src\l0\ l1\ l2\`（只挪文件、不改内容）；
3. 核对 19 个 HTTP 资源是否都还在 —— 用户明确说明"设备连不上 WiFi，先不用验证相关功能"，
   因此改为**不需要设备**的静态验证。

## 决定与理由

| 项 | 决定 | 理由 |
|---|---|---|
| OTA 分层 | 协议层进 `apps\common`（`ota_engine.h/.c`），平台层留在 `src\l0\ota.c` | 判据："换芯片要不要重写这段代码"。头解析 / 收包节奏 / 断点偏移 / 失败回码全都不重写；flash / PSA / mcuboot / settings / socket 全都要重写 |
| 后端接口 | `begin` 用**三态返回**：`<0 失败 / 0 全量 / >0 续传偏移` | 把"有没有断点、断点在哪"这个平台判断压成一个整数，引擎不必再问第二句 |
| 失败文案 | 后端结构里单列 `err_token` | 旧实现对不同失败回不同 JSON `err`（`flush`/`sha mismatch`/…），换实现不能静默改协议 |
| 目录收拢 | `git mv`（已跟踪的 5 个）+ 等价移动（本轮新切的 12 个 untracked）+ sha256 逐一比对 | `git mv` 对未跟踪文件无效，这条实测过 |
| 层级物理隔离 | `CMakeLists.txt` 按层收窄 `-I`，L2 不给 `src\l1` | 负向实证：注入 `#include "http_core.h"` 到 `cred.c` → `fatal error: http_core.h: No such file or directory` |
| 库类型 | **OBJECT 库**，用 `$<TARGET_OBJECTS:…>` 进 app | STATIC 库会把"只往 section 注册、没人按名字引用"的对象裁掉（见"事故"） |

## 改动清单

| 文件 | 改了什么 |
|---|---|
| `apps\common\include\ota_engine.h` | **扩展**：`enum ota_phase` / 协议原语 / `enum ota_check` + `ota_req_check` + `ota_check_http/token` / `struct ota_backend`（含 `err_msg`、`err_token`）/ `struct ota_sess` + `ota_sess_init/run/finish/abort` |
| `apps\common\src\ota_engine.c` | **新增实现**（约 370 行）：门禁判定、会话推进、失败登记；`OTA_SESS_CHUNK = 2048`（与旧 `OTA_CHUNK` 同值）；`EBADMSG`/`ECONNRESET` 加 `#ifndef` 兜底以便 PC 编译 |
| `apps\common\test\test_ota_engine.c` | **新增**：主机回归（假 flash + 假 socket，45 条断言） |
| `apps\esp32s3\s3_wifi_portal\src\l0\ota.c` | **重写升级入口**：`ota_do_upgrade()` 381 → 90 行；新增后端块 `be_auth/be_begin/be_write/be_finish/be_abort/sock_recv_cb/check_errno/reply_fail`；`g_hop/g_hop_live/g_be_ctx/g_be` 提到文件作用域 |
| `apps\esp32s3\s3_wifi_portal\src\{l0,l1,l2}\` | **目录收拢**：`main.c`/`portal_cfg.h`/`user_page.html` 留根；其余 17 个 `.c` + 16 个 `.h` 按层归位，内容 sha256 全部一致 |
| `apps\esp32s3\s3_wifi_portal\CMakeLists.txt` | **重写**：4 段 OBJECT 库（`portal_eng/portal_svc/portal_api` + `app`），按层 `-I`，`$<TARGET_OBJECTS:…>` 进 app，保留 `zephyr_linker_sources(SECTIONS …sections-rom.ld)` |
| `docs\参考\应用层分层改造教程-s3_wifi_portal.md` | **改**：§2.1/§2.2 换成目录 + OBJECT 骨架；§3.1/§3.3 修掉"所有库都在 whole-archive"的错误结论；§4.1 增"链接方式"行；§6 整章重写（OTA 后端表）；§9 数字与口径重写；§10 状态更新；**新增 §11 事故复盘**；附录文件清单换成分层路径 |

## 事故：拆库把功能静默裁掉（本轮最重要的发现）

拆成 4 个 `zephyr_library_named` 之后，`ninja` exit 0、无 error、`zephyr.bin` 837124 B，
一切看起来都对。做资源核对时才发现 ELF 里 **`http_resource_desc` 符号 0 个**：

| 证据 | 内容 |
|---|---|
| `build.ninja` 链接行 | `… -Wl,--no-whole-archive … libportal_api.a libportal_svc.a libportal_eng.a` —— 自建库落在组外 |
| `zephyr.map` | `http_core` 出现 0 次；`http_resource_desc_portal_service_area` 起止符号同址、段内无记录 |
| 两份 ELF 符号对比 | 缺 **189 个符号 / 58888 字节**，含 `index_html`(18646B)、`user_html`(8544B)、`_k_thread_stack_sh_cmd_tid`(6144B)、`connect_handler`/`status_handler`/`shell_handler` 等 |

根因：`extensions.cmake:878` 的 `zephyr_append_cmake_library()` 在 **application mode**
下只打一条 warning，不把库塞进最终 `--whole-archive` 组。于是"只注册、不被引用"的
对象在静态库里属于未使用，被按需丢弃。**改造前用 `target_sources(app …)`，对象直接进
可执行目标，不存在这个问题。**

试过的两条修法都失败：

| 修法 | 结果 |
|---|---|
| `zephyr_link_libraries(-Wl,--whole-archive … -Wl,--no-whole-archive)` | `ninja: error: dependency cycle: heap_constants.h -> libportal_api.a -> …`（挂到 `zephyr_interface` 后连带被 offsets/pre0 继承） |
| `target_link_libraries(app PRIVATE -Wl,--whole-archive …)` | pre0 链接期 `multiple definition of calloc / malloc / free …`（打乱了 Zephyr 给 libc 排的 group 语义） |
| **OBJECT 库 + `$<TARGET_OBJECTS:…>`** | ✅ `bin` 868916 B，20 条资源描述符在位（19 条业务 + `portal_shell`） |

连带修正了文档里两处错误口径：

- "拆层前后 `zephyr.bin` 都是 837124 B，证明零行为改动" → 真相是**两次都缺同一批对象**，所以相等；
- "所有 lib 都在 `--whole-archive` 里，所以安全" → 实测不在。

## 验证

| 项 | 证据 |
|---|---|
| 编译 | `ninja` exit 0（incremental，`-j 8`）；`zephyr.bin` = **868916 B** |
| 段内符号计数 | `http_resource_desc_portal_service_area` 段大小 `0xa0` = 160 B = 20×8；符号逐个在位：`portal_index`/`portal_index_html`/`portal_scan`/`portal_scan_start`/`portal_status`/`portal_wifi`/`portal_connect`/`portal_saved`/`portal_forget`/`portal_api_disconnect`/`portal_apclients`/`portal_rssi_hist`/`portal_wlog`/`portal_drops`/`portal_boot`/`portal_ota_slots`/`portal_ota_revert`/`portal_pw`/`portal_captive`（+ `portal_shell`） |
| shell 命令 | ELF 内 `shell_cmd_*` 静态命令 16 条 |
| OTA 引擎（主机） | MinGW gcc 编译 + 运行：**45 条断言全过**（门禁 6 类回码 / 全量 / pre-body 拼接 / 续传 / 摘要不符 / 写失败 / 短包 / abort） |
| 层级防火墙 | `cred.c` 注入 `#include "http_core.h"` → 精确报 `cred.c:18:10: fatal error: http_core.h: No such file or directory` |
| 增量耗时（新布局） | no-op **0.82 s**；改一个 L2 文件 **11.02 s**；改 `main.c` **10.93 s**；改 `ota_engine.c` **10.53 s** |
| 未验证 | 真机 OTA 全链路（正常 / 断电续传 / 校验失败）、19 条资源的实际 HTTP 响应、串口命令实际执行 |

## 遗留问题

1. **真机 OTA 回归没做**：设备恢复联网后至少要跑"正常升级 + 断电续传 + 校验失败"三条。
   当前只有主机断言与链接级证据，不覆盖 flash 真实擦写时序。
2. **`t_verify` 计时口径变宽**：现在包住 flush + `psa_hash_finish` + `flash_img_check`，
   旧实现只包 `flash_img_check`。日志里的 verify 数字会变大，属预期，不是回归。
3. **早期收包失败的 err_msg 文案统一化**：旧实现里带 `(early body)` 后缀，现在由后端统一给。
   只影响日志可读性。
4. **`OTA_CHUNK` 已从 app 消失**：块大小改由引擎 `OTA_SESS_CHUNK`（同值 2048）决定。
   若以后要按链路调块大小，改这里而不是 app。
5. **20 vs 19**：代码里 19 条业务资源 + 1 条 `portal_shell`（`CONFIG_SHELL_BACKEND_DUMMY` 打开时注册）。
   "19 个资源"这个提法指的是扣掉 shell 的那一组。
6. **变更已提交**：本轮结束按用户要求做了一次本地 `git commit`（不 push），提交内容见该提交信息；
   `apps\stm32f1\`、`tools\pull-stm32-modules.ps1`、`docs\教程\15-…\换芯片实战-STM32F103.md` 属于
   STM32F103 那条独立工作线，**没有并进这次提交**。

## 同轮整理：脚本与文档的路径同步

代码搬进 `src\l0|l1|l2` 之后，凡是"写死旧路径"的地方都会静默失效（不报错、就是找不到文件或改错文件），
本轮逐一核对并修掉：

| 位置 | 原来 | 现在 | 为什么必须改 |
|---|---|---|---|
| `tools\gen_pages.py` | `OUT = src\pages.h` | `src\l1\pages.h` | 不改的话会**在 `src\` 根新造一份 `pages.h`**；`src\` 也在 `-I` 里，会遮住 l1 里的正式副本 → "改了页面不生效"。改后 `--check` 通过（生成物与仓库内文件逐字节一致） |
| `tools\build_ota.ps1` | `$otaH = src\ota.h` | `src\l0\ota.h` | 版本号单一事实来源，找不到就 `throw`，构建脚本整条挂掉 |
| `apps\…\README.md` | §6 "代码结构（src/main.c）" | 三层结构图 + `apps\common` 的 OTA 协议层 | 这是使用者最先看的一页，旧图已经完全不对应代码 |
| `docs\教程\17.1` | "代码在 `src\main.c`" | 指向 `src\l1\` | 同一原因 |
| `docs\教程\17.5` | `src\ota.c` / `src\ota.h` | `src\l0\…` + 注明协议层在 `apps\common` | OTA 教程的入口路径 |
| `docs\教程\17.10` | `src/pages.h` | `src\l1/pages.h` | 生成物的落点变了 |
| `docs\教程\17\README.md` | "`main.c` 约 1180 行" | 分层的文件清单 | 行数口径已经不成立 |
| `tools\README.md` §6 | 无说明 | 加一段 `_` 前缀一次性脚本的现状 | 40 多个历史脚本写的是改造前路径，**再跑会改错文件**；本轮不删（部分数据件仍被 `sha_golden_test.py` 引用），只标注 |
| 根 `README.md` | `apps\common\` 只列 `store.c` | 补 `ota_engine.c` + `test\` | 组件层新增了东西 |

`_patch_*.py`（`_patch_otah_v2/_patch_shell/_patch_ver107/_patch_ver109`）等历史脚本**本轮保持原样未删**：
按 `tools\README.md` 的约定它们该清，但有的引用了仍在使用的 JS/binary 数据件，
清理要逐个确认引用，单开一轮做 `git rm` 更稳。

> **后记（同日清理轮）**：这一轮已经做完了 —— 34 支零引用脚本（含上面点名的 4 个 `_patch_*`）连同
> `_shcard_js.txt` / `_shblock.txt` 一起 `git rm`，被引用的数据件（`_sha_test.js`、`_otaauth_js.txt`、
> `.ota_nonce`、`_sha_test.js.*.bin`）逐条确认后保留。名单、判据与找回命令见
> `docs\变更记录\仓库精简-2026-11-16.md` 与 `tools\README.md` §10。
