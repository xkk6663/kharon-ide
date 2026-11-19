# 技术方案：Kharon 驾驶舱（Zephyr 模式）与双发行架构

> 状态：设计定稿，待执行
> 决策日期：2026-11-18（同日修订：classic 由"并行迭代"改为"完全冻结 + 退场"）
> **主旨：elab 会消失，Zephyr 才是主流，全面拥抱 Zephyr。**
> 关联：`技术方案_Provider工具链架构.md`、`技术方案_dsh-elab-flow插件移植.md` §16
> 上游参照仓库：`C:\Users\xiao1\Desktop\esp32\zephyr-learn`（Zephyr 4.5.99 / SDK 1.0.1）

---

## 0. 决策记录

| # | 决策 | 结论 | 依据 |
|---|---|---|---|
| D1 | 仓库形态 | **单仓双发行**，不 fork | fork 需复制 ~42,400 行，其中约 15,000 行在 Zephyr 模式下是僵尸代码；且插件的仓库根判据硬编码 `cockpit/server.py` + `services/elab`，分仓会同时断掉内核发现链路 |
| D2 | 产品关系 | **过渡期并列，终局只有一个产品** | 过渡期：两套命令 / 插件 / 站点各自独立，不互相提及。终局：classic 删除，只剩 Kharon |
| D3 | classic 模式 | **完全冻结，零投入，等待退场** | 不修 bug、不接新芯片包、不加 provider、不加任何产品级能力。classic 只作为 Kharon 的回归基线存在 |
| D4 | 产品命名 | **Kharon**；`elab` 这个名字最终退场 | 命令 `kharon`、npm 包 `dsh-kharon-cockpit`、站点独立 |

### D2/D3 的补充说明（主旨，决定了后面所有取舍）

**主旨：elab 会消失，Zephyr 才是主流，全面拥抱 Zephyr。**

所以「双发行」不是长期架构，是**可删除的过渡脚手架**。三句话划清边界：

1. **classic 冻结 = 真的什么都不做**。连「顺手修个 bug」「顺手接颗新芯片」都不做。留在仓库里只为一件事：当 Kharon 改了共享内核时，用它验证「老行为没坏」。它是**回归基线**，不是产品线。
2. **`distros/classic` 从第一天起就写删除日期**。P1 引入它是为了让内核改动可验证，P4 删除它。任何让它「更长期」的改动都是在延长退场，一律不做。
3. **唯一例外**：修 Kharon 时如果 bug 落在共享内核里（`run.py`、`serialmon.py`、`debugsession.py` 这些两边都在用的），修一次两边都受益——但这叫「修 Kharon 的人顺手」，不叫「投入 classic」。判断标准是**发起动机**：为了 classic 才改 → 不做。

### 必须承认的退场代价

elab classic 有一块能力 Zephyr 模式**不覆盖**：接管非 Zephyr 的裸机工程（CubeMX / AT32，靠 `adapt.py` 指纹 + `inject.cmake` 寄生）。elab 消失 = 放弃这块能力。

这不是 bug，是产品收缩。处理方式二选一（见 §6.4）：
- **放弃**：存量裸机工程留在 v3.1.0 的最后版本，不再支持
- **转化**：把 `adapt.py` 从「接管」改成「迁移器」——读 `.ioc` / AT32 工程，生成 Zephyr 的 board overlay + `prj.conf` 骨架。这是死代码唯一的活路，但属于增值项，不进 P0–P2

---

## 1. 现状量化（为什么要双发行而不是 fork）

### 1.1 代码账

| 层 | 文件 | 行数 |
|---|---|---|
| `services/elab` 内核 | 43 个 .py | 24,718 |
| `cockpit/server.py` | 1 | 3,828 |
| `cockpit/web/src` 前端 | ts/tsx/css | 11,986 |
| `packages/dsh-elab-cockpit` 插件 | ts/mjs | 1,859 |
| **合计** | | **~42,400** |

### 1.2 模块在 Zephyr 模式下的存活率

| 模块 | 行数 | Kharon 下 | 说明 |
|---|---|---|---|
| `serialmon` / `serialcodec` / `serialframes` / `framelearn` / `serialwidgets` / `serialport` | 7,064 | **100% 存活** | 串口与 OS 无关 |
| `debugsession` + `gdbmi` | 1,599 + ~300 | **~95% 存活** | gdb MI 与 openocd 都是通用协议 |
| `run` + `kernel/events` | 1,082 + 768 | **100% 存活** | 步骤编排与事件流 |
| `pkg` + `provider` + `market` | 2,072 + | ~90% 存活 | 只换发现根 |
| `config` / `flash`（部分） / `monitor` / `logdecode` / `doctor`（部分） | ~2,300 | ~50% 存活 | 判据与主机事实通用，芯片校验项作废 |
| `adapt` | 1,197 | **接管逻辑作废** | Zephyr 工程天然规范，不需要指纹/寄生；仅 `.ioc` 解析可能被「迁移器」复用（§6.4 F3 前定夺） |
| `builder` | 602 | **基本作废** | 构建走 provider，内核不碰 cmake |
| `ota_layout` | ~300 | **完全作废** | Zephyr 用 sysbuild + `pm` / `partitions.dtsi` |
| `modbus` / `modbusmaster` | 923 | 可选 | 与 OS 无关，按需保留 |
| `rtos`（FreeRTOS 任务视图） | ~200 | 需替换 | Zephyr 用 `k_thread` 遍历，非 `pxCurrentTCB` |

