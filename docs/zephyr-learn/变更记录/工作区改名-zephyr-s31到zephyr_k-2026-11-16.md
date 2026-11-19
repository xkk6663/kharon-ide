# 工作区改名：`zephyr-s31` → `zephyr_k`（2026-11-16）

## 为什么改

`zephyr-s31` 这个名字容易被读成"只服务 ESP32-S31 的工作区"，但实际上它同时装着 ESP32-S3、ESP32-S31、STM32F411、STM32F103 四条线的模块与 SDK 目标。改名成 `zephyr_k` 只表示它是**这颗 Zephyr 树的目录**，不再绑定某个芯片。

## 改了什么

| 对象 | 动作 |
|---|---|
| 工作区目录 | `Move-Item zephyr-s31 → zephyr_k`（同一盘符，秒级完成，`.west/`、五个模块的 `.git` 全部原样保留） |
| `.gitignore` | 忽略行改为 `zephyr_k/`，旧的 `zephyr-s31/` 一并留着（防止老路径残留目录被误入库），加了一行改名说明 |
| 脚本与配置 | `env.ps1`、`tools\build.ps1`、`tools\check-layout.ps1`、`tools\pull-stm32-modules.ps1`、`tools\speed_*.ps1`、`tools\fetch_*.ps1`、`tools\defender_exclude.ps1`、`tools\commit_wifi_milestone.ps1`、`tools\_rollback-layout-2026-11-14.ps1` 全部替换为新路径 |
| 应用侧 | 8 个 `CMakePresets.json`（`apps\_template`、`apps\esp32s31\korvo_*`、`apps\esp32s3\blinky-s3`、`input_btn`、`apps\stm32f4\f411_breath`）+ `apps\esp32s3\s3_wifi_portal` 的 `README.md` / `ACCEPTANCE.md` / `mcuboot.conf` |
| 文档（现状类） | 根 `README.md`、`docs\README.md`、`docs\参考\`（5 篇）、`docs\教程\`（12 篇，含 15 组三篇）、`docs\课程同款\`（11 篇）、`tools\README.md` |
| 未动 | `docs\变更记录\` 的 8 篇 + 1 个迁移清单 JSON：那是历史快照，正文里的 `zephyr-s31` 是当时的事实，按规则不回溯改（本篇就是它的后记） |

合计 **66 个文件**、约 300 处替换；`.west\config` 与 `west.yml` 里没有绝对路径，不需要动。

## 验证

| 项 | 结果 |
|---|---|
| `west list`（在新目录里跑） | 70 个项目，四个已落地模块 revision 不变：`hal_stm32@2fd0502`、`cmsis_6@1c1840a`、`mbedtls@c43f34b`、`tf-psa-crypto@765af96` |
| F103 `-Pristine` 复编 | `configure` 47 s / ninja 走完 154 步，**FLASH 17104 B（26.10%）/ RAM 5232 B（25.55%）**，与改名前一字不差 |
| 产物落点 | 仍是 `build\stm32f1\f103_breath\default`，`check-layout.ps1` = 5/5 |

## 踩到的坑

1. **双 BOM**：批量替换时用 `UTF8.GetString` 读、再带 BOM 写回，原文件里的 BOM 被当成正文首字符保留，写出来变成"两个 BOM"。`pwsh` 执行 `build.ps1` 直接报「无法将 `﻿#` 项识别为 cmdlet」。修法：按字节剥掉重复 BOM（本轮 11 个文件中招，已全部修正并按 `pwsh 7` 习惯写成无 BOM）。
2. **旧 build 缓存全部失效**：`build\**\CMakeCache.txt` 里写的是绝对路径，改名后 configure 会认为源码目录变了。规则同以前——**换路径后第一次必须 `-Pristine`**；不重建也不会"半对半错"，只会直接报错。
3. **`env.ps1` 会 `Set-Location` 到工作区**：`build.ps1` 里已经用 `$invokeDir` 把 `-App` / `-BuildDir` 解析成绝对路径（第 41~60 行），所以从仓库根直接跑仍然有效；自己写脚本时别依赖 cwd。

## 附带处理：失效的 sysbuild 缓存

改名后 `check-layout.ps1` 从 5/5 掉到 4/5，两条 FAIL 都指向同一处：`build\esp32s3\s3ota`（sysbuild 顶层 + `mcuboot\` 子目录）的 `CMakeCache.txt` 里记着旧的 `zephyr-s31\zephyr\share\sysbuild` 路径。这个缓存已经**必然失效**（源码目录不存在了），重建只能靠重新编译；按"这轮不编译"的要求，整体挪到 `tmp-stale-build-20261116\s3ota`（`tmp-*/` 已被忽略，20 个 `fw_v*.signed.bin` 历史产物一个没删），巡检随之回到 5/5。

## 没做

- ESP32 侧没重编（按约定由你手动）；`build\esp32s3\*`、`build\esp32s31\*` 里的旧缓存同理，第一次编之前建议 `-Pristine`。
