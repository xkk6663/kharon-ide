# 旧仓 `dsh-elab-flow` 清理记录（2026-11-20）

> 目标：`kharon-ide` / `zephyr-workspace` 已承接 Zephyr 产品线后，把旧仓恢复到
> **classic 多芯片 + DSH 插件** 的状态，不再保留 Zephyr 开发工作区。
>
> 结论：旧仓当前 `git status` 为 0 改动，`HEAD` 仍是 `e34fe07`；清理前全部内容
> 已固化在本地备份分支 `backup/pre-zephyr-clean`（`e01af33`），未推送、未合并。

---

## 0. 清理前是什么状态

| 项 | 清理前 |
|---|---|
| `HEAD` | `e34fe07 chore: 忽略宣传片制作工作区与本机绝对路径的接入文件` |
| 分支 | `main`，比 `origin/main` 多 5 个提交 |
| 已跟踪的 Zephyr 文件 | **0 个**（Zephyr 全在未提交工作区） |
| 未提交项 | **867 个** |
| 其中已删除的 classic 示例 | `examples/STM32F103C8T6/**` 616 个文件 |
| 暂存区 | 45 个 `services/elab/**` 文件被暂存为“重命名到新分层” |
| 工作区体积 | 约 2.4 GB（含 `west/`、`build/`、`.deps/`） |

关键判断：**旧仓的 Zephyr 不是提交历史的一部分，而是 867 个未提交项。**
所以“回滚”不是 `git reset` 到某个旧提交，而是“恢复 tracked 文件 + 清理未跟踪迁移产物”。

---

## 1. 备份（可完整回退）

清理前先做了两件事：

1. 保存状态清单与补丁到
   `kharon-ide/.work/dsh-elab-flow-cleanup-backup/`：
   - `status-before.txt`（867 项完整路径）
   - `unstaged-before.patch`（34 MB）
   - `staged-before.patch`（8 KB）
2. 在旧仓创建本地备份分支：

```text
backup/pre-zephyr-clean    e01af33
main                       e34fe07
```

该分支包含备份时的**全部工作区内容**（2013 个文件），包括：

- Zephyr 应用、芯片包、provider、`services/kharon`、`zephyr_modules`；
- DSH 插件未提交的 MCP 改动；
- 旧仓本地的 `.vscode/` 等未跟踪文件。

> 该分支只在本地，没有 push。需要找回任何清理前文件：

```powershell
git -C C:\Users\xiao1\Desktop\ELAB\dsh-elab-flow switch backup/pre-zephyr-clean
# 或只取单个文件：
git -C C:\Users\xiao1\Desktop\ELAB\dsh-elab-flow restore --source=backup/pre-zephyr-clean -- path/to/file
```

---

## 2. 清理动作

### 2.1 恢复 tracked 文件到 classic 基线

```powershell
git -C C:\Users\xiao1\Desktop\ELAB\dsh-elab-flow checkout -- .
```

恢复的内容：

- 616 个被删的 `examples/STM32F103C8T6/**` 文件；
- `docs/design` 5 篇、`docs/reports` 9 篇被删文档；
- `cockpit/web/src/primitives/Menu.*`；
- `elab.host.yaml`；
- `services/elab/**` 45 个被改成“12 行 re-export 桩”的 classic 实现，恢复为完整实现；
- `cockpit/**`、`packages/cockpit-ui-core/**` 中被 Zephyr 模式切换/矩阵/实测内存改过的文件；
- DSH 插件 tracked 改动（MCP 出口、tools facade）一并回退到 `HEAD`。

> 说明：这一步是“回到 classic 基线”，不是“只删 Zephyr”。因此 DSH 插件里
> 与 Zephyr 无关的 MCP 未提交改动也会从工作区消失；它们只在备份分支里。

### 2.2 删除未跟踪的 Zephyr 迁移产物

`git clean -fd` 清掉全部未跟踪文件，随后手动删除 `git clean` 因嵌套仓库而跳过的
Zephyr 工作区。