**结论：约 60% 内核与"用什么 OS/SDK"无关，约 40% 是 CubeMX/AT32 专用接管层。**

### 1.3 生态账（支撑用户判断）

| | elab classic | Zephyr |
|---|---|---|
| 已接入芯片包 | 3 个（`st/stm32`、`espressif/esp32`、`artery/at32`），全部自研 | 200+ 官方 board |
| 依赖拉取 | `elab pkg install` + 自建市场索引 | `west update` 拉模块 |
| 工具链 | 自接管 CMake 工具链文件 | SDK 官方多目标 |
| 维护负担 | 每颗新芯片都要写 pack + 指纹 + 坑位 | 上游维护，我们只写板级映射 |

这正是"可行但生态不可行"的量化表达：**我们的护城河是脏活，Zephyr 的护城河是网络效应。**

---

## 2. 架构：Distro 层

**先说定位**：这一层是**过渡脚手架**，不是终局架构。存在的唯一理由是「让 Kharon 从第一天起就有可验证的对照物」，F3 阶段 `distros/classic` 会被删掉，`distro.py` 退化为「默认加载 `distros/kharon`」。

所以这里只写「怎么让两套并存且互不污染」，不写「怎么让两套长期平衡」——后者是错的问题。

### 2.1 目录规划

```
dsh-elab-flow/
├── distros/
│   ├── classic/distro.yaml          ← 现有行为的显式化（内容等价于当前默认）。★待删：F3 阶段整体移除
│   └── kharon/distro.yaml           ← Kharon 发行清单（终局唯一发行）
├── services/elab/                   ← 共享内核（+ distro.py，约 120 行）
│   └── distro.py                    ← 新增：清单加载 + 发现根解析
├── cockpit/                         ← 共享 HTTP 面 + 前端（面板按 capabilities 渲染）
├── packages/
│   ├── dsh-elab-cockpit/            ← classic 插件。★冻结：只保留可用，不再改
│   └── dsh-kharon-cockpit/          ← Kharon 插件（薄壳，复用同一套 UI 源码）
├── packs/
│   ├── official/…                   ← classic 现有包。★冻结：不接新芯片
│   └── zephyr/                      ← 新增：zephyr/framework 包
├── providers/
│   ├── official/…                   ← classic 现有 provider。★冻结：不加新 provider
│   └── zephyr/                      ← 新增：zephyr/west provider
├── projects/
│   ├── *.yaml                       ← classic 现有项目。★冻结：只作回归基线
│   └── kharon/                      ← Kharon 项目绑定
├── docs/legacy/                     ← 新增：classic 冻结档案（已知问题 / 教程索引 / 退场说明）
├── elab.cmd / elab                  ← classic 入口。★F3 移除
└── kharon.cmd / kharon              ← 新增入口（一行壳）
```

`★` 标记的四类路径 = 冻结区。原则：**冻结区只接受「Kharon 需要它」的改动，不接受「classic 需要它」的改动**。判断发起动机，不看改动内容。

### 2.2 加载机制（数据驱动，禁止 `if distro ==`）

```
优先级：--distro <name>  >  ELAB_DISTRO  >  distros/active.txt  >  "classic"
```

`services/elab/distro.py` 只做四件事，**不做任何业务分支**：

1. 读 `distros/<name>/distro.yaml`，返回不可变 `DistroRef`
2. 覆盖发现根：`packs.roots` / `providers.roots` / `projects.root` / `official_root`
3. 提供步骤启用子集与文案覆盖（与项目级 `step_labels` 同语义，见 `flow.py:44-57`）
4. 提供 CLI `prog` 与横幅文案

**红线**：内核任何模块**不得**出现 `distro.name == "kharon"` 这类判断。发行差异只能通过 yaml 表达。这条与 AGENTS.md 第 5 条（芯片知识只写适配包）同一性质，并应由 `tests/test_distro_boundary.py` 静态扫描拦下。

### 2.3 distro.yaml 规范（schema v1）

```yaml
schema_version: 1
name: kharon
title: Kharon
description: Zephyr 模式驾驶舱

cli:
  prog: kharon
  banner: "Kharon — Zephyr 模式驱动 · west 命令可视化"

# 发现根（相对 ${ELAB_ROOT}；绝对路径也允许）
roots:
  packs:     [packs/zephyr]
  providers: [providers/zephyr]
  projects:  projects/kharon
  packs_official:     packs/zephyr        # 覆盖 pkg.official_root()
  providers_official: providers/zephyr    # 覆盖 provider.official_root()

host: kharon.host.yaml                    # 独立主机事实源，与 elab.host.yaml 互不污染

# 步骤启用子集（默认值仍在 flow.py，这里只做交集与文案）
flow:
  enabled_steps: [doctor, build, flash, monitor]
  step_labels:
    doctor: 环境体检
    build: 编译
    flash: 烧录
    monitor: 串口闭环
  plans:
    - { id: basic,  title: 编译,     steps: [build] }
    - { id: triple, title: 编译烧录, steps: [build, flash] }
    - { id: loop,   title: 全闭环,   steps: [doctor, build, flash, monitor] }

# 面板可见性（前端按 capabilities.meta 渲染，服务端只上报子集）
cockpit:
  panels: [build, flash, serial, debug, workspace, matrix]
```

