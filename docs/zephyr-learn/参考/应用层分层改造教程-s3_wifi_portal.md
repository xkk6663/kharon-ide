# 应用层分层改造教程 —— s3_wifi_portal

> 对象：`applications/esp32s3/s3_wifi_portal`
> 起点：`src/main.c` 单文件 **4399 行 / 161192 字节**
> 终点：`src/` 下 **17 个 `.c` + 16 个 `.h`**，编成 **4 个 CMake target**，`main.c` 剩 **217 行**
> 状态：**已落地并编译通过**（`zephyr.bin` = 868916 B，段内 20 条 HTTP 资源在位）。
> ⚠️ 初版写的"改造前后都是 837124 B"是错的口径，原因见 §9.3 与 §11。
> 本文只讲这一件事：**怎么把一个 4399 行的 Zephyr app 拆成有边界的四层，以及为什么这么拆**。

---

## 0. 先看结论

改造前，这个工程的所有 C 代码都在一个编译单元里，边界只存在于作者的记忆里。
改造后，边界变成了**编译期会报错的东西**：

```
app (L3 编排) ──link──> portal_api (L1 接口) ──> portal_svc (L2 服务) ──> portal_eng (L0 引擎)
                            │                       │                      │
                       认识 http              认识 wifi_mgmt          认识 flash/mcuboot
                       不认识 flash           不认识 http             不认识 http
```

四层的判据只有一句：**"它认不认识上面那层的东西"**。
L2 不认识 `http.h`，所以 L1 想和 L2 通信只能走 L2 自己声明的接口；L1 越权去摸 L2 的 `static` 变量，
编译器会直接给 `error: 'g_aps' undeclared`（本工程真实报过，见 §8）。

---

## 1. 改造前的真实形态

### 1.1 数字

| 指标 | 值 |
|---|---|
| `src/main.c` | 4399 行 / 161192 字节 / 纯 LF / UTF-8 无 BOM |
| `main()` 函数 | 第 4145–4264 行 |
| 最后一行 | `SHELL_CMD_REGISTER`（第 4398 行） |
| 编译单元（app 侧） | 1 个 `main.c` + 1 个 `ota.c` |

那 4399 行里同时装着这些东西：

1. 凭据表（`struct cred_item`、`g_creds`、NVS 读写、轮换重连下标）
2. 扫描缓存（`struct ap_entry`、`g_aps`、`ap_lock`、`MAX_APS`）
3. 链路状态机（`enum link_state`、`g_state`、连接超时 `k_work_delayable`、RSSI 采样线程）
4. SoftAP（`g_ap_params`、`ap_retarget()` 换信道、`ap_client_add/remove`、DHCP 起停）
5. 对时（SNTP 线程、`g_unix_at_sync`、`time_apply_http_date`）
6. 出网自检（DNS 解析 + HTTP GET `www.baidu.com`、`g_netcheck_*`）
7. 呼吸灯（`led_thread`、`K_THREAD_DEFINE`、相位对齐）
8. DNS 劫持（UDP :53 通配应答）
9. HTTP 服务（1 个 service + 19 个 resource 的 handler 与 detail）
10. 三张页面（用户页 HTML / 开发者调试台 / 口令页，含 `index_html[]` 这类几 KB 的字符串）
11. 上电自检（6 条 `http_self_test`）
12. 串口命令（`SHELL_CMD_REGISTER` 5 条）
13. `main()`：硬件就绪时间线

### 1.2 依赖特征（这才是问题所在）

Zephyr 的 app 本身就是一个库 —— `app/libapp.a`。所有 `src/*.c` 编进同一个静态库，
于是：

- **任何 `.c` 可以 include 任何 `.h`**，包括只该给 handler 用的页面表；
- **任何 `.c` 可以 `extern` 任何全局**，包括 `static` 改成 `extern` 的中间状态变量；
- **没有任何符号可见性约束**：所有非 `static` 函数对全工程可见；
- 唯一的"分层"是文件里注释的顺序。

代价不是"代码丑"，而是三条具体的操作成本：

| 场景 | 改造前你要做的事 | 哪一步会出错 |
|---|---|---|
| 改 WiFi 连接逻辑 | 在 4399 行里定位 `connect_handler`、`connect_work`、`wifi_event_handler` 三处，跨越 3000 行 | 漏改其中一处，出现"网页显示连上了但灯还是蓝的" |
| 加一个新 API | 在同一个文件里找 buffer 声明区、handler 区、detail 区、`HTTP_RESOURCE_DEFINE` 区，四个区相隔上千行 | buffer 忘了声明成 `static` → 与别的 handler 抢内存 |
| 解释一个 `static` 变量 | 全文件搜索，因为它可能被 40 处读写 | 不知道谁在写，改一个条件就成了概率性 bug |

---

## 2. 改造后的形态

### 2.1 文件与行的分布

`src/` 下 17 个 `.c`（14 个是这次新切的，`main.c`/`ota.c`/`ops.c` 是原有的），
按层收拢后分成三个目录（`src/l0` / `src/l1` / `src/l2`，根上只留跨层共享的
`main.c`、`portal_cfg.h`、`user_page.html`）：

| 层 | 文件 | 行数 |
|---|---|---|
| **L3 编排** | `main.c` | 217 |
| **L1 接口** | `http_core.c` | 193 |
| | `api_wifi.c` | 730 |
| | `api_dev.c` | 538 |
| | `page.c` | 429 |
| | `shell_cmds.c` | 151 |
| | `selftest.c` | 148 |
| **L2 服务** | `cred.c` | 382 |
| | `wifi_scan.c` | 142 |
| | `portal_ap.c` | 276 |
| | `wifi_link.c` | 822 |
| | `time_sync.c` | 292 |
| | `netcheck.c` | 236 |
| | `led_status.c` | 159 |
| | `dns_hijack.c` | 222 |
| **L0 引擎** | `ota.c` | 1332 |
| | `ops.c` | 686 |

`main.c` 从 4399 → 217 行，剩下的正好是它该干的三件事：
`wifi_event_handler` 的分派、`ipv4_event_handler`、`main()` 的时间线。

### 2.2 四个 target 的实际写法

`CMakeLists.txt` 现在是 4 段，顺序即依赖顺序。**用 OBJECT 库，不用 STATIC 库**
（原因见 §11；一句话：静态库会把"只往 section 注册"的对象裁掉）：

```cmake
# L0 引擎：ota / ops / 呼吸灯 / OTA 协议层。自带线程、自带 settings handler、不认识"配网页"
add_library(portal_eng OBJECT "")
target_link_libraries(portal_eng PUBLIC zephyr_interface)   # 拿 zephyr 的编译选项/宏
target_sources(portal_eng PRIVATE
        src/l0/ota.c src/l0/ops.c
        ${KORVO_COMMON}/src/breath_sync.c
        ${KORVO_COMMON}/src/ota_engine.c)
target_include_directories(portal_eng PRIVATE
        ${APP_SRC}/l0 ${APP_SRC} ${KORVO_COMMON}/include)

# L2 服务：凭据 / 扫描 / 链路 / SoftAP / 对时 / 自检 / 灯 / DNS。不认识 http.h，不吐 JSON
add_library(portal_svc OBJECT "")
target_link_libraries(portal_svc PUBLIC zephyr_interface)
target_sources(portal_svc PRIVATE
        src/l2/cred.c src/l2/wifi_scan.c src/l2/portal_ap.c src/l2/wifi_link.c
        src/l2/time_sync.c src/l2/netcheck.c src/l2/led_status.c src/l2/dns_hijack.c)
target_include_directories(portal_svc PRIVATE
        ${APP_SRC}/l2 ${APP_SRC}/l0 ${APP_SRC} ${KORVO_COMMON}/include)
target_link_libraries(portal_svc PUBLIC portal_eng)

# L1 接口：HTTP 资源 + 串口命令 + 上电自检。只做"协议 ↔ 快照"的翻译，不持有状态
add_library(portal_api OBJECT "")
target_link_libraries(portal_api PUBLIC zephyr_interface)
target_sources(portal_api PRIVATE
        src/l1/http_core.c src/l1/api_wifi.c src/l1/api_dev.c src/l1/page.c
        src/l1/shell_cmds.c src/l1/selftest.c)
target_include_directories(portal_api PRIVATE
        ${APP_SRC}/l1 ${APP_SRC}/l2 ${APP_SRC}/l0 ${APP_SRC})
target_link_libraries(portal_api PUBLIC portal_svc)

# L3 编排：app 只剩 main.c。顺序是产品行为，只属于这一层
target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE ${APP_SRC}/l1 ${APP_SRC}/l2 ${APP_SRC}/l0 ${APP_SRC})
# OBJECT 库不会自动把对象交给下游，必须显式取 TARGET_OBJECTS（漏了 = app 里一个符号都没有）
target_sources(app PRIVATE
        $<TARGET_OBJECTS:portal_eng>
        $<TARGET_OBJECTS:portal_svc>
        $<TARGET_OBJECTS:portal_api>)
```

