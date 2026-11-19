# AGENTS.md — kharon-ide

本仓是 **Kharon / Zephyr 专属 IDE** 产品线：内核在 `services/elab/**`，standalone cockpit 在
`cockpit/server.py`，Zephyr 工具链事实在 `providers/official/zephyr/**`，板级与框架事实在
`packs/official/zephyr/**`。**不含 DSH 插件、classic 多芯片入口、芯片插件市场。**

## 硬边界

1. **业务代码只写业务层**：闭环 / 构建 / 烧录 / 串口 / 调试 → `services/elab/**`。
2. **流程清单唯一事实源**：`services/elab/flow.py` 的 `STEP_META` / `KERNEL_PLANS`；驾驶舱只引用不复制。
3. **不改 DSH**：本仓没有 `packages/dsh-elab-cockpit`，也不要重新引入。对外只保留 loopback HTTP ICD。
4. **芯片/框架知识只写数据包**：`packs/official/zephyr/**`；内核不 import 包内文件、不为某厂商开分支。
5. **工具链命令只写 provider**：`providers/official/zephyr/**`；provider 是数据，执行器只接受解析后的结果。
6. **应用公共层只进 module**：`zephyr_modules/elab-app-common/**`；应用不得再用 `../../common` 引 C 源。
7. **应用可独立**：`applications/**` 每个目录 = 一个 freestanding app；目录名与 `projects/kharon/*.yaml`
   的 `name:` 保持一致。新增应用必须同时登记工程（`kharon adapt --write` 或手写 YAML）。
8. **本机路径只进 `host.local.yaml`**（gitignored）；profile 只写仓库相对路径 `${ELAB_ROOT}/...`。
9. **没有明确命令不 git commit**；改动留在工作区。

## 验证

```powershell
.\kharon.cmd bootstrap --check
.\kharon.cmd deps --check
.\kharon.cmd env --check
.\kharon.cmd ws --app applications\stm32f4\f411_breath
.\kharon.cmd run -p f411_breath --steps doctor,build
pytest tests -q
```

- 改了 app CMake / module：至少跑一个 f411 工程与一个 S3 工程。
- 改了 `manifests/zephyr.west.yml`：跑 `kharon bootstrap --check`，确认 revision 不漂移。
- 改了 `cockpit/server.py`：跑 cockpit 冒烟（`kharon cockpit`）并检查 HTTP ICD。