> `flow.enabled_steps` 是 `STEP_META` 的**交集**，不是新增词汇。`debug_verify` 在 Kharon 中默认关闭：`west flash` 没有跨 runner 统一的校验语义，硬做会造出假证据。

---

## 3. Kharon 的差异化（决定产品是否站得住）

Zephyr 用户是主动选择终端的那批人。若 Kharon 只是"west 的按钮版"，必败。真正 west 不给的只有四件事，按价值排序：

| # | 能力 | west 现状 | Kharon 做法 | 内核改动 |
|---|---|---|---|---|
| 1 | **多板矩阵**：一个 app × N board 并排对照 | 完全不给，要开 N 个终端手抄 | 一组 project 的**聚合视图** | 0（见 §3.1） |
| 2 | **工作区可视化**：模块树 / SDK 目标 / board 清单 | `west list` 纯文本 | 新面板，读 `west list` + `west manifest` | 新增 1 个只读端点 |
| 3 | **证据链与回放** | 无 | 已有：`run/events` + 串口域事件 | 0 |
| 4 | **串口 + shell + 结构化日志 + 调试** | `west espressif monitor` 仅透传 | 已有 7,000 行串口子系统 | 0 |

### 3.1 为什么多板矩阵不引入第二套真相

**矩阵不是新数据模型，是 project 集合的视图。**

- 一个 board = 一个 `projects/kharon/<app>__<board>.yaml`，每份都符合现有 schema
- 矩阵视图只读这些 project 的 `run` 结果（内存 / 产物 / 串口结论）做对照
- 内核的"一个 project 一个 chip"假设完全不动（`plan_for` 单 chip，`config.chip_of` 单 chip）

这条设计直接沿用 `docs/教程/15-Zephyr跨芯片/` 的方法论：**同一份源码在四块板上的差异，必须能被并排看见**。终端里做不到，这正是 Kharon 的存在理由。

### 3.2 明确不做

| 想法 | 判断 |
|---|---|
| Kconfig 图形配置器（Web 版 menuconfig） | **不做**。需解析 Kconfig 依赖图，成本极高，且是 Zephyr 生态里最"IDE 化"的东西，背离双方哲学。替代：`prj.conf` 结构化编辑 + 一键拉起 `west build -t guiconfig` |
| 用 elab 包管 `west update` 的模块 | **不做**。定位写死：`elab pkg` = 板级知识；`west update` = 上游源码 |
| 把 `build.ps1` 的 ccache/-j/pristine 逻辑搬进内核 | **不做**。留在 provider 命令模板里 |
| 重写 `rtos.py` 为 Zephyr 线程视图 | P3 再看。Zephyr 的 `k_thread` 遍历需要 gdb python 脚本，成本不低 |

---

## 4. Zephyr 适配层：四份 yaml

### 4.1 `kharon.host.yaml`（主机事实源）

```yaml
# 与 elab.host.yaml 完全独立：换机器只改这一份
tools:
  zephyr:
    workspace: C:/Users/xiao1/Desktop/esp32/zephyr-learn/zephyr_k   # west 工作区（cwd）
    base:      C:/Users/xiao1/Desktop/esp32/zephyr-learn/zephyr_k/zephyr
    sdk:       C:/Users/xiao1/zephyr-sdk-1.0.1
    venv_scripts: C:/Users/xiao1/Desktop/esp32/zephyr-learn/.venv/Scripts
    ccache:    C:/Users/xiao1/Desktop/esp32/zephyr-learn/.ccache
    jobs: "8"
    # PATH 前置（按顺序）；env_prepend_path 逐项拼，缺失项由 doctor 报错
    gnu_bins:
      - C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin
      - C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/riscv64-zephyr-elf/bin
      - C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/xtensa-espressif_esp32s3_zephyr-elf/bin
  cmake: { path: C:/Users/xiao1/Desktop/esp32/zephyr-learn/.tools/cmake/cmake-3.30.2-windows-x86_64/bin/cmake.exe }
  ninja: { path: C:/Users/xiao1/Desktop/esp32/zephyr-learn/.tools/ninja/ninja.exe }
  python: { path: C:/Users/xiao1/Desktop/esp32/zephyr-learn/.venv/Scripts/python.exe }

# ── 三套 gdb（实测文件名，均在 SDK 内）────────────────────────────
gdb_zephyr_arm:    { path: C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb.exe }
gdb_zephyr_riscv:  { path: C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/riscv64-zephyr-elf/bin/riscv64-zephyr-elf-gdb.exe }
gdb_zephyr_xtensa: { path: C:/Users/xiao1/zephyr-sdk-1.0.1/gnu/xtensa-espressif_esp32s3_zephyr-elf/bin/xtensa-espressif_esp32s3_zephyr-elf-gdb.exe }

# ── 两套 openocd（★ 必须分开，实测结论）──────────────────────────
# 实测：xpack 的 board/ 目录**没有** esp32s31-builtin.cfg / esp32s3-builtin.cfg；
#       这两个脚本只在 Espressif 的 openocd-esp32 里。
#       所以 ARM 板走 xpack，ESP32 两板走 openocd_esp32（classic 已在用，直接复用）。
openocd_zephyr:
  path:    C:/xpack-openocd-0.12.0-7/bin/openocd.exe
  scripts: C:/xpack-openocd-0.12.0-7/openocd/scripts
# openocd_esp32 沿用 elab.host.yaml 同款声明（v0.12.0-esp32-20260703）

probes:
  default: dap
  list:
    dap:    { backend: cmsis-dap, interface_cfg: interface/cmsis-dap.cfg, transport: swd, speed: 5000 }
    jlink:  { backend: jlink,     interface_cfg: interface/jlink.cfg,      transport: swd, speed: 8000 }
    builtin: { backend: esp_usb_jtag, interface_cfg: "", transport: jtag, speed: 20000 }

serial: { default: auto, baud: 115200 }
```