四段的差别只有三样东西：`sources`、`include_directories`、`link_libraries`。
**分层不是靠目录名，是靠这三个 `-I` 和一条单向的 `link_libraries`。**
目录只是让人一眼看出边界在哪。

---

## 3. Zephyr 提供的三件工具，各自到底在干什么

拆层这件事，Zephyr 给了三个原语：`zephyr_library`、`SYS_INIT`、iterable section。
很多人把它们混着用（"反正都是 Zephyr 的东西"），其实它们解决的是三个**完全不同**的问题：

| 工具 | 解决的问题 | 本工程用不用 |
|---|---|---|
| `zephyr_library` / `zephyr_library_named` | **编译期**：把一组 `.c` 变成一个带 include 边界的静态库 | ✅ 用，构成四层 |
| `SYS_INIT` | **启动期**：把"初始化动作"从 `main()` 里挪走，按依赖级别排序 | ❌ 刻意不用（见 3.2） |
| iterable section | **链接期**：让数据/对象自注册进表，不用手工维护清单 | ✅ 靠它，`HTTP_RESOURCE_DEFINE` 等 |

### 3.1 `zephyr_library` / `zephyr_library_named`

**它是什么。** 在 `west/zephyr/cmake/modules/extensions.cmake` 里，它最终做两件事：

```cmake
# extensions.cmake:646 起
function(zephyr_library_named name)
  set(ZEPHYR_CURRENT_LIBRARY ${name} PARENT_SCOPE)
  add_library(${name} STATIC "")      # 1) 建一个普通 CMake 静态库
  zephyr_append_cmake_library(${name}) # 2) 登记进 ZEPHYR_LIBS_PROPERTY，链接时自动带上
  ...                                  # 3) 继承 zephyr 的编译选项 / -I / warnings
endfunction
```

`zephyr_library` 就是 `zephyr_library_named(app)` —— 名字固定为 `app`。所以：

- **`app` 也是一个 `zephyr_library`**（名叫 `app`），只不过 Zephyr 早就替你建好了；
- 你新建的 `portal_svc` 与 `app` 是**平级的库**，谁链谁由你写；
- ⚠️ **但"平级的静态库"不等于"和 app 一样不会被裁"** —— 这是本教程最贵的一课，
  见 §11。原因是 `zephyr_append_cmake_library` 在**application mode** 下只打一条 warning，
  并不把你建的库塞进 `ZEPHYR_LIBS`（见 `extensions.cmake:878-888`），
  于是这些库在最终链接行上落在 `-Wl,--no-whole-archive` 之后，
  只被 section 注册、没有符号引用的对象会被静默裁掉。**当前工程已改用 OBJECT 库规避。**

**为什么拆库才叫分层。** 关键在 `zephyr_library_include_directories(PRIVATE ...)`：

```cmake
zephyr_library_include_directories(PRIVATE ${KORVO_COMMON}/include)
```

`PRIVATE` 的含义是"只给这个 target 自己用，不向后传递"。于是：

- `portal_svc` 的每个 `.c` 都能 `#include "breath_sync.h"`；
- `app` 的 `main.c` **不能** —— 它没有这个 `-I`，`#include "breath_sync.h"` 直接
  `fatal error: breath_sync.h: No such file or directory`。

这就是"编译期防火墙"：**越过边界不是道德问题，是构建失败。**

反过来说，如果你写 `PUBLIC`，这条防火墙就自己拆了：
`PUBLIC` 会传递给所有链接它的 target，`app` 又能看见 `portal_svc` 的私有头，
几个月后没人记得边界在哪。**这条规则在本工程是硬约束：一律 `PRIVATE`。**

**三个必须记住的坑（本工程全踩过）：**

1. **`zephyr_library_link_libraries` 作用于"刚创建的那个库"。**
   它的实现是 `target_link_libraries(${ZEPHYR_CURRENT_LIBRARY} PUBLIC ...)`
   （`extensions.cmake:790`）。所以如果你想给 L1 链 L2，必须**紧跟在
   `zephyr_library_named(portal_api)` 之后**调用。写成"最后统一链"就会链到
   `ZEPHYR_CURRENT_LIBRARY` 当时的值上 —— 本工程真实报过：

   ```
   Target "portal_api" links to itself
   ```

   原因：在 L3 段里调 `zephyr_library_link_libraries(portal_api)`，
   `ZEPHYR_CURRENT_LIBRARY` 仍是 `portal_api`（最后创建的那个），于是自己链自己。

2. **`PRIVATE` 不传递，所以下层要用的头必须在下层自己写。**
   `portal_svc` 要 `breath_sync.h`，就得自己在 `zephyr_library_include_directories`
   里加 `${KORVO_COMMON}/include` —— 不能指望从 `portal_eng` 传下来
   （`portal_eng` 加的是 `PRIVATE`，不传）。

3. **`app` 不是 `zephyr_library` 的函数调用对象，L3 要用 `target_link_libraries`：**

   ```cmake
   target_link_libraries(app PRIVATE portal_api)   # ✅
   zephyr_library_link_libraries(portal_api)       # ❌ 会把 portal_api 链到 portal_api
   ```

**拆库会不会让编译变慢？**（这是当时最担心的问题，结论用实测回答，见 §9）
真正的全量触发器不是"文件数"，而是 `prj.conf`（→ `autoconf.h`）和 `*.overlay`
（→ `devicetree_generated.h`）—— 这两个任何一个变了，650 个 `.obj` 全废，
与拆不拆库无关。而改一个 `.c` 时，拆库后要重编 1 个更小的文件再全量重链接，
净效果是**没有变快，慢了约 4 s**（6.6 s → 10.5–11.0 s），原因见 §9.2 的诚实分析。

### 3.2 `SYS_INIT`：为什么本工程**刻意不用**

**它是什么。** `include/zephyr/init.h` 里，`SYS_INIT(fn, level, prio)` 把函数指针塞进
`.zinit` 段（`struct init_entry`），内核启动时由 `kernel/init.c` 的
`z_sys_init_run_level()` 按级别顺序调用：

```
PRE_KERNEL_1 → PRE_KERNEL_2 → POST_KERNEL → APPLICATION
每个级别内按 prio 从小到大
```

它的语义是**依赖关系**："这块初始化必须在别的什么之前"。典型用法：驱动在
`POST_KERNEL` + `prio 50` 注册，因为它依赖内核对象已可用。

**为什么本工程不用。** 因为 `main()` 在这里表达的不是依赖，而是**硬件就绪的时间线**：

```c
main():
  k_msleep(2000)            // 等 wifi 驱动把 netif 建好
  enable_softap()           // 起 AP
  bind(udp, 53)             // DNS 劫持必须在 netif 存在之后
  http_server_start()       // HTTP 必须在 AP 起来之后（否则首包丢）
  6 条 http_self_test()     // 自检要有 HTTP + 路由
  settings_load()           // NVS
  ops_boot_init()           // 重启原因记账
  ota_srv_start()           // 最后才开 OTA 端口
```

把这些搬进 `SYS_INIT` 会得到两个后果：

