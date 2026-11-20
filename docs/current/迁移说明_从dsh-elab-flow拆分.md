# 迁移说明 · 从 `dsh-elab-flow` 拆分出 `kharon-ide`

日期：2026-11-19。目的：把“Zephyr 专属 IDE”从“classic 多芯片 + DSH 插件发布线”里独立出来，
让两条产品线的版本、发布节奏和目录边界互不牵扯。

> **现状校准（2026-11-20）**：本文记录的是 11-19 拆分当时的中间态，路径按**历史快照**读。
> 现在工作区已再次物理拆分：IDE 根 `kharon-ide`，west topdir 是同级 `zephyr-workspace`；
> 板级在 `zephyr-workspace/kharon-boards`，公共层在 `zephyr-workspace/elab-app-common`，
> 产品在 `zephyr-workspace/kharon-manifest/products/kharon`。以
> [`tutorials/15`](../tutorials/15-Zephyr工作区双根与板级APP产品.md) 为准。

## 1. 两个仓的定位

| 仓 | 定位 | 状态 |
|---|---|---|
| `dsh-elab-flow` | classic 多芯片（STM32 / AT32 / ESP-IDF）+ DSH 插件发布线 | **冻结**；已有 tag / release 不动，工作区既有改动保留 |
| `kharon-ide` | Kharon / Zephyr 专属 IDE（CLI + standalone cockpit） | 新仓；后续 Zephyr 功能只进这里 |

旧仓的 `v4.0.0` 及之前 release 是发布基线；本仓不是 `v4.0.0` 的快照，而是“发布版本之后 Zephyr
相关工作区改动”的收口。两者不共享提交历史。

## 2. 本仓保留了什么

- `services/elab/**` 内核：闭环、构建、烧录、串口、调试、项目/工作区解析、用量报告。
- `cockpit/server.py` + `packages/cockpit-ui-core/**`：standalone 驾驶舱与 HTTP ICD。
- `packs/official/zephyr/**`、`providers/official/zephyr/**`：Zephyr 框架包 / 工具链 provider。
- `applications/**`（14 个 freestanding 应用）、`projects/kharon/**`（15 个工程登记）。
- `zephyr_modules/elab-boards/**`、`zephyr_modules/elab-app-common/**`。
- `docs/tutorials` 的 Zephyr 主线（00、07、09–14）与 `docs/zephyr-learn` 索引。
- `docs/design` 中仍适用于内核/架构的文档。

## 3. 本仓删掉了什么

- DSH 插件：`packages/dsh-elab-cockpit/**`、插件市场/侧边栏设计文档、`skills/**`、`.agents/**`；
  `contracts/tools.schema.json`（`elab_*` 工具注册面）一并删除，只保留 events/SSE 契约。
- classic 入口：`elab.cmd`、`cockpit.cmd`、classic 芯片包（`packs/official/st/**` 等）、
  ESP-IDF provider、`examples/**`。
- classic 主机 profile：`config/host.classic.profile.yaml`。
- 已过时的 classic 教程（01–06、08）与整目录 `docs/architecture`、`docs/archive`、
  `docs/promo`、`docs/releases`、`docs/requirements`。
- 发布产物 / 站点 / 宣传素材：`site/**`、`docs/promo/**`。
- 本机依赖：`west/`、`.deps/`、`build/`、`host.local.yaml`（全部 gitignore，clone 后由
  `kharon bootstrap` 生成）。

## 4. 关键结构调整

### 4.1 公共层从相对路径变成 Zephyr module

旧结构里 5 个应用用 `../../common` 直接引 C 源，应用一拷走就断。现在：

```
zephyr_modules/elab-app-common/
├── zephyr/module.yml
├── CMakeLists.txt
├── include/{breath_sync.h, ota_engine.h, store.h}
├── src/{breath_sync.c, ota_engine.c, store.c}
└── test/test_ota_engine.c
```

`breath_sync` / `ota_engine` 始终编译；`store` 按 `CONFIG_NVS` 条件编译。
provider 的 `EXTRA_ZEPHYR_MODULES` 同时挂 `elab-boards` 与 `elab-app-common`。
应用 CMake 只保留自己的 `src/main.c`。