> 路径全部集中在 host 层，`provider.yaml` 与 `projects/kharon/*.yaml` 里不出现任何绝对路径（沿用 D1 约束）。

### 4.2 `providers/zephyr/west/provider.yaml`

```yaml
schema_version: 1
provider_api: 1
name: zephyr/west
version: 0.1.0
license: Apache-2.0
description: Zephyr west（工作区构建 / 烧录 / 调试）

requires:
  kernel_api: ">=1 <2"
  host:
    - { key: tools.zephyr.workspace, check: dir }
    - { key: tools.zephyr.base,      check: dir }
    - { key: tools.zephyr.sdk,       check: dir }
    - { key: tools.python.path,      check: file }
  probe: true          # 烧录要探针；build 不需要（由 steps 级判定）
  serial: true

toolchain:
  name: Zephyr SDK
  version: "1.0.1"
  docs: https://docs.zephyrproject.org/latest/develop/getting_started/index.html
  install:
    windows:
      - winget install Kitware.CMake Ninja-build.Ninja
      - python -m venv C:\zephyr\.venv && C:\zephyr\.venv\Scripts\pip install west
      - wget https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v1.0.1/zephyr-sdk-1.0.1_windows-x86_64.7z
  host_keys: [tools.zephyr.workspace, tools.zephyr.base, tools.zephyr.sdk]

defaults:
  # ★ 必须 cd 到 west 工作区，不是 app 目录：app 在 workspace 外，
  #   west 需要用 topdir 定位 zephyr/（zephyr-learn/tools/build.ps1:43 同款做法）
  cwd: ${host:tools.zephyr.workspace}
  env:
    ZEPHYR_BASE: "${host:tools.zephyr.base}"
    ZEPHYR_SDK_INSTALL_DIR: "${host:tools.zephyr.sdk}"
    ZEPHYR_TOOLCHAIN_VARIANT: zephyr
    CCACHE_DIR: "${host:tools.zephyr.ccache}"
    CCACHE_MAXSIZE: 3G
    CCACHE_COMPRESS: "1"
  env_prepend_path:
    - ${host:tools.zephyr.venv_scripts}
    - ${host:tools.zephyr.cmake_bin}
    - ${host:tools.zephyr.ninja_bin}
    - ${host:tools.zephyr.gnu_bins}

artifacts:
  elf: ${work_dir}/zephyr/zephyr.elf
  bin: ${work_dir}/zephyr/zephyr.bin
  hex: ${work_dir}/zephyr/zephyr.hex
  map: ${work_dir}/zephyr/zephyr.map

steps:
  doctor:
    cmd: ["west", "--version"]
    timeout_s: 120
  build:
    # -- 之后传 ninja：-j 与 ccache launcher 都在这里，内核不认识这些参数
    cmd: ["west", "build", "-b", "${chip.target}", "${root}", "-d", "${work_dir}",
          "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "--", "-j", "${host:tools.zephyr.jobs}"]
    timeout_s: 1800
  flash:
    cmd: ["west", "flash", "-d", "${work_dir}",
          "--runner", "${project.flash.runner}", "--config", "${project.flash.config}"]
    timeout_s: 600

debug:
  backend: zephyr
  features: [session, control, stack, variables, registers, breakpoints, memory, disasm, watch]
```

**为什么 `build` 不用两步（configure + ninja）**：`west build` 本身幂等，第二次调用自动增量；拆两步反而要自己判断"是否需要 pristine"，与 `build.ps1` 的 `-Pristine` 语义打架。pristine 由项目 yaml 的 `build.pristine: true` 映射为 `-p always` 参数（P2 再加）。

### 4.3 `packs/zephyr/framework/pack.yaml` + `chips/*.yaml`

