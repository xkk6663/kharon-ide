# 现行文档（docs/current）

这里是本仓的**现行口径入口**。clone 后只需读本页列出的文档，即可完成
`host.local.yaml` → `kharon bootstrap` → `doctor` → `build`。

`bootstrap` 默认 `learning`：跟随上游 + 全量 `west update`，对齐 Zephyr 教程；需要
CI / 回归 / 复现时加 `--pinned`，只拉 manifest 里钉住的 6 个模块。

## 先读什么

| 顺序 | 文档 | 用途 |
|---|---|---|
| 1 | [项目全览与快速开始](../tutorials/00-项目全览与架构解读.md) | 定位、四层事实源、目录地图、F411 跑通 |
| 2 | [迁移说明](迁移说明_从dsh-elab-flow拆分.md) | 旧仓冻结、新仓范围、公共层与工程登记变更 |
| 3 | [设计约束 C1–C36](../design/设计约束.md) | 硬约束：每条都附「为什么」与事故来源 |
| 4 | [ICD_cockpit_events](../design/ICD_cockpit_events.md) | 事件 topic 全集、SSE 帧格式、兼容承诺 |
| 5 | [ADR-001 事实源与边界](ADR-001-事实源与边界.md) | 板子 / pack / provider 三条边界决策 |

## 按任务查

| 任务 | 文档 |
|---|---|
| Zephyr 工作区 / 应用形态 | [12-Zephyr工作区三形态](../tutorials/12-Zephyr工作区三形态.md) |
| 主线闭环 | [10-Zephyr模式上手](../tutorials/10-Zephyr模式上手.md) |
| 调试工作区 | [14-Zephyr调试区操作](../tutorials/14-Zephyr调试区操作.md) |
| 串口 / 帧协议 / Modbus | [07-串口面板使用](../tutorials/07-串口面板使用.md)、[09-协议上手_Modbus](../tutorials/09-协议上手_Modbus.md)、[11-Zephyr_Modbus从站](../tutorials/11-Zephyr_Modbus从站.md) |
| OTA | [13-Zephyr_OTA局域网升级](../tutorials/13-Zephyr_OTA局域网升级.md) |
| Provider 工具链 | [技术方案_Provider工具链架构](../design/技术方案_Provider工具链架构.md) |
| 应用公共层 / 工程登记 | [架构优化与目录重构方案](../design/架构优化与目录重构方案_2026-11-19.md) |
| 事件 / 契约生成物 | 仓库根 `contracts/*.schema.json` → `scripts/generate-contracts.mjs` |

## 契约与生成物

- 事件 topic 与 HTTP ICD 的**唯一事实源**是仓库根 `contracts/`。
- 生成物在 `cockpit/web/src/api/generated/contracts.ts`（禁止手改）；本仓没有 DSH 插件生成物。
- 漂移检查：`node scripts/check-generated-contracts.mjs`（CI 与 `tests/contract` 双重拦截）。

## 历史材料

classic 时代的历史计划、调研与验收报告留在旧仓 `dsh-elab-flow`，**不是**本仓现行行为的依据。
本仓只保留与 Zephyr / Kharon 相关的设计文档。
