cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(korvo_nvs)

# store 由 Zephyr module `elab-app-common` 提供；本工程保持 NVS 配置打开。
target_sources(app PRIVATE src/main.c)

# 本工程连"公共板级片段"都不需要：它不碰 PA_CTRL / WS2812 / ADC / I2C，
# 所以没有 app.overlay（只有 NVS + shell）。
