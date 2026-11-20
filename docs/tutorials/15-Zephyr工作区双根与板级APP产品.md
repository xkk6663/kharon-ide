# 15 · Zephyr 工作区双根与 Board / APP / Product 分层

> **现行口径（2026-11-20）**：本仓已经完成“IDE 根”和“Zephyr 工作区根”的物理拆分。
> 本篇是**结构事实的入口**：板级真相在哪、APP 只该放什么、产品 target 怎么选。
> 旧教程里的 `west/`、`applications/**/boards/**`、`CMakePresets.json`、
> `projects/kharon/*.yaml` 都是拆分前写法，按本篇末尾的映射表替换。
>
> 读完后你应该能：① 用 `kharon where` 说清两个根；② 在驾驶舱“架构”页看懂一块板
> 上跑了哪些 APP、哪些产品 target；③ 给同一份 APP 加第二块板，而不是复制产品 yaml。

---

## 0. 一句话版本

**IDE 管“怎么编译—烧录—调试—展示”，Zephyr 工作区管“板子、APP、产品组合”；板级差异只进
`kharon-boards`，APP 只放自己的源码和少量 overlay，产品用 `targets[]` 把同一份 APP 绑到多块板。**

| 层 | 唯一宿主 | 只回答一个问题 | 不该出现在哪 |
|---|---|---|---|
| **Board** | `zephyr-workspace/kharon-boards/boards/**` | 这块 PCB 上有什么、console/alias 是什么 | APP 的 `boards/` 里、`services/elab/**` |
| **APP** | `zephyr-workspace/applications/<family>/<app>/**` | 这份源码编什么、需要哪些能力 | 完整 `board.yml` / 整份板级 DTS / `CMakePresets.json` |
| **Product** | `zephyr-workspace/kharon-manifest/products/kharon/*.yaml` | 哪个 APP 配哪块板、产物/串口/判据 | IDE 源码里、APP 源码里 |
| **Toolchain** | `providers/official/zephyr/**` | 命令怎么拼 | 产品 yaml / 芯片包 |
| **Chip** | `packs/official/zephyr/**` | 芯片内核/内存/指纹 | 内核代码 / 驾驶舱 |

---

## 1. 双根：先认清两个路径

```text
C:\Users\xiao1\Desktop\ELAB\kharon\
├── kharon-ide\                 ← IDE_ROOT：内核、驾驶舱、provider、host 配置
│   ├── services\elab\**
│   ├── cockpit\**
│   ├── config\host.kharon.profile.yaml
│   └── .work\                 ← 运行产物（不入库）
└── zephyr-workspace\           ← ZEPHYR_ROOT：west topdir + 板 + APP + 产品
    ├── .west\
    ├── zephyr\                 ← ZEPHYR_BASE
    ├── kharon-boards\          ← 自研板 module（板级真相）
    ├── applications\           ← APP（workspace application）
    ├── elab-app-common\        ← APP 公共层 module
    ├── kharon-manifest\products\kharon\   ← 产品登记
    └── build\                  ← 默认构建产物
```