1. **时序被抹平**：`SYS_INIT` 只保证"级别/优先级顺序"，不保证"等 AP up 再 bind :53"。
   AP up 是**异步事件**（`NET_EVENT_WIFI_AP_ENABLE_RESULT`），不是初始化阶段。
2. **失败无处上报**：`SYS_INIT` 返回错误会让启动失败，但这里每条都是"失败了就降级继续"
   （比如自检失败只影响页面上的一个红点）。

**什么时候该用：** 组件有"独立于 app 的初始化契约"时。比如 `breath_sync` 需要
起一个线程、注册一个 `breath_clock_observe_src` 观察者 —— 这件事和产品的时间线无关，
可以用 `SYS_INIT(..., APPLICATION, 90)`。**判据一句话：**
"这段初始化失败，产品还能不能跑？"能跑且顺序无关 → `SYS_INIT`；不能 → 留在 `main()`。

> 提醒：`SYS_INIT` 也不是"更高级"的写法。把 `main()` 掏空成 `SYS_INIT` 一堆，
> 只会把"启动顺序"这个产品行为藏进链接顺序里，出了问题更难查。本工程的做法是
> 在 `main.c` 顶部用注释把时间线钉死，**顺序是一等公民**。

### 3.3 iterable section：拆库之后，自注册表为什么不会空

**它是什么。** 三个东西配合：

```c
// 1) 数据进段
#define HTTP_RESOURCE_DEFINE(symbol, service, path, detail)  \
        static const struct http_resource_desc symbol         \
        __attribute__((section(".http_resource_desc_" #service))) = { ... };

// 2) 段边界由链接脚本给（START/END 两个符号）
// 3) 遍历
STRUCT_SECTION_FOREACH(http_resource_desc_portal_service, d) { ... }
```

`STRUCT_SECTION_FOREACH` 展开成"从 `_start` 到 `_end` 指针走一遍"。
所以这张表**不需要任何人手工维护**：加一个 `HTTP_RESOURCE_DEFINE` 就自动出现。

本工程有三处依赖它：

| 宏 | 段 | 谁遍历 |
|---|---|---|
| `HTTP_SERVICE_DEFINE(portal_service, ...)` | `http_service_desc` | Zephyr http_server 起服务 |
| `HTTP_RESOURCE_DEFINE(name, portal_service, path, detail)` | `.http_resource_desc_portal_service` | 上面那个 service |
| `SHELL_CMD_REGISTER(name, help, fn)` | `shell_cmd` | shell 子命令表 |
| `K_THREAD_DEFINE(tid, ...)` | `.k_thread` / `_k_thread_obj` | 内核启动时把线程建起来（本工程 `led_status.c` 的 `led_tid`） |

**为什么拆库不丢。** 静态库的默认链接行为是"只取用到的 `.o`"。
如果 `api_wifi.c` 里的 `HTTP_RESOURCE_DEFINE` 没有被任何代码显式引用，
链接器会认为这个 `.o` 没用到 → 整个丢掉 → 段里少一条 → 网页 404。

**这里曾经写错过一次，必须记住教训。** 本文档初版写的是"本工程所有 lib 都在
`--whole-archive` 里，所以安全"。这句话在 2026-11-16 被实测推翻：

```
build.ninja 最终链接行： ... -Wl,--no-whole-archive  ...  libportal_api.a libportal_svc.a libportal_eng.a
elf 里 http_resource_desc 符号：0 个；段 ._http_resource_desc_portal_service.static.* 为空
```

也就是说，四个 `zephyr_library_named` 库**不在** `--whole-archive` 组里，
19 条 HTTP 资源、串口命令线程、页面数据全被裁掉，而链接器一声不吭。
修法与复盘见 §11 —— 一句话：**用 OBJECT 库，别用 STATIC 库。**

**因此这条结论只能靠"数符号"来验证，不能靠读 CMake 文档来假设。** 验证方法：

```powershell
# 资源描述符都在 .http_resource_desc_portal_service 段里，数符号即可
objdump -t zephyr.elf | Select-String 'http_resource_desc_portal_service'
```

**一个必须手写的段声明。** 段名是 `".http_resource_desc_" #service` **动态拼出来的**，
工具链的默认链接脚本不认识它，于是链接期报：

```
undefined reference to `_http_resource_desc_portal_service_list_start'
```

解法是在应用里显式给链接脚本补一段（本工程 `sections-rom.ld`）：

```ld
SECTION_PROLOGUE(.http_resource_desc_portal_service,,)
{
        __http_resource_desc_portal_service_list_start = .;
        KEEP(*(SORT(.http_resource_desc_portal_service*)));
        __http_resource_desc_portal_service_list_end = .;
} GROUP_ROM_LINK_IN(RAMABLE_REGION, ROMABLE_REGION)
```

并在 `CMakeLists.txt` 里挂上去：

```cmake
zephyr_linker_sources(SECTIONS ${CMAKE_CURRENT_SOURCE_DIR}/sections-rom.ld)
```

**这一行绝对不能删**，删了链接就断。拆层时它被保留在文件末尾。

---

## 4. 改造前 vs 改造后：依赖关系到底变了什么

这一节是整篇文档最该逐行读的地方。同一份代码，"依赖"在改造前和改造后指的是**不同的东西**。

### 4.1 依赖的形态

| 维度 | 改造前（单文件 / libapp.a） | 改造后（4 个 target） |
|---|---|---|
| 谁能 include 谁的 `.h` | 谁都能 include 谁：`-I src` 是全局的 | 按层下发：L2 看不到 `http_core.h`，L3 看不到 `portal_ap.h`（除非 L1 头里自己 include） |
| 越界时会发生什么 | 什么都不会发生，悄悄编译通过 | `fatal error: xxx.h: No such file or directory` 或 `error: 'g_aps' undeclared` |
| 全局变量的可见性 | 全工程可见（同一编译单元） | 跨库只能是 `extern` 声明 + 定义在某个 `.c`；`static` 真的只在本文件可见 |
| 链接方式 | 同一个可执行目标里的对象 | 4 个 OBJECT 库，对象经 `$<TARGET_OBJECTS:…>` 进 app（**不能用 STATIC，见 §11**） |
| 新增一个功能要动几处 | 同一文件 4 个区（buffer/handler/detail/注册） | 改所属模块 1 个 `.c` + 必要的 `.h`；注册宏仍在同文件 |
| 隔离性 | 改 A 可能影响 B（共享 static） | 改 A 只影响 A 的接口被谁用（编译器列出引用者） |
| 目标数 | 1 个（`app` 收全部源码） | `portal_eng` / `portal_svc` / `portal_api` / `app`（前三个是 OBJECT 库） |
| 编译单元（app 侧） | 2 个（`main.c`、`ota.c`） | 18 个（17 个 src + 1 个 common） |

### 4.2 一个具体的对比例子：`/api/scan` 读扫描缓存

**改造前**，`scan_handler` 在同一编译单元里直接读 `g_aps[i]`：

```c
/* 改前：handler 直接摸 L2 的数组和锁 */
k_mutex_lock(&ap_lock, K_FOREVER);
for (i = 0; i < g_ap_count; i++) {
        json_escape(esc, ..., g_aps[i].ssid);
        ... g_aps[i].rssi, g_aps[i].channel ...
}
k_mutex_unlock(&ap_lock);
```

这里有三件事被隐式绑死了：
锁的名字（`ap_lock`）、数据的形状（`struct ap_entry` 的字段名）、上限（`MAX_APS`）。
任何一个人改 `wifi_scan.c` 里的结构体字段，`api_wifi.c` 会"跟着"编译通过或不通过 ——
**而这两件事之间本来没有任何契约。**

**改造后**，L2 提供三个接口（`wifi_scan.h`）：

```c
void               scan_lock(void);
void               scan_unlock(void);
const struct ap_entry *scan_entries(void);   /* 数组基址 */
int                scan_count(void);
```

L1 的写法变成：

```c
scan_lock();
for (int i = 0; i < scan_count() && n < sizeof(g_scan_json) - 120; i++) {
        const struct ap_entry *e = &scan_entries()[i];
        json_escape(esc, sizeof(esc), e->ssid);
        ...
}
scan_unlock();
```

变的不是代码量（差不多），而是**谁拥有什么**：

- 锁归 L2 所有，名字改了 L1 不用动（它调 `scan_lock()`）；
- 数组归 L2 所有，内部扩成 40 条、加个 `bssid` 字段，L1 只用到它要的字段；
- **"调用方拿锁"这个选择是明确决策**：`scan_entries()` 返回的是内部数组地址，
  L1 拿到之后必须在锁内用完。这个约定写在 `wifi_scan.h` 的注释里，
  而不是靠"大家都知道要加锁"。

> 这里刻意没做"给 handler 返回一份拷贝"：`MAX_APS = 20` × 每条约 100 B ≈ 2 KB 栈，
> 而 HTTP 线程栈只有 8 KB。**用锁换栈，是这个工程的正确取舍。**

### 4.3 依赖方向为什么必须单向

一旦允许反向（L2 认识 L1），会发生这些事：

1. L2 里出现 `#include "http_core.h"` → `portal_svc` 的 `-I` 里没有 `src` 的 http 头？
   其实有（`-I src` 全在同一个目录），所以**物理上拦不住**；