```yaml
# pack.yaml
schema_version: 1
name: zephyr/framework
vendor: zephyr
platform: framework
version: 0.1.0
license: Apache-2.0
status: official
description: Zephyr 板级映射与平台坑位（**不含**内存/核心/编译宏——那些归 dts/Kconfig）
kind: framework            # ★ 新增字段，见 K4

requires:
  kernel_api: ">=1 <2"
  schema_version: 1

log_profiles:
  - id: zephyr
    label: Zephyr（LOG / 致命错误）
    panic_lines: ['^\s*FATAL ERROR', 'Kernel panic', '^\*\*\*\*\* (HARD|MPU|BUS|USAGE) FAULT', 'ASSERTION FAIL']
    hardfault_lines: ['^\*\*\*\*\* (HARD|MPU|BUS|USAGE) FAULT']
    reset_lines: ['^\*\*\* Booting Zephyr', '^\[00:00:00\.0']
    reason_pattern: 'FATAL ERROR \d+:\s*(.+)'
    backtrace_line: '(?:Backtrace|Call trace):\s*(.*)'
    addr_pattern: '0x[0-9a-fA-F]{8}'
    symbolize: { tool_key: addr2line_zephyr_arm, args: ['-pfiaC'] }

pitfalls:
  - "改 overlay / prj.conf / 分区表 / sysbuild 配置会触发**全量重编**（400–500 s）；能改 .c 就别改配置"
  - "`boards/<board>.overlay` 与 `app.overlay` **二选一不叠加**（configuration_files.cmake 先命中板级）"
  - "拆多个 CMake target 时用 OBJECT 库，**不要** `zephyr_library_named`（STATIC 不在 --whole-archive 组，HTTP_RESOURCE_DEFINE / SHELL_CMD_REGISTER 会被静默裁掉）"
  - "构建被 kill 后 .ninja_lock 残留 → `WriteFile(.ninja_lock): Permission denied`，先清 ninja 进程"
```

```yaml
# chips/blackpill_f411ce.yaml —— board 即 chip（Kharon 的粒度）
id: blackpill_f411ce
vendor: zephyr
platform: framework
target: blackpill_f411ce          # ★ west -b 的值，复用 chip.target 模板变量
core_name: Cortex-M4F
# ★ 刻意不写 memory / compiler_defines / verify：那是 dts 与 Kconfig 的事实，
#   抄进来就会出现第二套构建真相（红线，见 §6.1）
debug:
  backend: zephyr
  gdb_key: gdb_zephyr_arm         # ★ 键名由芯片声明，host 只提供事实（正交模型）
  openocd_key: openocd_zephyr     # ARM → xpack
  openocd_config: C:/Users/xiao1/Desktop/esp32/zephyr-learn/applications/stm32f4/common/dap_link_openocd.cfg
  device: STM32F411CE
serial: { baud: 115200 }
```

```yaml
# chips/esp32s31_function_coreboard_hpcore.yaml —— 与 ARM 板的差异只有三行
id: esp32s31_function_coreboard_hpcore
vendor: zephyr
platform: framework
target: esp32s31_function_coreboard/esp32s31/hpcore
core_name: RISC-V (RV32)
debug:
  backend: zephyr
  gdb_key: gdb_zephyr_riscv
  openocd_key: openocd_esp32        # ★ 必须用 esp32 那套：board/esp32s31-builtin.cfg 只在这里
  openocd_config: C:/Users/xiao1/Desktop/esp32/zephyr-learn/boards_local/esp32s31_function_coreboard/support/openocd.cfg
  device: esp32s31
serial: { baud: 115200 }
experimental: true                 # 依赖未合入主干的 SoC 移植（见 R4）
```

四块板的 `chips/*.yaml`：`blackpill_f411ce`、`esp32s3_devkitc_procpu`、`esp32s31_function_coreboard_hpcore`、`stm32f103_mini`。**它们之间的差异只允许出现在 `target` / `gdb_key` / `openocd_key` / `openocd_config` 四个字段上**——这正是 Kharon 相对 classic 的收益：换板不再需要改内核的 archetype/ld/flags 探测。

### 4.4 `projects/kharon/f411_breath.yaml`

```yaml
name: f411_breath
root: C:/Users/xiao1/Desktop/esp32/zephyr-learn/applications/stm32f4/f411_breath
pack: zephyr/framework
chip: blackpill_f411ce
archetype: zephyr
provider: zephyr/west@>=0.1 <1

build:
  kind: cmake                    # 描述性：实际构建走 provider，内核不执行 cmake
  source: ${root}/CMakeLists.txt
  # 产物只落 build\<chip>\<app>\<variant>（zephyr-learn/AGENTS.md 第 1 条）
  work_dir: C:/Users/xiao1/Desktop/esp32/zephyr-learn/build/stm32f4/f411_breath/default

flash:
  runner: openocd
  config: C:/Users/xiao1/Desktop/esp32/zephyr-learn/applications/stm32f4/common/dap_link_openocd.cfg

artifacts:
  elf: ${work_dir}/zephyr/zephyr.elf
  bin: ${work_dir}/zephyr/zephyr.bin
  map: ${work_dir}/zephyr/zephyr.map

serial: { port: auto, baud: 115200, reconnect: { enabled: true, min_ms: 200, max_ms: 5000 } }

monitor:
  close_on:
    - { kind: regex, pattern: "Booting Zephyr|breath|\\[alive\\]", within_s: 15 }
  fail_on:
    - { kind: regex, pattern: "FATAL ERROR|Kernel panic|HARD FAULT|ASSERTION FAIL" }
  idle_timeout_s: 30
```

---

## 5. 内核改造清单