`applications/common/` 只保留两份板级 dtsi（`korvo_board.dtsi`、`korvo_i2c.dtsi`），
供 overlay 相对 include；C 源与头文件已迁入 module。

### 4.2 工程登记补齐

`projects/kharon/` 从 9 个补到 15 个，新增：
`input_btn`、`korvo_wifi`、`korvo_apps`、`korvo_lab`、`korvo_nvs`、`korvo_periph`。
每个工程都是“app + board + 产物 + 串口判据”，不是只写一个路径。

### 4.3 bootstrap 入口

`kharon.cmd bootstrap` 已可用，等价于原来的
`python -m kharon.bootstrap`。2026-11-19 起分两条通道：

- **learning（全新 clone 默认）**：`west init` 跟随上游，`west update` 全量；对齐 Zephyr
  官方教程，适合第一次学 Zephyr、看完整 boards / samples / drivers。
- **pinned（`--pinned`）**：按 manifest 的 zephyr commit 初始化，只拉
  `zephyr.pinned_modules` 的 6 个模块；适合 CI、回归、复现某次实验。

`--check` 只读体检，`--offline` 只用本机目录，`--write-local` 写 `host.local.yaml`
的 `kharon:` 分节。当前工作区会沿用上次记录的通道；通道状态在 `.kharon/bootstrap.json`
（gitignored）。`west` 是 junction 时拒绝联网更新，避免误改旧仓工作区。

`manifests/zephyr.west.yml` 的 `hal_espressif` 已从漂移的 `pull/657/head` 钉到
`d89f455da929bdda8cfe0960fceeb36c704ce1b8`；升级 Zephyr 时整份重新导出，不允许只改单个 revision。

## 5. 对使用者的影响

| 旧用法 | 新用法 |
|---|---|
| `elab build -p STM32F103C8T6` | 本仓不提供 classic 芯片；F103 示例仅作 Zephyr 应用保留 |
| `elab.cmd ...` | `.\kharon.cmd ...` |
| `applications/common` 直接编译 | 由 `elab-app-common` module 提供，应用不再写相对路径 |
| DSH 侧边栏 / 插件市场 | 不在本仓；未来只作为 HTTP ICD 外部消费者 |
| `host.classic.profile.yaml` | 不存在；只用 `host.kharon.profile.yaml` + `host.local.yaml` |

## 6. 验收基线（本仓当前）

- `kharon bootstrap --check`：pinned 通道 manifest / host profile / venv / workspace /
  `zephyr.commit` / 6 模块 / SDK / 工具链全绿；learning 通道按当前 `west list` 检查全量模块。
  新 clone 首次选 learning 会缺模块，属正常，跑 `kharon bootstrap` 下载即可。
- `kharon deps --check`：`esp32s31_function_coreboard` 板级 module 可见。
- `kharon env --check`：5 个变量 / 5 条 PATH 前置。
- `kharon run -p f411_breath --steps doctor,build`：通过，产物在
  `build/stm32f4/f411_breath/default/zephyr/zephyr.elf`。
- 15 份 `projects/kharon/*.yaml` 均通过极简 YAML 解析。

## 7. 还没做的

- 本地已 `git init` 并完成首次提交（`628b261` + 清理提交 `21992d5`）；remote 地址与 push 待定。
- learning 通道的 fresh clone 全量 `west update`（约 1.1G）尚未在本机实跑；本机验证复用旧仓
  `west` / `.deps` junction，跑的是 pinned 6 模块闭包。
- `pinned` 通道的 fresh clone 6 模块 `west update` 也待一台干净机器实测。
- `docs/design` 中 classic 时代文档保留作设计背景，文首已统一标注“历史文档（classic / DSH 时代）”。
- CI：`.github/workflows/kharon-ci.yml` 已创建（Windows + Python 3.11，契约漂移检查 + 全量 pytest）；
  需要真机/探针的 `f411_breath` 闭环仍由 `ci/matrix.yaml` 的 self-hosted runner 承担。
- `PROVENANCE.md` 记录拆分基线（旧仓 `e34fe07`）。