2. 于是"L2 不认识 http"这条规则只能靠人守，几个月后必然破；
3. 破了之后 `portal_svc` 无法单独复用（换个工程没有 http server 就编不过）。

这就是为什么本次改造把"结论"写进了 `CMakeLists.txt` 的注释里 ——
**注释是给人看的，`-I` 是给编译器看的，两者必须一致。**

> 遗留问题：14 个模块的 `.h` 现在都平铺在 `src/`，`-I src` 让所有层互相可见。
> 目录收拢（`src/l1/`、`src/l2/`…）放到下一轮只做 `git mv` + include 修正 ——
> 那一轮之后，"物理防火墙"才真正闭环（§10）。

---

## 5. 分层判据：一个函数该放哪一层

不要按"文件篇幅"切，按这四个问题切：

| # | 问题 | 是 → | 否 → |
|---|---|---|---|
| 1 | 它认识 `struct http_client_ctx` / `HTTP_*` 宏吗？ | **L1** | 继续问 |
| 2 | 它认识 `flash_area` / `boot_*` / `settings_*` 吗？ | **L0** | 继续问 |
| 3 | 它绑某一颗 SoC / 某块板吗？（GPIO 号、`esp_*` API、分区名） | **留在 app** | 继续问 |
| 4 | 有几个消费者？（≥2 才有共享价值） | **L2 或 common** | 就放在调用它的那一层，别上升 |

用这四问过一遍本工程的关键选择：

| 东西 | 归属 | 判据结果 |
|---|---|---|
| `scan_handler`（吐 JSON） | L1 | 认识 `http_client_ctx` → 问 1 命中 |
| `wifi_scan.c`（缓存 AP 列表） | L2 | 不认识 http、不认识 flash、不绑 SoC → 问 3/4 → 有两处消费者（页面 + 串口命令） |
| `enable_softap()` | L2 | 用 `net_mgmt`（跨平台），不绑 SoC → L2；但 **AP 的 SSID/信道策略**留在 `portal_cfg.h` |
| `main()` 的时间线 | L3 | 是产品行为，只有一份 |
| `ota.c` 的 flash 写入 / `boot_request_upgrade` | L0 + 平台 | 认识 `flash_img` / mcuboot → 问 2 命中，且**换芯片必须重写** |
| `ota_engine.c`（头解析 / 分段算术） | **applications/common** | 不认识 http/flash、不绑 SoC、有 2 个消费者（升级入口 + 串口校验）→ 全命中 |

最后一行是这次新增的 OTA 抽象，下一节单独讲。

---

## 6. OTA 抽象层：把整条升级流程抽成"协议 + 后端"

> 需求原话：**"ota 的框架和 ota 的升级逻辑可以把抽象层提出来放到组件里面，方便以后别的跨平台跨芯片 ota 使用"**。
>
> 分两轮做的：第一轮只抽了 145 行纯函数；第二轮（本轮）把 `ota_do_upgrade()` 整条流程
> 搬进组件，`ota.c` 只剩"这个平台怎么落地"。下面写的是**最终形态**。

### 6.1 判据：一条线切开"协议"和"平台"

抽之前先把 `ota_do_upgrade()` 的 381 行按"换芯片要不要重写"分堆：

| 只有协议说了算（→ `applications/common`） | 只有本平台说了算（→ 留在 `ota.c`） |
|---|---|
| 头怎么解析、缺哪个头算非法 | 写到哪个分区、用哪个 flash API |
| 收到的字节和 `Content-Length` 还差多少 | `flash_img_buffered_write` 的缓冲语义 |
| 断点偏移怎么算、何时允许续传 | 断点记录存哪（settings 还是别的） |
| 整包摘要何时结束、跟谁比 | PSA 怎么初始化、SHA 上下文多大 |
| 失败该回哪个 HTTP 码、哪个 token | mcuboot 怎么切槽、是否 PERMANENT |
| 会话阶段名与状态机 | socket 怎么收、PSK 从哪来 |

左边这一列**一行 Zephyr 代码都没有**，右边这一列**每一行都在碰 Zephyr**。
分界线不需要争论，看 include 就知道。

### 6.2 组件里现在有什么（`applications/common/include/ota_engine.h`）

三块，彼此独立：

```c
/* ① 协议原语：纯函数，无状态 */
int  ota_find_hdr_end(const uint8_t *buf, size_t len);
const char *ota_hdr_get(const char *hdr, const char *name);
uint32_t ota_hdr_get_u32(const char *hdr, const char *name, uint32_t dflt);
size_t ota_hdr_get_hex(const char *hdr, const char *name, char *dst, size_t dst_len);
bool ota_hex_eq(const char *a, const char *b, size_t n);
void ota_hex_encode(const uint8_t *in, size_t n, char *out);
uint32_t ota_seg_index(uint32_t off, uint32_t seg_size);
uint32_t ota_seg_base(uint32_t idx, uint32_t seg_size);
uint32_t ota_seg_count(uint32_t total, uint32_t seg_size);
const char *ota_phase_str(enum ota_phase ph);

/* ② 门禁：把"什么样的升级请求算合法"变成一次判定 + 一张文案表 */
enum ota_check ota_req_check(const char *hdr, uint32_t local_ver, uint32_t slot_max,
                            uint32_t last_nonce, ota_auth_cb auth, void *auth_ctx,
                            struct ota_req *out, char *why, size_t why_len);
uint16_t     ota_check_http(enum ota_check c);   /* OK→200 / NO_HDR→400 / AUTH→401 … */
const char  *ota_check_token(enum ota_check c);  /* "ok" / "badauth" / "replay" … */

/* ③ 会话推进：把"收到多少、还差多少、何时算完"变成状态机 */
struct ota_backend { … };      /* 四个回调，见 6.3 */
struct ota_sess    { … };      /* phase / total / written / start_off / err / token */
void ota_sess_init(struct ota_sess *s, const struct ota_req *req);
int  ota_sess_run(struct ota_sess *s, const struct ota_backend *be,
                  ota_recv_cb recv, void *recv_ctx,
                  const uint8_t *pre, size_t pre_len);
int  ota_sess_finish(struct ota_sess *s, const struct ota_backend *be,
                     const struct ota_req *req);
void ota_sess_abort(struct ota_sess *s, const struct ota_backend *be);
```

`ota_check_http` / `ota_check_token` 的意义容易被低估：**"哪种失败回 400、哪种回 403"
是协议常识，不是板子的知识。** 以前这两张 switch 写在 app 里，换工程就得抄一遍、
抄错一个分支就是"版本过旧回 500"。放组件里，所有用这套 OTA 的工程回码天然一致。

### 6.3 后端表：四个回调，语义只有一条不好猜

```c
struct ota_backend {
        /* <0 = 开包失败（rc 直接当 errno 用）
         *  0 = 从头收全量
         * >0 = 可以续传，这是断点偏移（引擎会跳过这么多字节） */
        int32_t (*begin)(void *ctx, const struct ota_req *req);
        int     (*write)(void *ctx, const uint8_t *data, size_t len);
        int     (*finish)(void *ctx, const struct ota_req *req);
        void    (*abort)(void *ctx);
        void *ctx;
        char  err_msg[64];     /* 给人看的失败原因 */
        const char *err_token; /* JSON 里 err 字段的值 */
};
```