| # | 改动 | 位置 | 量级 | 验收 |
|---|---|---|---|---|
| **K0** | 新增 distro 加载器 + 发现根解析 | `services/elab/distro.py`（新） | ~120 行 | `ELAB_DISTRO=kharon elab pkg list` 只列出 zephyr 包 |
| **K1** | 调试后端加第三分支 `zephyr`：**gdb / openocd 按 `chip.debug.gdb_key` / `openocd_key` 取**，不再二元硬编码 | `plan.py:99-104`（现为 `esp_idf` 二分）、`flash.py:_openocd_argv`、`debugsession.py` | ~30 行 | `f411_breath` 能下断点、读寄存器 |
| **K2** | openocd 支持「整份 cfg」模式 | `flash.py:_openocd_argv`（现只有 interface+target 两段 + `scripts_dir` 强校验） | ~15 行 | S31 用 `boards_local/.../support/openocd.cfg`（内部 source `board/esp32s31-builtin.cfg`）能连上 |
| **K3** | `doctor_deep` 允许 provider 提供 | `provider.py:46-47` `CORE_ONLY_STEPS` | ~20 行 | Kharon 下 `doctor_deep` 不再报"内核保留" |
| **K4** | pack schema 加 `kind: framework`，允许 chip 缺 memory/core | `pkg.py:295-320` `validate_manifest` | ~20 行 | `zephyr/framework` 包能装、能过 `pkg index --check` |
| **K5** | Zephyr 工程识别 **新建模块**，不改 `adapt.py` | `services/elab/zephyr_probe.py`（新）；指纹 = `find_package(Zephyr)` + `prj.conf` + `west.yml` | ~60 行 | `kharon adapt <app>` 生成正确 project yaml；`adapt.py` 一行不动（它要在 F3 被删） |
| **K6** | `rtos.py` 换 Zephyr 线程遍历 | `rtos.py` | ~200 行（P3） | 调试页 RTOS 标签页显示 Zephyr 线程 |
| **K7** | 发现根可配置（`packs.official` / `providers.official` / `projects.root`） | `pkg.py:official_root`、`provider.py:135`、`config.py` | ~25 行 | 两个发行版互不看见对方的包 |
| **K8** | 插件启动时透传 `--distro`，仓库根判据放宽 | `host-process.ts:164`、`bootstrap.ts:71`、`discover-route.ts:108` | ~15 行 TS | Kharon 插件能拉起 Kharon 内核 |
| **K9** | 多板矩阵视图（只读聚合） | `cockpit/server.py` 新端点 + 前端新面板 | ~150 行（P2） | 四块板的内存/串口结论并排可见 |

**内核改动合计约 300 行 Python + 15 行 TS + 150 行矩阵视图。** 对比 fork 的 42,400 行复制，这是唯一合理的路径。

---

## 6. 红线

### 6.1 唯一构建真相

| 事实 | 归属 | 禁止 |
|---|---|---|
| 板级内存 / 主频 / 外设 / 链接脚本 | dts + Kconfig | **禁止**写进 `packs/zephyr/**/chips/*.yaml` |
| 编译宏 / FPU / `-mfpu` | Zephyr toolchain | **禁止**沿用 `chip.core` / `chip.compiler_defines` |
| 构建命令与参数 | `provider.yaml` | 禁止进内核 |
| 主机路径 | `kharon.host.yaml` | 禁止进 provider / project |

一旦 chip yaml 抄了 dts 的事实，`doctor` 的 `expect_memory` / `expect_flags` 就会与真实 dts 打架，出现"体检说错、构建是对的"这类最难排查的问题。这条与 AGENTS.md 第 5 条同硬度。

### 6.2 发行隔离

- 内核不得出现 `distro.name == "..."` 判断，由 `tests/test_distro_boundary.py` 静态扫描
- 两个发行的 host yaml、pack 根、provider 根、projects 根完全隔离
- 工具层仍只走 loopback HTTP ICD（AGENTS.md 第 3 条不变）

**隔离的验收判据（F0→F3 期间必须成立）**：Kharon 的改动对 classic 零影响——
`distro.py` 未指定时等价于当前行为（发现根与现在逐字相同）；classic 路径在 STM32F103C8T6 上的 `elab doctor` / `elab run` 输出与改动前逐字一致。这是「冻结」在技术上可被证明的唯一方式（F3 之后该约束随 classic 一起移除）。

### 6.3 classic 冻结的执行细则（不是"不退化"，是"不动"）

冻结期的唯一职责是当回归基线。具体落实：

| 项 | 冻结前的处理 | 冻结后 |
|---|---|---|
| 已知 bug | 扫一遍 `docs/` 里的问题记录，凡不阻塞 Kharon 的一律不动，写进 `docs/legacy/classic-已知问题.md` 存档 | 不再修复，文档注明"停在 vX.Y.Z" |
| 测试 | `tests/` 里 classic 专属用例（test_plan/test_builder/test_adapt 等）保留 | 从 **blocking CI 降为 informational**：只跑、不阻塞、红了也不修（否则等于还在维护） |
| 依赖 | 锁死现有版本 | 不做依赖升级；安全类 CVE 若不影响 Kharon 继承的模块，不处理 |
| 新芯片 / provider | — | **一律不接**。用户提需求 → 答复"请等 Kharon 的对应 board 支持" |
| 文档 | 教程不动 | 顶部加一行"本文档对应 classic，已冻结" |

