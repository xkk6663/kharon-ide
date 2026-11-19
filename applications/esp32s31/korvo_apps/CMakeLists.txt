cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(korvo_apps)

# store / breath_sync 由 Zephyr module `elab-app-common` 提供。
# store 依赖 CONFIG_NVS，模块内按配置条件编译；本工程只需保持 NVS 打开。
target_sources(app PRIVATE src/main.c)