**唯一需要背下来的是 `begin` 的三态返回值。** 它把"有没有断点、断点在哪"这个
平台相关的判断，压成了一个整数：

| 返回 | 引擎怎么做 | 平台在这之后必须保证什么 |
|---|---|---|
| `>0` | `sess.written = off`，只收剩下的 `total - off` | flash 的写指针已经挪到 `off`，`off` 之前的字节已经在介质里 |
| `0` | 从头收满 `total` | 介质已清空/可覆盖，断点记录已重建 |
| `<0` | 整个请求失败，不再收一个字节 | `err_msg` / `err_token` 已填好 |

`begin` 返回正数时，引擎**不会再问一句"你确定吗"** —— 所以平台的回读校验必须
在 `begin` 里做完（本工程就是 `rb_check` + `rb_readback_verify`，见 6.4）。

`err_token` 单列一个字段，是为了不改动失败文案：旧实现里"flush 失败"回
`{"err":"flush"}`、"摘要不符"回 `{"err":"sha mismatch"}`，现在由后端在自己的失败点
写 `be->err_token`，引擎原样透传 —— 这样换实现不会静默改变对外协议。

### 6.4 平台侧：`ota.c` 里现在剩下什么

`ota_do_upgrade()` 从 381 行缩到 **90 行**，只剩三段：门禁 → 收包 → 收尾切槽。
其余全部变成后端回调：

| 回调 | 它做的事 | 关键细节（都是踩出来的） |
|---|---|---|
| `be_begin` | `flash_img_init` → 整包/分段两个 `psa_hash_setup` → 续传握手 → 重建断点记录 | 续传成功必须同时设 `stream.bytes_written = off` **和** `stream.erased_up_to = off`（后者相对前者，归零会重擦已写好的开头）；`g_rb_valid` 要立刻置真，否则"断线→立刻重连"会退化成全量 |
| `be_write` | `flash_img_buffered_write` + 整包 `psa_hash_update` + `seg_feed` + 累加 `g_ota.written` | 顺序不能换：先落介质再累积摘要，失败时摘要不脏 |
| `be_finish` | flush → `psa_hash_finish` → 比 `X-Sha256` → `flash_img_check` 回读 | 两次比对都要有：期望摘要拦"包被换掉"，回读拦"没写进去" |
| `be_abort` | 释放两个 PSA 上下文 | 幂等；`finish` 已经释放过时不能二次 abort |

`g_hop`（整包摘要上下文）**必须从局部变量升成文件作用域**：它要跨
`begin/write/finish` 三次回调存活。这是"抽流程"必然带来的一个副作用 ——
状态搬到了调用者看不见的地方，所以配上 `g_hop_live` 标志位防止二次释放。

**一处刻意保留的旧判据。** 续传请求失败要退回全量时，旧代码用 `if (!resumed)` 重建记录；
新代码的等价条件是 `if (b->off == 0)`（"最终从 0 开始"），**不是** `if (!req->resume)`。
差别在"要求了续传但回读校验失败"这条路径上：后者会留下一条 0 字节旧记录，
下次断线重连仍被判成"记录可用"而去回读校验。这一条是迁移后回看旧代码才发现的偏差。

### 6.5 迁移前后对照

| 维度 | 迁移前 | 迁移后 |
|---|---|---|
| `ota_do_upgrade()` | 381 行，头部解析/收包循环/摘要/回读/切槽混在一起 | 90 行，三段：门禁 → 收包 → 收尾 |
| 块大小 `OTA_CHUNK` | app 里的宏（2048） | 组件里的 `OTA_SESS_CHUNK`（2048，同值；app 不再需要这个宏） |
| 失败文案 | app 里 12 处 `reply(...)` 各写各的 | 引擎统一 `sess.token/.http_code`，app 一个 `reply_fail()` |
| 换芯片要改哪 | 整段重写 | 只重写四个回调 |
| 能上 PC 跑单测吗 | 不能（`zsock_recv`/`flash_img_*` 全在里面） | 能（见 6.6） |
| 计时口径 | `t_verify` 只包 `flash_img_check` | `t_verify` 包 flush + hash_finish + check（**数字会变大，只影响日志**） |

### 6.6 怎么验证"没改坏"（没有设备的情况下）

设备连不上 WiFi，所以这一轮用的是两道**不需要板子**的关卡：

1. **编译 + 链接**：`ninja` exit 0；`zephyr.bin = 868916 B`。
2. **主机回归**：`applications/common/test/test_ota_engine.c` + MinGW gcc，把引擎当普通 C 库编，
   用一个"假 flash + 假 socket"跑 **45 条断言**：

```
[gate]              OK / NO_HDR(400) / NO_SHA / AUTH(401) / TOO_BIG(413) / REPLAY / OLD_VER(409) / X-Force 放行
[full: begin=0]     run → written==total → 假 flash 字节一致 → verify → finish → done
[pre-body]          header 后已读到的 500 B 与后续 2500 B 拼接无错位
[resume: begin=1024] 只收剩下的 1976 B，续上位置正确
[finish 失败]        token=sha mismatch / http=500 / phase=fail
[write 失败]         token 来自后端 err_token、err_msg 来自后端
[short body]         token="short body" / http=400
[abort]              可空转不崩
RESULT: PASS（0 条失败）
```

命令（仓库根）：

```powershell
gcc -std=gnu11 -Wall -Wextra -I applications\common\include `
    applications\common\test\test_ota_engine.c applications\common\src\ota_engine.c -o t_engine.exe
.\t_engine.exe
```

**这个测试本身就是"平台无关"的证明**：引擎这一份代码用 MinGW 编得出来、跑得通，
就说明它真的不依赖 Zephyr。副作用是它揪出了一个真问题 —— 引擎最初用了
`EBADMSG` / `ECONNRESET` 两个 POSIX 宏，MinGW 的 `errno.h` 里没有，
于是加了 `#ifndef` 兜底（见 `ota_engine.c` 头部注释）。

> **体积不能用来证明等价。** 上一节那种"改前改后 bin 完全一致"的手法这次不适用：
> 本轮同时修了 §11 的裁符号事故，基线从"缺了小半功能"的 837124 B 变成完整的
> 868916 B。跨过这个断点做体积对比没有意义，等价性只能靠上面 45 条断言。

---

## 7. 动手步骤：怎么在自己的工程里做同样的事

下面是本次实际走的顺序，不是"理论上应该"的顺序。**每一步之后都编译一次**，
这是唯一能控制风险的做法（本次共 13 轮编译，错误数 36 → 10 → 5 → 3 → 2 → 0 → 链接 1 → 0）。

### 步骤 1：先画依赖，不要先动代码

在拆分之前必须回答：**哪些函数的调用关系是朝向"数据"而不是"协议"的？**

做法：把 `main.c` 里的函数名 + 全局变量名列一遍，标出三类：

```
全局变量 → 谁写它、谁读它     （决定模块归属）
函数     → 谁调它            （决定层归属）
回调     → 谁注册它          （决定它必须和注册点同层还是同文件）
```

本工程得到的关键结论有三条，全部影响后面的切法：

1. `g_aps` / `g_creds` / `g_state` 被 handler 读得最多 → **必须提供读接口，不能只是搬文件**；
2. `wifi_event_handler` 是**唯一**的事件入口 → 它只能留在 L3（或者被 L3 调用）；
3. 19 个 `HTTP_RESOURCE_DEFINE` 的 `detail` 里 `.cb` 指向 handler →
   **`detail` 必须和 handler 同文件**（因为 handler 是 `static`，这是本次最大的一类错误来源）。

### 步骤 2：从叶子切起

叶子 = 没人依赖它、它只依赖别人的模块。本次的顺序：

```
cred → wifi_scan → portal_ap → time_sync → netcheck → led_status → dns_hijack → selftest
     → http_core → api_wifi → api_dev → page → shell_cmds
最后才是 main.c
```