一条命令确认，不要靠记忆：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon\kharon-ide
.\kharon.cmd where
```

实测输出（2026-11-20）：

```text
ide_root       C:\Users\xiao1\Desktop\ELAB\kharon\kharon-ide
zephyr_root    C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace
workspace      C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace
base           C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace\zephyr
boards_local   C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace\kharon-boards\boards
```

> 变量口径：`${ELAB_ROOT}` 仍指向 **IDE 根**（历史兼容名），`${ZEPHYR_ROOT}` 指工作区根。
> 产品 yaml 里写 `root: ${ZEPHYR_ROOT}/applications/...`，不要再写 `${ELAB_ROOT}/applications`。

### 1.1 为什么拆

- `west update`、Zephyr 内核、模块、build 产物都属于工作区，换 Zephyr 版本不应该动 IDE 代码；
- IDE 的 `services/`、`cockpit/`、`providers/` 要独立升级，也不应该被 `west update` 冲掉；
- 板级 module 和产品 manifest 与工作区一起走，`west boards` 才能看见自研板。

拆完之后，`kharon ws -p <工程>` 会显示 **`应用形态 workspace`**、构建 cwd 是
`zephyr-workspace`，而不是旧文档里的 `standalone` / `west/`：

```text
工程        C:/.../zephyr-workspace/applications/stm32f4/f411_breath
应用形态    workspace —— 工作区应用 workspace application
构建位置参  applications/stm32f4/f411_breath
构建 cwd    C:/.../zephyr-workspace
```

---

## 2. Board：板级真相只有一份

### 2.1 目录长什么样

```text
zephyr-workspace\kharon-boards\
├── zephyr\module.yml           ← 标准 Zephyr module，告诉 west 板根在哪
└── boards\
    ├── espressif\
    │   ├── korvo1\
    │   │   ├── board.yml
    │   │   ├── korvo1_hpcore.dts
    │   │   └── ...
    │   └── esp32s3_korvo\
    └── ...
```

自研板的 `board.yml` 是 `west boards` 的发现入口；主 DTS 固定 console、aliases、
板载外设。APP 侧只允许 `boards/<board>.overlay` 描述“这个 APP 额外要开的外设”。

### 2.2 判断一条事实该放哪

| 事实 | 放 Board | 放 APP overlay |
|---|---|---|
| console / shell UART、baud | ✅ | ❌ |
| `led0` / `sw0` / `watchdog0` alias | ✅ | ❌ |
| 板载 I2C/SPI/ADC 总线与片选 | ✅ | ❌ |
| 某 APP 临时多接一颗传感器 | ❌ | ✅ |
| 电机 PWM 演示的额外节点 | ❌ | ✅ |

一句话：**换 APP 不应该改板子；换板子不应该改 APP 源码**。如果一条板级事实随着 APP 变，
它本来就该是 APP 的 overlay。

### 2.3 驾驶舱里看板

打开 `http://127.0.0.1:8398/` → 顶栏「架构」：

- 左列「板级真相」：板 id、vendor、SoC、`console`、aliases、`zephyr,user` 属性、
  板级文件数、关联 APP/产品 target；
- 卡片上直接列出这块板当前被哪些 APP 使用，避免“板名对、外设不对”。

---

## 3. APP：freestanding，但不再自带整块板

### 3.1 最小 APP

```text
zephyr-workspace\applications\stm32f4\f411_breath\
├── CMakeLists.txt
├── prj.conf
├── src\main.c
└── boards\blackpill_f411ce.overlay   ← 可选：只写 APP 额外外设
```

`CMakeLists.txt` 只写自己的源文件：

```cmake
cmake_minimum_required(VERSION 3.20)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(f411_breath)
target_sources(app PRIVATE src/main.c)
```

**不再有** `CMakePresets.json`。构建参数由产品 yaml + provider + host 解析，
`kharon run --dry-run` 给出的 argv 就是实跑 argv。

### 3.2 `app.yaml`：只声明 APP 自己的边界

产品 APP 可以不写；实验台/样例建议写：

```yaml
name: korvo_lab
kind: lab               # lab / sample / product
board_scope: single     # portable / single
board: korvo1           # board_scope=single 时声明唯一允许的板
```

| 字段 | 作用 | 门禁 |
|---|---|---|
| `kind: product` | 进产品矩阵的正常 APP | 不许出现寄存器/固定 pin 硬编码 |
| `kind: lab` | 教学实验台，允许原始寄存器 | 必须显式 allowlist，不进产品矩阵 |
| `kind: sample` | 模板/示例 | 不绑定产品 |
| `board_scope: portable` | 多板可移植 | `boards/` 只放 overlay |
| `board_scope: single` | 只允许一块板 | 必须写 `board:` |