| 类别 | 删除内容 |
|---|---|
| Zephyr 应用 | `applications/**`（138 个文件：`_template`、ESP32/S31/F411 应用、`common`） |
| 芯片包 / provider | `packs/official/zephyr/**`、`providers/official/zephyr/**` |
| 旧仓内核新分层 | `services/elab/{api,core,debug,platform,serial,workflow}/**`、`services/kharon/**`、`westexec.py`、`westws.py`、`wszephyr.py`、`zephyr.py`、`zephyr_meta.py`、`envgen.py`、`kharon_probe.py` |
| 旧仓驾驶舱新分层 | `cockpit/api/**`、`cockpit/core.py`、`cockpit/debug/**`、`cockpit/platform/**`、`cockpit/run/**`、`cockpit/profiles/kharon.yaml` |
| 前端 Zephyr 页面 | `cockpit/web/src/matrix/**`、`cockpit/web/src/west/**`、`cockpit/web/src/zephyr/**` |
| 工程 / 模板 | `projects/kharon/**`、`manifests/**`、`patches/**`、`templates` 下 Zephyr 模板 |
| 文档 | `docs/zephyr-learn/**`、`docs/current/**`、`docs/archive/**`、`docs/tutorials/10..14-Zephyr*.md`、`docs/tutorials/assets/{kharon,market,reports,zephyr-debug}/**`、`docs/design/*Kharon*`、`*Zephyr替代路线*`、`*仓库结构地图*`、`*架构优化与目录重构*` |
| 测试 | `tests/architecture/**`、`tests/contract/**`、`test_build_provider.py`、`test_cockpit_mode.py`、`test_envgen.py`、`test_events_export.py`、`test_kharon_probe.py`、`test_rtos_zephyr.py`、`test_wszephyr.py`、`test_zephyr_debug.py` |
| 入口 / 配置 | `kharon`、`kharon.cmd`、`pytest.ini`、`config/**`、`contracts/**`、`scripts/**`、`tools/**`、`.vscode/**` |
| Zephyr 工作区 / 构建 | `west/**`（含未清干净的嵌套仓库）、`zephyr-learn/**`、`build/**` |
| 依赖缓存 | `.deps/**`（被 `git clean` 删成半个 venv，已整体移除）、`.pytest_cache/**` |

### 2.3 为什么 `.deps` 也删了

`git clean` 删掉了 `.deps/venv` 的一部分包，留下的是不完整 venv；继续保留会让人
误以为依赖可用。旧仓的 `elab.cmd` / `cockpit.cmd` 本来就用系统 `python`（可用
`ELAB_PYTHON` 覆盖），不依赖这个 venv，所以整体移除。

需要旧仓测试环境时，用系统 Python 重建即可：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\dsh-elab-flow
python -m venv .deps\venv
.\.deps\venv\Scripts\python.exe -m pip install -r requirements-dev.txt  # 若仓内存在
```

---

## 3. 清理后状态

```text
## main...origin/main [ahead 5]
```

| 项 | 清理后 |
|---|---|
| `git status` | **0 改动** |
| tracked Zephyr 文件 | **0 个** |
| tracked `examples` | **1462 个**（616 个 F103 示例已恢复） |
| 顶层目录 | `.agents .github .screenshots .work .workbuddy ci cockpit components docs examples market packages packs projects providers services site skills templates tests toolchains` |
| `west/` / `build/` / `zephyr-learn/` | 已删除 |
| 入口验证 | `elab.cmd --help` 正常；`elab.cmd list` 能看到 `st/stm32`、`artery/at32`、`espressif/esp32` 芯片包与 `STM32F103C8T6` 等 classic 工程 |

`elab.cmd list` 实测节选：

```text
packs:
  artery/at32          v0.1.0    installed
  espressif/esp32      v0.1.0    installed
  st/stm32             v0.1.0    installed
chips:
  st/stm32f103xb             cpu=cortex-m3
  st/stm32f411ceux           cpu=cortex-m4
projects:
  STM32F103C8T6  A-class  chip=st/stm32f103xb
  stm32f411ceu6  A-class  chip=st/stm32f411ceux
```

---

## 4. 需要知道的副作用

1. **旧仓 `main` 没有新提交**：清理只是把工作区恢复干净，`HEAD` 仍是 `e34fe07`，
   比 `origin/main` 多 5 个提交（这 5 个提交本来就没 push）。
2. **DSH 插件的 MCP 未提交改动不在工作区了**：它和 Zephyr west 工具改在同一批
   tracked 文件里，无法只保留 MCP 而完全去掉 Zephyr。需要时从备份分支取。
3. **`.deps` 已移除**：旧仓用系统 Python；如果要跑 pytest，先重建 venv。
4. **备份分支不要删**：确认旧仓不再需要任何清理前内容后，再考虑删
   `backup/pre-zephyr-clean`。

---

## 5. 一句话

旧仓现在是 **`e34fe07` 的 classic 干净工作区**：0 改动、0 tracked Zephyr、
`examples` 全回来、classic CLI 可用；清理前 867 项全部封存在本地分支
`backup/pre-zephyr-clean`。