**为什么先 cred**：它没有上游，只有 `settings` 一个下游依赖。切完立刻能编译，
用它验证"CMake 骨架 + 头文件 + include"这一套流程是否正确，**拿最小的模块试错**。

**为什么 main.c 最后**：它是唯一"什么都知道"的文件。它一改，
所有模块的接口都得定型 —— 改早了就得反复改。

### 步骤 3：CMake 骨架（可直接复制）

```cmake
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(<你的工程名>)

set(MY_COMMON ${CMAKE_CURRENT_SOURCE_DIR}/../../common)

# L0：自带线程、自带 settings handler、不认识"界面"
zephyr_library_named(my_eng)
zephyr_library_sources(src/<引擎>.c ${MY_COMMON}/src/<公共>.c)
zephyr_library_include_directories(PRIVATE src ${MY_COMMON}/include)

# L2：状态与动作，不吐 JSON、不认识 http.h
zephyr_library_named(my_svc)
zephyr_library_sources(src/<服务>.c)
zephyr_library_include_directories(PRIVATE src ${MY_COMMON}/include)
zephyr_library_link_libraries(my_eng)

# L1：协议 ↔ 快照的翻译
zephyr_library_named(my_api)
zephyr_library_sources(src/<接口>.c)
zephyr_library_include_directories(PRIVATE src)
zephyr_library_link_libraries(my_svc)

# L3：只剩 main.c
target_sources(app PRIVATE src/main.c)
target_link_libraries(app PRIVATE my_api)
```

三条纪律：

1. **每个 `zephyr_library_link_libraries` 紧跟在对应的 `zephyr_library_named` 之后**；
2. **include 一律 `PRIVATE`**；
3. **L3 用 `target_link_libraries(app PRIVATE ...)`**。

### 步骤 4：切一个，编一次

每次只切一个模块，跑一次构建。错误数应该是收敛的；如果某一轮**错误数上升**，
说明上一个模块的接口设计错了，回退它比继续改后面的更省时间。

看错误的顺序也有讲究：

```
error: 'xxx' undeclared            → 该模块引用了不属于它的全局，去 L2 加读接口
fatal error: xxx.h: No such file   → include 目录没给对（PRIVATE 不传递）
error: static declaration follows non-static → 头文件里声明了，定义还是 static
undefined reference to ...         → 链接顺序错，或函数在别层没定义
multiple definition of ...         → 与 SDK / blob 的符号冲突（加前缀）
```

### 步骤 5：最后改 main.c，并删掉它的一切实体逻辑

`main.c` 改完后只允许有三样东西：

```c
void wifi_event_handler(...)   /* 事件分派器：只做 switch → 调 L2/L1 的 ev_xxx() */
void ipv4_event_handler(...)
int  main(void)                /* 时间线：一行一个业务动作，按顺序 */
```

**判据：`main.c` 里出现任何一个 `static` 数据结构，就是切错了。**
本工程改完是 217 行，`static` 数据 0 个（全部下沉到模块的 `.c` 里）。

### 反模式清单

| 反模式 | 后果 |
|---|---|
| 把模块全塞进一个 `zephyr_library`，靠目录分层 | 等于没分，`-I` 依然全局 |
| include 用 `PUBLIC` | 边界自动消失，几个月后没人知道谁能 include 谁 |
| 为了"干净"把全局都改成 `extern` 声明在 `.h` | 只是把"同一编译单元"换成"跨库"，可见性反而更乱；应提供**读接口** |
| 在 L1 里 `k_mutex_lock(&ap_lock)` | 锁归 L2 所有，应提供 `scan_lock()/scan_unlock()` |
| 拆层同时改行为 | 出问题时无法判断是"拆错了"还是"改错了"。本次原则：**只挪位置，不改逻辑**（唯一例外是"读全局 → 调接口"） |
| 把 `main()` 里的时序搬进 `SYS_INIT` | 抹平硬件就绪时间线，症状是"偶发首包丢失 / 偶发 404" |

---

## 8. 本次真实踩过的坑（13 条，按性质分组）

### 8.1 切分本身造成的错位

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | `g_netcheck_*` / `g_dns_*` / `g_net_ip` 出现在 `time_sync.c` | 按**行区间**切分，符号恰好在区间边界上，被归到了相邻模块 | 用编译错误反查，手工搬回 `netcheck.c` |
| 2 | `NETCHECK_HOST` 落在 `time_sync.c` | 同上（`#define` 在区间尾部） | 搬回 |
| 3 | `ap_reopen_work` / `connect_defer_handler` / `led_tid` 的 `#endif` 落在 `portal_ap.c` | `#if` 在 A 模块、`#endif` 在 B 模块 | 补 `#endif` 到原文件 |
| 4 | `main()` 的函数体残片出现在 `shell_cmds.c` 开头 | 行区间覆盖了文件尾部 | 删除残片 |
| 5 | `page.c` 开头 175 行是**旧的 `wifi_event_handler` 全量副本** | `main.c` 重写后又保留了一份 | 删除（`page.c` 从 650 → 429 行） |
| 6 | `http_core.c` 里有孤儿声明 `void url_decode(char *s);` | 声明原本属于 `api_wifi.c` 的注释块，被切到别处 | 删除，改为在 `http_core.h` 里正常声明 |

**教训**：用脚本做"行区间切分"时，切完必须做两件事 ——
(a) 每个模块单独编译；(b) 全文搜索"孤儿 `#endif` / 孤儿前置声明 / 重复函数体"。

### 8.2 符号可见性

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 7 | `error: static declaration of 'url_decode' follows non-static declaration` | `http_core.h` 已声明为公共函数，定义处还留着 `static` | 去掉 `static` |
| 8 | `error: `sizeof` on incomplete type` / `ARRAY_SIZE` 报错 | 头文件里 `extern char json_header[];` 是不完整类型，`ARRAY_SIZE` 需要定长 | 声明成 `extern const struct http_header json_header[2];` |
| 9 | `error: 'scan_handler' undeclared here` | 五个 `detail` 被切到了 `page.c`，而它们的 `.cb` 指向 `api_wifi.c` 的 `static` handler | `detail` 与 handler **同文件**（搬回 `api_wifi.c`） |
| 10 | `error: 'g_aps' undeclared`（跨 4 个文件） | L1 直接读 L2 的 `static` 数组 | 加 `scan_lock/scan_entries/scan_count` 接口 |
| 11 | `error: assignment of read-only location '*link_err()'` | 把"写全局"的代码机械替换成了"调只读接口" | 那段整体换成 `link_request_connect(ssid, len, psk, len)` 一次调用 |

**教训**：#9 是"自注册对象与回调必须同编译单元"的通用规则。
只要 `.cb = 某个 static 函数`，这个对象就不能离开 handler 所在的 `.c`。

### 8.3 工具与环境

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 12 | `multiple definition of 'scan_start'`（链接期，来自 `libnet80211.a`） | 我们的 `scan_start()` 与 ESP-IDF net80211 blob 的导出符号重名 | 改名为 `wifi_scan_start()`。**拆库后第一次真正暴露这类冲突**（以前在同一库里也可能撞，但没人查） |
| 13 | 编译全绿、`bin` 不变，但配网页 / API / shell 全没了 | 自建 STATIC 库不在 `--whole-archive` 组，只有 section 注册的对象被按需丢弃 | 改用 OBJECT 库。**完整复盘见 §11**，这是本次代价最大的一坑 |

其他环境坑（与分层无关但会浪费时间）：

- 批量改名 `sta_iface → link_sta_iface()` 时产出过 `link_sta_iface()()` 和
  `link_link_sta_iface()` 两种畸形 —— **正则批量改名后必须 grep 一次特征模式**；
- `Get-Content` 读 16 万字节的源码时出现过"4171 行 GBK 乱码"的幻影版本，
  而文件实际是 4399 行纯 LF。**大文件用 hash / python 读，不要用 `Get-Content` 判断行数**。

---

## 9. 实测数据（同机、同 ccache、`-j 8`）

