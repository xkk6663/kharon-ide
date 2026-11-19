# 文档索引 · kharon-ide

> 先读 [`current/`](current/README.md)：现行边界、迁移说明与 ADR。
> 操作从 [`tutorials/00`](tutorials/00-项目全览与架构解读.md) 开始；框架学习看
> [`zephyr-learn/`](zephyr-learn/README.md)。

## tutorials/ —— 操作主线

| 文档 | 内容 |
|---|---|
| [00 项目全览与快速开始](tutorials/00-项目全览与架构解读.md) | clone → bootstrap → 编译 F411，四层事实源与目录地图 |
| [07 串口面板使用](tutorials/07-串口面板使用.md) | 串口视图、编码/解码、帧协议 |
| [09 协议上手_Modbus](tutorials/09-协议上手_Modbus.md) | Modbus 主站/从站入门 |
| [10 Zephyr 模式上手](tutorials/10-Zephyr模式上手.md) | Kharon provider、工程登记、闭环步骤 |
| [11 Zephyr Modbus 从站](tutorials/11-Zephyr_Modbus从站.md) | Zephyr 侧 Modbus 从站与串口判据 |
| [12 Zephyr 工作区三形态](tutorials/12-Zephyr工作区三形态.md) | 仓库应用 / 工作区 / 独立应用三种形态 |
| [13 Zephyr OTA 局域网升级](tutorials/13-Zephyr_OTA局域网升级.md) | MCUboot + 局域网 OTA |
| [14 Zephyr 调试区操作](tutorials/14-Zephyr调试区操作.md) | GDB、断点、变量、寄存器、实时内存 |

## current/ —— 现行口径

| 文档 | 内容 |
|---|---|
| [README](current/README.md) | 现行入口与阅读顺序 |
| [ADR-001 事实源与边界](current/ADR-001-事实源与边界.md) | 唯一事实源、分层边界 |
| [迁移说明](current/迁移说明_从dsh-elab-flow拆分.md) | 旧仓冻结、新仓范围、公共层/工程登记变更 |

## design/ —— 设计与约束

以下文档与 Zephyr 产品线直接相关；其余 classic 时代文档仅作历史参考，不再作为现行依据
（文首统一加“历史文档（classic / DSH 时代）”标注，路径与流程可能已失效）。

| 文档 | 内容 |
|---|---|
| [设计约束](design/设计约束.md) | C1–C36 硬约束 |
| [仓库结构地图与官方对照](design/仓库结构地图与官方对照_2026-11-18.md) | 目录术语与 Zephyr 官方结构对照 |
| [架构优化与目录重构方案](design/架构优化与目录重构方案_2026-11-19.md) | 应用公共层、工程登记、bootstrap 收口 |
| [技术方案_Kharon融合实施与UI改造](design/技术方案_Kharon融合实施与UI改造_2026-11-18.md) | Kharon 模式与驾驶舱改造 |
| [技术方案_Provider工具链架构](design/技术方案_Provider工具链架构.md) | provider 是数据、执行器只认解析结果 |
| [ICD_cockpit_events](design/ICD_cockpit_events.md) | 事件契约与 SSE |
| [技术方案_调试面板复刻cortex-debug](design/技术方案_调试面板复刻cortex-debug.md) | 调试工作区 P0–P3 |

## zephyr-learn/ —— 学习线

| 文档 | 内容 |
|---|---|
| [README](zephyr-learn/README.md) | 四层索引、三条路线、版本表 |
| `教程/` | S3/S31/F411/F103 框架概念与实机实验 |
| `课程同款/` | 与课程章节对应的同款工程 |
| `参考/` | 环境搭建、术语、官方对照 |
| `变更记录/` | 学习线自己的变更日志 |

## assets/

教程共用截图与 SVG；新增图放 `docs/tutorials/assets/`，命名按教程编号前缀。
