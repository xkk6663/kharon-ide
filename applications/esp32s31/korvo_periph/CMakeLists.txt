cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(korvo_periph)

# 纯应用层：业务代码都在本目录。板级差异 dtsi 仍来自 applications/common；
# 芯片无关的呼吸灯模块由 Zephyr module `elab-app-common` 提供。
target_sources(app PRIVATE src/main.c)
