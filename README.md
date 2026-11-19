# kharon-ide

Kharon 是面向 **Zephyr 工作区** 的独立 IDE / 开发闭环：编译、烧录、串口、调试、用量报告都走
同一套 HTTP ICD，Web 驾驶舱（standalone cockpit）与 CLI 共用同一份内核状态。

本仓是 **Zephyr 专属产品线**，不包含 DSH 插件、`elab.cmd`、芯片插件市场与 classic 多芯片发布线。
旧仓 `dsh-elab-flow` 保留为 classic / DSH 发布线并冻结，已有 tag / release 不再变动。

## 快速开始（别人 clone 后）

前置：Windows + PowerShell 7、Python 3.10+、Git；Zephyr SDK 与探针驱动需本机安装。

```powershell
git clone <本仓地址> kharon-ide
cd kharon-ide

# 1. 本机路径：复制模板后改 SDK / gdb / openocd（host.local.yaml 不入库）
Copy-Item config\host.local.yaml.example host.local.yaml
notepad host.local.yaml

# 2. 只读体检：缺什么、下一步跑什么
.\kharon.cmd bootstrap --check

# 3. 幂等补齐 venv + west 工作区，并安装随仓的 zephyr/framework 适配包（会联网；--offline 只查本机）
.\kharon.cmd bootstrap

# 4. 看当前工作区事实（拓扑 / 应用形态 / 模块 / 板子）
.\kharon.cmd ws --app applications\stm32f4\f411_breath

# 5. 编译 + 探活
.\kharon.cmd run -p f411_breath --steps doctor,build

# 6. 驾驶舱
.\kharon.cmd cockpit
```

`host.local.yaml` 只需写**本机绝对路径**；`config/host.kharon.profile.yaml` 里的仓库相对路径
（`${ELAB_ROOT}/...`）已入库，换机器不用改。

## 目录

| 路径 | 作用 |
|---|---|
| `applications/**` | freestanding 应用：`CMakeLists.txt` + `prj.conf` + `src/` + `boards/` |
| `projects/kharon/**` | 工程登记（app + board + 产物 + 监视规则）；`kharon adapt` 可识别外来工程并写这里 |
| `zephyr_modules/elab-boards/**` | 自研板 Zephyr module（经 `EXTRA_ZEPHYR_MODULES` 接入） |
| `zephyr_modules/elab-app-common/**` | 应用公共层 module（`breath_sync` / `ota_engine` / `store`） |
| `services/elab/**` | 内核：闭环编排、构建、烧录、串口、调试、项目/工作区解析 |
| `cockpit/server.py` | standalone cockpit 的 HTTP 面与串口自描述 |
| `providers/official/zephyr/**` | 工具链 provider（声明式，不含可执行插件） |
| `packs/official/zephyr/**` | Zephyr 框架包与板级事实 |
| `manifests/zephyr.west.yml` | west 允许清单（zephyr commit + module revisions） |
| `docs/**` | 教程与设计文档 |

## 工作区识别

- 仓库内应用：`kharon ws --app applications\<family>\<app>` → `app_role=standalone`
- west 树内官方样例：`kharon ws --app west\zephyr\samples\hello_world` → `app_role=repo`
- 外来工程：`kharon adapt <外部 app 目录>` 识别 `find_package(Zephyr)` / `prj.conf` / `boards/*.overlay`，
  默认打印登记 YAML；确认后加 `--write` 落到 `projects/kharon/<name>.yaml`

应用只依赖公共 module，不再用 `../../common` 相对路径；把某个应用目录单独拷走时，
同时带上 `zephyr_modules/elab-app-common` 与板级 module 即可。

## 与旧仓的关系

- 旧仓 `dsh-elab-flow`：classic 多芯片 + DSH 插件发布线，冻结；`v4.0.0` 及已有 release 不动。
- 本仓：只保留 Zephyr / Kharon 能力，新增功能只进本仓；HTTP ICD 边界不变，未来 DSH 若要接
  只作为外部消费者，不再把插件源码放回本仓。

## 文档

- `docs/tutorials/00-项目全览与架构解读.md`（Kharon 快速开始）
- `docs/tutorials/14-Zephyr调试区操作.md`
- `docs/zephyr-learn/README.md`（S3/S31 学习线）