**为什么测试要降级**：如果 classic 测试还是 blocking，那么 CI 一红，人就必须去修——这恰恰是"D3 说零投入、实际还在投入"的典型漏洞。冻结必须是**机制上的冻结**，靠人自觉守不住。

### 6.4 退场路径（classic 如何消失）

按里程碑触发，不按日期。每一步都可停在原地不烂尾：

| 阶段 | 触发条件 | 动作 | 可回退 |
|---|---|---|---|
| **F0 冻结** | D3 生效（现在） | 代码打 `# legacy:` 标记（仅注释，不改逻辑）；`docs/legacy/` 建档；CI 降级 | 是（删注释即可） |
| **F1 隐性化** | P1 完成，Kharon 能独立跑通四板 | `kharon` 为默认入口；`elab` 命令保留但 `--help` 首行提示已冻结；README 主线切 Kharon，classic 移到 `docs/legacy/` | 是 |
| **F2 市场下架** | P2 完成，Kharon 有插件与站点 | classic 的市场条目下架、npm 包 `deprecate`（提示语指向 Kharon）；**不删包**（72 小时后 npm 也删不掉） | 部分 |
| **F3 代码删除** | Kharon 能覆盖用户实际在用的全部工程（判据：连续一个季度没有为 classic 发起任何改动） | 删 `distros/classic`、`adapt.py`（若未转迁移器）、`builder.py` 注入层、`ota_layout.py` 对账；`elab.cmd` 移除 | 否，但有 git 历史 |
| **F4 命名退场** | F3 之后 | 仓库/包/文档全面以 Kharon 命名；`elab` 仅作为历史名出现在 NOTICE 与变更记录里 | 否 |

两条不可动的约束：
- **已发布的 MIT 授权永久有效**（v1.0.0–v3.1.0），F2/F3/F4 都不改变它。`NOTICE` 里的协议沿革章节永久保留。
- **F3 之前不删任何代码**。冻结期删代码 = 失去回归基线，等于让 Kharon 裸奔。

---

## 7. 执行计划

### P0 零代码验证（半天）

**目标**：证明 provider 机制能承载 west，不改一行内核。

| # | 任务 | 验收 |
|---|---|---|
| 1 | 手写 `kharon.host.yaml` + `providers/zephyr/west/provider.yaml` + `packs/zephyr/framework/**` + `projects/kharon/f411_breath.yaml` | 四份文件齐全，`elab pkg install zephyr/framework` 通过 |
| 2 | 用 **classic 内核** 跑（不启用 distro 层） | `python -m elab run -p f411_breath --steps doctor,build` 出 `OK` |
| 3 | 对照 `--dry-run` 与 `build.ps1` 的实际命令 | 两条命令逐字等价（除路径差异） |
| 4 | 烧录 + 串口 | `--steps flash,monitor` 能烧进去并读到呼吸统计 |

> P0 用**现有内核当载具**（不改它一行）+ `ELAB_*_ROOTS` 环境变量指向新目录即可，不需要 distro 层。注意这是「借它跑一次」，不是「修它」——载具要有 bug 也不动，绕过去。**这是整个方案的风险闸门**：P0 不通，后面全部作废。

### P1 发行层（1–2 天）

| # | 任务 | 验收 |
|---|---|---|
| 1 | `services/elab/distro.py` + `distros/{classic,kharon}/distro.yaml` | 两套 `distro.yaml` 都能加载，classic 行为逐字不变 |
| 2 | `kharon.cmd` / `kharon` 入口 | `kharon --help` 的 prog 名与横幅正确 |
| 3 | K7 发现根可配置 | 两个发行版 `pkg list` 互不看见对方 |
| 4 | Zephyr log profile 落地（`pack.yaml` 的 `log_profiles`） | 故意触发 `FATAL ERROR` 能识别 + 符号化 |

### P2 驾驶舱与调试（2–3 天）

| # | 任务 | 验收 |
|---|---|---|
| 1 | K8 插件透传 distro，出 `packages/dsh-kharon-cockpit` | 面板能拉起 Kharon 内核并显示四个步骤 |
| 2 | K1 + K2 调试后端第三分支 | 断点 / 变量 / 寄存器 / 内存 / 反汇编全通 |
| 3 | K5 Zephyr 工程识别（新建 `zephyr_probe.py`） | `kharon adapt <new_app>` 生成可直接跑的 project yaml |
| 4 | K9 多板矩阵视图 | 同一 app 的四块板并排，内存与串口结论可对照 |

### P3 生态收口（按需）

| # | 任务 | 验收 |
|---|---|---|
| 1 | K3 `doctor_deep` 可被 provider 提供 | Kharon 有深度体检 |
| 2 | K4 `kind: framework` 正式进 schema + `elab pkg index` 支持 | 包可发布到市场 |
| 3 | `west list` 工作区面板 | 模块树 / SDK 目标可见 |
| 4 | K6 Zephyr 线程视图 | 调试页显示线程列表 |
| 5 | 站点 `kharon` 分支 + npm 发布 | 独立 gh-pages 与 npm 包 |