> 环境：Windows / `zephyr-sdk 1.0.1` / `xtensa-espressif_esp32s3_zephyr-elf` / ccache 开启。

### 9.1 改造后的构建耗时

| 操作 | 耗时 | 说明 |
|---|---|---|
| no-op（`ninja`，无任何改动） | **0.82 s** | `ninja: no work to do` |
| 改 1 个 L2 模块（`netcheck.c` 236 行） | **11.02 s** | 编 1 个 `.obj` + **全量重链接** |
| 改 L3（`main.c` 217 行） | **10.93 s** | 同上 |
| 改 common 组件（`ota_engine.c` 370 行） | **10.53 s** | 同上 |
| `build.ps1` 一轮（含 `west build --cmake-only`） | **51.1 s** | configure **38.8 s** + build 12.3 s |

**注意 `build.ps1` 的 configure 成本**：脚本每次都先跑 `west build --cmake-only`
（约 39 s），再跑 `ninja`。所以"感觉编译慢"经常是这 39 s 造成的，
**与改造无关，改造前后一样**。只想验证代码时直接 `ninja -C <builddir>` 即可（0.6 s 起步）。

### 9.2 改造前 vs 改造后（诚实对照）

| 指标 | 改造前（单文件） | 改造后（4 个 OBJECT 库 + 分层目录） | 变化 |
|---|---|---|---|
| no-op 增量 | 1.64 s（历史实测） | 0.82 s | 略快（目标判定更细） |
| 改一个 `.c` 的增量 | 6.59 s（改 4399 行 `main.c`） | 10.5–11.0 s（改 145–822 行模块） | **慢约 4 s** |
| 干净全量 | 216.7 s（历史实测） | 未重测（`prj.conf`/overlay 未变，机制相同） | 不变 |
| `zephyr.bin` | 未留档（当时的产物没保存） | 868916 B | 见 §9.3 的口径说明 |

**为什么"改一个文件"反而慢了约 4 s？**（这个结论必须说清楚，否则就是骗人）

- 单文件**编译**确实变快了：编 145–822 行 vs 编 4399 行，ccache 命中率也更高；
- 但**链接**是每轮都要做的固定成本（650 个对象重新链接一次，约 8–9 s 是纯链接）；
- 改造前的 6.6 s 是在"改 4399 行 main.c、其余库不变"的场景下测的，
  与现在的场景并不严格可比。**如实记录：差值约 4 s，本轮没有把它归因到某个具体项。**
  （曾经归因于"3 个静态库的归档开销"，但那三个库已改成 OBJECT 库、根本不再归档，
  所以这条旧解释已作废。）

**结论：拆层不是为了编译更快。** 它买到的是 §4.1 那张表里的东西。
如果哪天觉得 9 s 太长，应该动的是 `prj.conf`/overlay（全量的真正触发器）
和链接策略，不是把层合并回去。

### 9.3 体积

| 阶段 | `zephyr.bin` | 可信度 |
|---|---|---|
| 单文件时代（4399 行 `main.c`） | 未留档 | ⚠️ 当时的构建产物没有保存，不能事后补写数字 |
| 拆层后（STATIC 库版本） | 837124 B | ❌ **缺功能的镜像**：19 条资源、页面数据、shell 线程都被裁掉了（§11） |
| 修好裁符号 + 目录收拢 + OTA 后端迁移 | **868916 B** | ✅ 完整（段里 20 条资源、页面数据齐） |

**为什么要专门解释这张表。** 初版把"拆层前后都是 837124 B"当成"零行为改动"的证据，
但这两次构建里其实**都缺了同一批对象**（所以才"一样"）。修好之后 bin 变成 868916 B，
差出来的 32092 B 不是新增代码，而是**原本就该在、却一直没在**的页面数据与 handler 代码。
**教训：体积相等只能证明"两次都被裁得一样狠"，不能证明功能完整。**

拆层本身不引入运行时开销：没有虚表、没有额外拷贝、没有堆分配。
但"静态库化"这个动作会改变链接语义，见 §11。

---

## 10. 还没做完的事

按优先级（截至 2026-11-16，§10 的前三项已经做完，保留原文以便对照）：

1. ~~**`ota.c` 迁移到 `struct ota_backend`**~~ —— **已完成**，见 §6。
   `ota_do_upgrade()` 381 → 90 行，平台侧只剩四个回调 + `g_hop` 提到文件作用域。
2. ~~**目录收拢 `src/<layer>/`**~~ —— **已完成**：`src/l0` / `src/l1` / `src/l2`，
   文件用 `git mv`（已跟踪的 5 个）+ 等价移动（本轮新切的 12 个 untracked），
   全部 sha256 逐一比对**内容零改动**。`CMakeLists.txt` 的 `-I` 按层收窄。
   负向实证：往 `src/l2/cred.c` 注入 `#include "http_core.h"` →
   `fatal error: http_core.h: No such file or directory`（`-I` 里确实没有 `src/l1`）。
3. ~~**真机自检**~~ —— 设备连不上 WiFi，改为**不需要设备的两道关卡**：
   ELF 段内符号计数（20 条资源在位，§11.5）+ 主机 45 条断言（§6.6）。
   **仍需补的真机项**：设备恢复联网后跑一次 OTA 全链路（正常 / 断电续传 / 校验失败）。
4. **变更记录** —— 见 `docs/变更记录/`。

**本轮已知的行为差异（不打算改回去，但必须登记）：**

| 差异 | 影响 | 说明 |
|---|---|---|
| `t_verify` 计时区间变宽 | 只影响日志数字 | 现在包住 flush + hash_finish + flash_img_check；旧实现只包 check |
| 早期收包失败（`pre` 里那截）的 err_msg 文案 | 只影响日志 | 统一由后端 `err_msg` 提供，不再写 `(early body)` 后缀 |
| `OTA_CHUNK` 宏从 app 消失 | 无 | 块大小语义搬到引擎的 `OTA_SESS_CHUNK`（同值 2048） |

---

## 11. 最贵的一课：拆库把功能"静默裁掉"（2026-11-16 复盘）

这一节不是设想，是**已经发生过的真实事故**，而且当时所有"编译通过"的信号都是绿的。

### 11.1 症状

把 4 个 `zephyr_library_named` 拆好之后，`ninja` exit 0、`zephyr.bin` 837124 B、
无 error 无 warning。直到做"19 个 HTTP 资源是否全在"的静态核对时才发现：

```
elf 里 http_resource_desc 符号：0 个
段 ._http_resource_desc_portal_service.static.*：空
```

也就是说这份固件**没有配网页、没有 API、没有 shell 命令线程**。
如果直接烧上去，现象会是"能连上 AP、访问任何路径都 404"，
而排查方向会一路错到 HTTP 服务配置上去。

### 11.2 证据链（三条命令定位）

**① 链接行：库落在 whole-archive 之外**

`build/ninja` 的 `LINK_LIBRARIES` 里：

```
... -Wl,--whole-archive  <37 个 Zephyr 库>  -Wl,--no-whole-archive  zephyr/kernel/libkernel.a  ...  libportal_api.a libportal_svc.a libportal_eng.a  ...
```

我们自己建的三个库，**在 `--no-whole-archive` 之后**。

**② map 文件：整个目标文件都没进**

```
zephyr.map 里 'http_core' 出现次数 = 0
http_resource_desc_portal_service_area
    0x3c0ac500  _http_resource_desc_portal_service_list_start = .
    *(SORT_BY_NAME(...))            ← 一条记录都没有
    0x3c0ac500  _http_resource_desc_portal_service_list_end = .
```

**③ 符号表对比：两份 elf 差了 189 个符号 / 58888 字节**

| 只在"完整"那份里出现的符号 | 大小 |
|---|---|
| `index_html`（配网页 HTML） | 18646 B |
| `user_html` | 8544 B |
| `_k_thread_stack_sh_cmd_tid` | 6144 B |
| `g_wlog_json` / `g_sh_json` / `g_sh_out` … | 2176 / 2144 / 2048 B |
| `connect_handler` / `status_handler` / `shell_handler` / `forget_handler` | 数百 B 各 |

