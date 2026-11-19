# 来源与拆分说明（PROVENANCE）

本仓 `kharon-ide` 是从旧仓 `dsh-elab-flow` 拆分出的 **Zephyr 专属产品线**。

| 项 | 值 |
|---|---|
| 旧仓 | `https://github.com/xkk6663/elab-flow.git` |
| 拆分基线 | `e34fe07e74c036b67231343dd15dd83967ee09fe`（2026-11-16） |
| 旧仓发布线 | `v4.0.0` 及已有 tag / release **保持不动** |
| 本仓定位 | Kharon / Zephyr 专属 IDE：编译、烧录、串口、调试、standalone cockpit |
| 不含 | DSH 插件、`elab.cmd`、classic 多芯片入口、芯片插件市场、`examples/` |

## 保留

- `services/elab/**`：闭环内核、构建、烧录、串口、调试、工程/工作区解析。
- `cockpit/server.py` 与 `cockpit/web/**`：standalone cockpit 及 loopback HTTP ICD。
- `zephyr_modules/elab-boards/**`、`zephyr_modules/elab-app-common/**`：板级与公共层 module。
- `packs/official/zephyr/**`、`providers/official/zephyr/**`：Zephyr 框架事实与工具链 provider。
- `applications/**`、`projects/kharon/**`：freestanding 应用与工程登记。
- `docs/tutorials/**`（Zephyr 主线）、`docs/zephyr-learn/**`、`docs/current/**`、`docs/design/**`。

## 裁剪 / 改写

- 删除 `packages/dsh-elab-cockpit/**` 及 DSH 契约生成物；`contracts/tools.schema.json` 已移除。
- 删除 classic 芯片包 `packs/official/st/**`、`providers/official/espressif/**`、classic host profile。
- 删除 `examples/**`、`site/**`、旧发布/宣传/归档文档。
- 应用公共 C 源从 `applications/common` 迁入 `zephyr_modules/elab-app-common`，应用不再用 `../../common`。
- 工程登记补全为 15 个；`kharon bootstrap` 暴露为一级命令，本机路径统一落 `host.local.yaml`。

## 许可

本仓沿用旧仓 `LICENSE` / `NOTICE`。第三方组件与上游工程（Zephyr、west、MCUboot 等）的许可见各自目录与 `NOTICE`。