### P4 退场（classic 消亡，按里程碑触发而非日期）

对应 §6.4 的 F2–F4。前置条件：Kharon 已覆盖四板 + 插件 + 站点可用。

| # | 任务 | 验收 |
|---|---|---|
| 1 | classic 市场条目下架 + npm `deprecate` | 包还在但不推荐；提示语指向 Kharon |
| 2 | 删 `distros/classic`、`adapt.py`、`builder.py` 注入层、`ota_layout.py` 对账、`elab.cmd` | 内核行数下降 ~3,600；`kharon` 全测试绿；`tests/` 里 classic 用例一并删除 |
| 3 | 文档收口：`docs/legacy/` 归档 + README 只讲 Kharon | 新读者不会看到两套叙事 |
| 4 | 命名退场：仓库 / 包 / 站点统一 Kharon | `elab` 只出现在 NOTICE 的协议沿革里 |

---

## 8. 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 全量重编 400–500 s，驾驶舱里像卡死 | 用户以为挂了 | 流式逐行输出（`run.py:_run_decl` 已支持）+ 显式提示"全量重编中，约 7 分钟" |
| R2 | ninja lock 残留导致连续失败 | 编译永久失败 | 构建前探测 `ninja.exe` 进程，提示而非自动杀（杀进程风险高） |
| R3 | Zephyr SDK 路径因版本升级漂移 | 全体项目构建失败 | 路径只在 `kharon.host.yaml`，`doctor` 逐项校验并给修复提示 |
| R4 | ESP32-S31 依赖未合入 PR 的 SoC 移植 | 该板不可用 | 该项目标 `experimental: true`，面板显式标注 |
| R5 | `west flash` 无统一 verify 语义 | 烧录"成功"但实际没写进去 | 禁用 `debug_verify`，改用 `monitor` 闭环作为唯一判据（不做假证据） |
| R6 | classic 冻结不彻底，实际上还在维护 | 退场无限延期，两边叙事互相稀释 | **机制冻结**：CI 降为 informational、冻结区加 `★` 标记、`docs/legacy/` 建档；新需求一律答复"等 Kharon" |
| R7 | 冻结期 bug 不修，存量用户受损 | 口碑风险 | 冻结前把已知问题存档并公开（`docs/legacy/classic-已知问题.md`）；npm `deprecate` 时明示"请迁 Kharon" |
| R8 | classic 删除后裸机工程无处可去 | 产品能力收缩，用户流失 | F3 前定夺：放弃，或把 `adapt.py` 转成「CubeMX → Zephyr 迁移器」（§6.4） |

---

## 9. 不做

- 不 fork 仓库，不复制内核（复制出来的 40% 是必死代码）
- 不做 Kconfig 图形配置器
- 不让 elab 包管 west 模块
- **不为 classic 投入任何资源**：不修 bug、不接新芯片、不加 provider、不加面板 —— 唯一例外是 Kharon 共享内核的 bug 顺手修
- 不把 `distros/` 当长期架构（它是过渡脚手架，F3 删除）
- 不提前删 classic（F3 之前删 = 失去回归基线）
- 不在内核里写任何 `distro ==` 分支

---

## 10. 执行清单

- [ ] F0-1 `docs/legacy/classic-已知问题.md`（冻结前扫一遍问题记录，不阻塞 Kharon 的不修）
- [ ] F0-2 classic 专属测试从 blocking CI 降为 informational
- [ ] F0-3 classic 教程加"已冻结"标注；README 主线改 Kharon
- [ ] P0-1 四份 yaml 草案落在仓库（`providers/zephyr`、`packs/zephyr`、`projects/kharon`、`kharon.host.yaml`）
- [ ] P0-2 现有内核跑通 `doctor,build`（借载具，不改载具）
- [ ] P0-3 `--dry-run` 与 `build.ps1` 命令逐字对照
- [ ] P0-4 烧录 + 串口闭环
- [ ] P1-1 `distro.py` + 两份 `distro.yaml`
- [ ] P1-2 `kharon` 入口
- [ ] P1-3 K7 发现根可配置
- [ ] P1-4 Zephyr log profile
- [ ] P2-1 K8 插件透传 + `dsh-kharon-cockpit`
- [ ] P2-2 K1 + K2 调试后端
- [ ] P2-3 K5 Zephyr 工程指纹（**新建** `zephyr_probe.py`，不复用 classic `adapt.py`——它要在 F3 被删）
- [ ] P2-4 K9 多板矩阵
- [ ] P3-1 K3 `doctor_deep`
- [ ] P3-2 K4 `kind: framework` 进 schema
- [ ] P3-3 `west list` 工作区面板
- [ ] P3-4 K6 Zephyr 线程视图
- [ ] P3-5 kharon 站点 + npm 发布
- [ ] P4-1 classic 市场条目下架 + npm `deprecate`（不删包）
- [ ] P4-2 删 `distros/classic` / `adapt.py` / `builder.py` 注入层 / `ota_layout.py` 对账 / `elab.cmd`
- [ ] P4-3 `docs/legacy/` 归档 + README 只讲 Kharon
- [ ] P4-4 命名统一 Kharon（`elab` 仅留 NOTICE 协议沿革）