`zephyr.bin` 因此差了 **32092 B**（`index_html`+`user_html` 走 flash，JSON 缓冲和线程栈走 RAM，
所以符号总量大于 bin 差值）。**根因很清楚：凡是"只往 section 里注册、没有符号被引用"的对象，
在静态库里都属于"未使用"，被链接器按需丢弃。**

### 11.3 为什么 `zephyr_library_named` 不等价于 `app`

`extensions.cmake:878` 那段 function 长这样：

```cmake
function(zephyr_append_cmake_library library)
  if(TARGET zephyr_pre0)
    message(WARNING
      "zephyr_library() or zephyr_library_named() called in Zephyr CMake "
      "application mode. `${library}` will not be treated as a Zephyr library.")
  endif()
  set_property(GLOBAL APPEND PROPERTY ZEPHYR_LIBS ${library})
endfunction()
```

`find_package(Zephyr)` 返回之后，应用的 `CMakeLists.txt` 就处在 **application mode**
（`zephyr_pre0` 目标已经存在）。此时 `zephyr_library_named` 做了三件事：

1. `add_library(${name} STATIC "")` —— 建了一个**普通静态库**；
2. `zephyr_append_cmake_library(${name})` —— 只打一条 warning，**不会**把它塞进
   最终 `--whole-archive` 组；
3. `target_link_libraries(${name} PUBLIC zephyr_interface)` —— 只为拿编译选项。

而改造前的写法是 `target_sources(app PRIVATE src/*.c)`：**对象直接进 `app` 这个可执行目标**，
不存在"按需挑选"这一步，所以从来没有这个问题。

一句话：**静态库会挑，可执行目标不挑。**

### 11.4 三种修法，为什么最后选了 OBJECT

| 方案 | 结果 | 原因 |
|---|---|---|
| `zephyr_link_libraries(-Wl,--whole-archive portal_eng portal_svc portal_api -Wl,--no-whole-archive)` | ❌ `ninja: error: dependency cycle: heap_constants.h -> libportal_api.a -> …` | `zephyr_link_libraries` 挂到 `zephyr_interface`，连带被 `offsets`/`zephyr_pre0` 继承，和自动生成的头形成环 |
| `target_link_libraries(app PRIVATE -Wl,--whole-archive … -Wl,--no-whole-archive)` | ❌ pre0 链接期 `multiple definition of calloc / malloc / free …` | 这组 flag 插进 `app` 的 link interface 后，破坏了 Zephyr 给 libc 排的 group 语义（同一个 libc 被扫两遍） |
| **`add_library(<name> OBJECT "")` + `target_sources(app PRIVATE $<TARGET_OBJECTS:…>)`** | ✅ 通过，`bin` 868916 B | 回到"对象直接进 app"的语义，绕开归档挑选；每个 target 仍然有自己的 `-I` |

最终写法（`CMakeLists.txt` 四段都是这个形状）：

```cmake
add_library(portal_svc OBJECT "")
target_link_libraries(portal_svc PUBLIC zephyr_interface)   # 拿 zephyr 的编译选项/宏
target_sources(portal_svc PRIVATE src/l2/cred.c src/l2/wifi_scan.c …)
target_include_directories(portal_svc PRIVATE
        ${APP_SRC}/l2 ${APP_SRC}/l0 ${APP_SRC} ${KORVO_COMMON}/include)
target_link_libraries(portal_svc PUBLIC portal_eng)          # 层间依赖，仅用于 -I 传递

# L3：对象在这里一次性进 app
target_sources(app PRIVATE
        $<TARGET_OBJECTS:portal_eng>
        $<TARGET_OBJECTS:portal_svc>
        $<TARGET_OBJECTS:portal_api>)
```

**`target_link_libraries(app PRIVATE portal_api)` 这行必须删掉/不用指望它**：
OBJECT 库不会把自己的对象交给下游（只有 STATIC/INTERFACE 会），必须显式 `TARGET_OBJECTS`。
忘了这一步的表现不是链接失败，而是 **app 里找不到任何 portal 符号** —— 又是一个"报错很晚"的坑。

### 11.5 30 秒自检：你这套拆法有没有同样的病

按顺序跑，任意一条不合预期就别烧板子：

```powershell
# ① 链接行：自建库是否落在 --no-whole-archive 之后
Select-String -LiteralPath build\<board>\<app>\build.ninja -Pattern 'whole-archive'

# ② 段内符号数（把 portal_service 换成你的 service 名）
xtensa-...-objdump.exe -t zephyr\zephyr.elf | Select-String 'http_resource_desc_<svc>'

# ③ 段大小：19 条 × sizeof(struct http_resource_desc) 应该对得上
xtensa-...-objdump.exe -h zephyr\zephyr.elf | Select-String 'http_resource_desc'
```

本工程修复后的实测：段大小 **`0xa0` = 160 B = 20 条 × 8 B**（19 条业务资源 + 1 条
`portal_shell`，后者在 `CONFIG_SHELL_BACKEND_DUMMY` 打开时才注册）。

### 11.6 这一节的真正教训

1. **"编译通过"只能证明语法和链接，不证明功能还在。** 自注册类组件（HTTP 资源、
   shell 命令、`K_THREAD_DEFINE`）必须额外数符号。
2. **静态库化不是零成本重构。** 它把"没人引用就丢弃"这条链接器规则引进了你的构建，
   而 Zephyr 有一半的机制恰好依赖"没人引用但必须存在"。
3. **框架的 warning 要读。** `zephyr_append_cmake_library` 那条 warning 本轮一早就出现了，
   当时被当成噪音跳过，代价是两次错误修法和约一小时。

---

## 附：本次改造的完整文件清单

**新增（14 个 `.c` + 15 个 `.h`，均已按层收拢）**

```
src/l2/cred.c        cred.h          凭据表 + NVS
src/l2/wifi_scan.c   wifi_scan.h     扫描缓存 + 锁
src/l2/portal_ap.c   portal_ap.h     SoftAP + 换信道 + client 记账
src/l2/wifi_link.c   wifi_link.h     链路状态机 + 重连 + RSSI 采样
src/l2/time_sync.c   time_sync.h     SNTP + HTTP Date 兜底
src/l2/netcheck.c    netcheck.h      DNS + 出网自检
src/l2/led_status.c  led_status.h    呼吸灯线程 + 相位
src/l2/dns_hijack.c  dns_hijack.h    UDP :53 通配
src/l1/selftest.c    selftest.h      上电 6 条自检
src/l1/http_core.c   http_core.h     service + 19 条 resource 注册 + 守卫宏
src/l1/api_wifi.c    （共用 http_core.h）  扫描/状态/连接/凭据/客户端/RSSI
src/l1/api_dev.c     （共用 http_core.h）  OTA / ops / shell / 槽位
src/l1/page.c        pages.h         三张页面 + captive
src/l1/shell_cmds.c  （无独立头）    串口命令
```

**新增到 `applications/common`（跨工程复用）**

```
include/ota_engine.h   OTA 协议层接口：协议原语 + 门禁 + struct ota_backend + 会话
src/ota_engine.c       全部实现（约 370 行，平台无关，可上 PC 单测）
test/test_ota_engine.c 主机回归（45 条断言，MinGW gcc 可直接编）
```

**重写**

```
src/main.c             4399 行 → 217 行（事件分派 + 时间线）
CMakeLists.txt         单库 → 4 个 OBJECT 库（按层 -I）+ TARGET_OBJECTS + sections-rom.ld
```

**被改动的**

```
src/l0/ota.c           删掉 82 行协议层（→ ota_engine）、381 行 ota_do_upgrade（→ 后端表 + 90 行主流程）
```

**收拢前后位置对照**

```
src/main.c  src/portal_cfg.h  src/user_page.html   （跨层共享 / 页面资源，留在根）
src/l0/{ota.c,ota.h,ops.c,ops.h}
src/l1/{http_core,api_wifi,api_dev,page,shell_cmds,selftest}.c  + pages.h/http_core.h/selftest.h
src/l2/{cred,wifi_scan,portal_ap,wifi_link,time_sync,netcheck,led_status,dns_hijack}.{c,h}
```
