# zephyr_modules/elab-app-common/CMakeLists.txt
#
# 应用层公共代码的 Zephyr module 宿主。
# 纯 C（breath_sync / ota_engine）始终编译；store 依赖 NVS，按 CONFIG_NVS 条件编译。
# 这样应用只声明“我依赖 elab_app_common”，不再用 ../../common 的相对路径。

zephyr_library_named(elab_app_common)
zephyr_library_sources(
  src/breath_sync.c
  src/ota_engine.c
)
zephyr_library_sources_ifdef(CONFIG_NVS src/store.c)
zephyr_include_directories(include)