### 3.3 APP 里禁止出现什么

| 禁止 | 原因 |
|---|---|
| `applications/**/board.yml` | 板级真相重复，`west boards` 会看到两块同名板 |
| `applications/**/CMakePresets.json` | 绝对路径/旧 `ZEPHYR_BASE` 的来源，已统一删除 |
| 完整板级 `*_procpu.dts` / `*_hpcore.dts` | 板定义必须回 `kharon-boards` |
| `GPIO0_BASE` / `sys_read32()` / `.pin = <数字>` | 产品 APP 不许直接碰寄存器/固定脚 |
| `../../common` / `applications/common` | 公共层走 `elab-app-common` module |

门禁脚本：

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon\kharon-ide
$env:PYTHONPATH='services'
.\.deps\venv\Scripts\python.exe scripts\app_boundary_check.py --root C:\Users\xiao1\Desktop\ELAB\kharon\zephyr-workspace
```

通过时输出 `[boundary] OK：14 个 APP，产品清单无 target 冲突`。

---

## 4. Product：targets[] 是“同一份 APP 配多块板”的唯一写法

### 4.1 单板产品

```yaml
name: f411_breath
root: ${ZEPHYR_ROOT}/applications/stm32f4/f411_breath
pack: zephyr/framework
chip: blackpill_f411ce
archetype: zephyr
provider: zephyr/west@>=1 <2
build:
  work_dir: ${ZEPHYR_ROOT}/build/stm32f4/f411_breath/default
```

### 4.2 多板产品：一份源码，两个 target

`kharon-manifest/products/kharon/korvo_sys.yaml`：

```yaml
name: korvo_sys
root: ${ZEPHYR_ROOT}/applications/esp32s31/korvo_sys
pack: zephyr/framework
provider: zephyr/west@>=1 <2

targets:
  - id: s31
    chip: korvo1
    build:
      work_dir: ${ZEPHYR_ROOT}/build/esp32s31/korvo_sys/default
  - id: s3
    chip: esp32s3_korvo
    build:
      work_dir: ${ZEPHYR_ROOT}/build/esp32s3/korvo_sys/default
