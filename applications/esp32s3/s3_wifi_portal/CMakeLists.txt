# 镜像头版本（重要，别又踩回来）
#
# imgtool 不给 --version 时，镜像头里的版本恒为 0.0.0，于是「slot0 是哪一版、
# slot1 是哪一版」从镜像头根本读不出来，任何 bootloader 级版本判断
# （NO_DOWNGRADE、DIRECT_XIP 挑高版本、回滚前比新旧）全部失效。
#
# Zephyr 的干净入口是 CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS（modules/Kconfig.mcuboot:160）。
# 坑在 sysbuild：cmake/modules/kconfig.cmake:303-317 在 SYSBUILD 模式下**只读
# sysbuild_cache 里名字形如 <镜像名>_CONFIG_xxx 的变量**，app 自己 CMakeLists 里的
# set(CONFIG_xxx CACHE ...) 会被直接忽略（我试过，.config 里仍是 ""）。
# 所以必须在构建命令行注入，镜像名就是工程名 s3_wifi_portal：
#
#   west build --sysbuild ... -- `
#     -Ds3_wifi_portal_CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS="--version 1.<OTA_LOCAL_VERSION>.0"
#
# 单一事实来源仍是 src/ota.h 的 OTA_LOCAL_VERSION；构建后用
# tools\img_hdr.py 校验镜像头版本，对不上就别推板子。

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(s3_wifi_portal)

# 芯片无关的公共模块由 Zephyr module `elab-app-common` 提供（breath_sync / ota_engine）。
# 本工程只负责时间、刷灯与 flash 后端；抽象接口见 elab-app-common/include/ota_engine.h。
set(APP_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src)

# ======================================================== 四层 = 四个 OBJECT 库
#
#   目录                      target           允许的 -I（只列自己 + 下层）
#   src/l0/  ota ops          portal_eng       l0, src, common
#   src/l2/  服务             portal_svc       l2, l0, src, common
#   src/l1/  接口             portal_api       l1, l2, l0, src
#   src/main.c                app              l1, l2, l0, src
#
# "-I 只给到下层"就是物理防火墙：L2 想 include "http_core.h" 时编译器找不到该头
# （src/l1 不在它的 -I 里），于是"反向依赖"从纪律问题变成构建失败。
# 而它下面各层的头仍然可见 —— L2 调 ops_* / ota_* 这类方向是对的，不拦。
#
# 为什么是 OBJECT 而不是 STATIC（这一条很容易看起来无关紧要，实际会吃掉功能）：
#
#   HTTP_RESOURCE_DEFINE / SHELL_CMD_REGISTER / K_THREAD_DEFINE / SYS_INIT 都只
#   "往 linker section 里丢一条记录"，没有任何 C 代码按名字引用它们。
#   静态库的函数/数据按"有没有被引用"挑选 obj，这些只有注册表、没人引用的 obj
#   会被安静丢掉：链接不报错、镜像照出，跑起来就是 404 / 没有 shell 命令。
#
#   实测（2026-11-16，STATIC 版本）：elf 里 http_resource_desc 一个都不剩，
#   段 ._http_resource_desc_portal_service.static.* 为空；
#   build.ninja 的链接行里 libportal_*.a 落在 -Wl,--no-whole-archive 之后
#   （zephyr_library_named 在"application mode"下不会进 ZEPHYR_LIBS，见
#   extensions.cmake:878 zephyr_append_cmake_library 的那条 warning）。
#
#   用 OBJECT 库就回到改造前的语义：所有对象直接进 app，一个都不少，
#   同时每个 target 仍然有自己的 -I，防火墙照样成立。
#
# 编译成本：改一个 .c 只重编那一个文件再重链（实测 9–10 s / no-op 0.6 s），
# 分层的代价可以忽略 —— 真正的全量触发器是 prj.conf 和 *.overlay。

# ---------------------------------------------------------------- L0 引擎层
# ota(平台后端) / ops(重启记账) / breath_sync(灯规范) / ota_engine(OTA 协议层)
add_library(portal_eng OBJECT "")
target_link_libraries(portal_eng PUBLIC zephyr_interface)
target_sources(portal_eng PRIVATE
	src/l0/ota.c
	src/l0/ota_cloud.c
	src/l0/cloud_ops.c
	src/l0/ops.c
)
target_include_directories(portal_eng PRIVATE
	${APP_SRC}/l0
	${APP_SRC}
)

# ---------------------------------------------------------------- L2 服务层
# 凭据 / 扫描缓存 / 链路状态机 / SoftAP / 对时 / 出网自检 / 呼吸灯 / DNS 劫持。
# 这一层不认识 http.h，也不吐 JSON —— 它只提供"读快照 + 发动作"。
add_library(portal_svc OBJECT "")
target_link_libraries(portal_svc PUBLIC zephyr_interface)
target_sources(portal_svc PRIVATE
	src/l2/cred.c
	src/l2/wifi_scan.c
	src/l2/portal_ap.c
	src/l2/wifi_link.c
	src/l2/time_sync.c
	src/l2/netcheck.c
	src/l2/led_status.c
	src/l2/led_ctrl.c
	src/l2/dns_hijack.c
)
target_include_directories(portal_svc PRIVATE
	${APP_SRC}/l2
	${APP_SRC}/l0
	${APP_SRC}
)
# 服务层要记账（ops）、判"OTA 是否在写"（ota）、喂呼吸灯（breath_sync）
target_link_libraries(portal_svc PUBLIC portal_eng)

# ---------------------------------------------------------------- L1 接口层
# HTTP 资源 + 串口命令 + 上电自检：只做"协议 ↔ 快照"的翻译，不持有状态。
add_library(portal_api OBJECT "")
target_link_libraries(portal_api PUBLIC zephyr_interface)
target_sources(portal_api PRIVATE
	src/l1/http_core.c
	src/l1/api_wifi.c
	src/l1/api_dev.c
	src/l1/page.c
	src/l1/shell_cmds.c
	src/l1/selftest.c
)
target_include_directories(portal_api PRIVATE
	${APP_SRC}/l1
	${APP_SRC}/l2
	${APP_SRC}/l0
	${APP_SRC}
)
target_link_libraries(portal_api PUBLIC portal_svc)

# ---------------------------------------------------------------- L3 编排层
# app 只剩 main.c：注册事件回调、按顺序上电。顺序是产品行为，只属于这一层。
target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE
	${APP_SRC}/l1
	${APP_SRC}/l2
	${APP_SRC}/l0
	${APP_SRC}
)
# 三个 OBJECT 库的对象在这里一次性进 app。
# 注意不能写成 target_link_libraries(app PRIVATE portal_api)：OBJECT 库不会
# 自动把自己的对象交给下游（只有 STATIC/INTERFACE 会），必须显式取 TARGET_OBJECTS。
target_sources(app PRIVATE
	$<TARGET_OBJECTS:portal_eng>
	$<TARGET_OBJECTS:portal_svc>
	$<TARGET_OBJECTS:portal_api>
)

# HTTP server 的资源是"按服务名分组"的 iterable section：
#   HTTP_RESOURCE_DEFINE(name, portal_service, ...) 把资源放进
#   http_resource_desc_portal_service 段，服务用其 _start/_end 遍历。
# 段名是动态拼出来的，工具链不会预先生成，必须在应用里显式声明，
# 否则链接期报 undefined reference to `_http_resource_desc_portal_service_list_start'。
zephyr_linker_sources(SECTIONS ${CMAKE_CURRENT_SOURCE_DIR}/sections-rom.ld)