```

规则：

- 顶层 `chip` 和多 target 二选一；同时写且值不同会直接报冲突。
- 每个 target 必须有唯一 `id` 与 `chip`；`chip` 必须能解析到一块板。
- 不传 `--target` 时，多 target 产品必须报错并列出可选 target，不许猜。
- 单 target 产品仍可省略 `targets[]`，旧产品零改动。

### 4.3 命令行选 target

```powershell
.\kharon.cmd run -p korvo_sys --target s3  --steps build --dry-run
.\kharon.cmd run -p korvo_sys --target s31 --steps build --dry-run
.\kharon.cmd run -p korvo_sys --steps build --dry-run   # 期望：报“需要 --target”
```

解析结果（实测）：

| target | board target | work_dir |
|---|---|---|
| `s3` | `esp32s3_korvo/esp32s3/procpu` | `build/esp32s3/korvo_sys/default` |
| `s31` | `korvo1/esp32s31/hpcore` | `build/esp32s31/korvo_sys/default` |

### 4.4 驾驶舱里选 target

顶栏「架构」→ 右列「产品 target」：

1. 每个 `product / target` 显示 `target → board / app` 与 chip；
2. 点「设为当前」，左栏工程卡和阶段轨都会换成该 target；
3. 点「预览 build」，底部直接给出该 target 的 argv 与 work_dir；
4. 顶栏工作区会记住，刷新后仍停在「架构」。

这是**只读事实 + 选择入口**：真正的执行仍走阶段轨或 CLI，不在页面里直接改产品 yaml。

---

## 5. 新增一块板 / 一个 APP 的正确顺序

### 5.1 新板

1. 在 `kharon-boards/boards/<vendor>/<board>/` 建 `board.yml` + 主 DTS + pinctrl；
2. 跑 `.\kharon.cmd deps --check` 确认板 module 已接入（它会用正确的 `EXTRA_ZEPHYR_MODULES` 跑 `west boards`）；
3. 先用 `applications/_template` 或 `blinky` 跑一次最小镜像，确认 console 能出字；
4. 再接业务 APP，**不要**先把整块板复制进 APP。

### 5.2 同一 APP 上第二块板

1. 只补 `applications/<app>/boards/<new_board>.overlay`（如确实需要额外外设）；
2. 在产品 yaml 加一个 `targets[]` 条目，写新 `id` / `chip` / `work_dir`；
3. `kharon run -p <product> --target <new> --steps build --dry-run` 看 argv；
4. 驾驶舱「架构」页确认新 target 出现在产品卡上。

### 5.3 新 APP

1. 从 `applications/_template` 复制目录，目录名与 `name:` 逐字相同；
2. 写 `app.yaml`（`kind` / `board_scope`，实验台再加 `board`）；
3. 在 `kharon-manifest/products/kharon/` 登记产品；
4. 跑 `app_boundary_check.py`，再 `kharon run --steps doctor,build`。

---

## 6. 旧教程 → 现行口径映射

| 旧写法 | 现在 | 说明 |
|---|---|---|
| `kharon-ide/west/` | `zephyr-workspace/` | 工作区整体搬到 IDE 旁边 |
| `west/zephyr` | `zephyr-workspace/zephyr` | `ZEPHYR_BASE` |
| `kharon-ide/applications/...` | `zephyr-workspace/applications/...` | APP 属于工作区 |
| `applications/**/boards/xtensa/<board>/` | `kharon-boards/boards/<vendor>/<board>/` | 板级真相上移 |
| `applications/**/CMakePresets.json` | 无 | 构建参数由产品 + provider + host 解析 |
| `projects/kharon/*.yaml` | `kharon-manifest/products/kharon/*.yaml` | 产品登记进工作区 manifest |
| `zephyr_modules/elab-boards` | `kharon-boards/` | 自研板 module |
| `applications/common` | `elab-app-common/` | 公共层 module |
| `chip: esp32s3_korvo` + 两份产品 yaml | 一份产品 + `targets[]` | 同一 APP 多板 |
| 顶栏 classic ⇄ Zephyr 开关 | 无 | 生产只保留 `kharon.yaml`，`POST /api/mode` 已删除 |

> 旧教程（`docs/zephyr-learn/课程同款/02`、`05` 等）保留课程对照与实机证据，
> **结构性段落按本表读**；要照着做时以本篇和 `docs/tutorials/00` 为准。

---

## 7. 验收清单

```powershell
cd C:\Users\xiao1\Desktop\ELAB\kharon\kharon-ide
.\kharon.cmd where
.\kharon.cmd ws -p f411_breath
.\kharon.cmd run -p f411_breath --steps build --dry-run
.\kharon.cmd run -p korvo_sys --target s3 --steps build --dry-run
```

- [ ] `where` 的 `ide_root` 与 `zephyr_root` 是两个不同目录；
- [ ] `ws -p f411_breath` 显示 `应用形态 workspace`，构建 cwd 在 `zephyr-workspace`；
- [ ] `applications/**` 里没有 `board.yml`、`CMakePresets.json`、整份板级 DTS；
- [ ] 驾驶舱「架构」页显示 `5 板 / 14 APP / 13 产品 / 0 warnings`（数量随登记变化）；
- [ ] 多 target 产品能点「设为当前」，阶段轨命令带正确 `-b` 与 work_dir；
- [ ] `app_boundary_check.py` 通过。

---

**下一篇**：[13 · Zephyr OTA 局域网升级](./13-Zephyr_OTA局域网升级.md) 看产品如何复用同一套
Board/APP 事实；排查构建与串口问题回到 [10 · Zephyr 模式上手](./10-Zephyr模式上手.md)。
