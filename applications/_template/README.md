# applications\_template — 新建工程的骨架（课程同款结构）

第四课在做"项目迁移与新建"，他的做法是 `cp -r boards` + `cp CMakePresets.json`。
这套工程里板级不在你手上（在上游 PR 树里），所以**"迁移"的对象换成了 overlay**：

```
applications\_template\
├── CMakeLists.txt        ← 3 行；改 project() 的名字即可
├── prj.conf              ← 应用级 Kconfig（要什么开什么）
├── boards\
│   └── esp32s31_function_coreboard_esp32s31_hpcore.overlay
│                         ← 板级差异：一行 include 公共片段 + 本工程独有节点
│                             文件名 = 板目标下划线化，Zephyr 按 -b 自动挑，不用传命令行参数
├── CMakePresets.json     ← 4 个 configure preset（s31-debug/s31-release/s3-debug/s3-release）
│                             产物落在 ../../build/<芯片>/_template/<preset>/（不在工程目录里）
└── src\main.c            ← 最小可跑：拿 gpio_dt_spec + 判驱动就绪 + 心跳
```

> 想加 S3 支持？在 `boards\` 里再放一份 `esp32s3_devkitc_esp32s3_procpu.overlay`（参照 `applications\esp32s31\korvo_apps\boards\` 那份），
> 两边共存、`-b` 换板时自动切换。

## 新建一个工程（3 步）

新工程一律放到**芯片目录**下：S31（Korvo-1）→ `applications\esp32s31\`，S3 → `applications\esp32s3\`。
下面以 `applications\esp32s31\korvo_demo` 为例。

```powershell
. C:\Users\xiao1\Desktop\esp32\zephyr-learn\env.ps1
$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"

# 1. 复制骨架到对应芯片目录
Copy-Item -Recurse "$root\applications\_template" "$root\applications\esp32s31\korvo_demo"

# 2. 改工程名（CMakeLists.txt 里的 project(...)）
(Get-Content -LiteralPath "$root\applications\esp32s31\korvo_demo\CMakeLists.txt") `
  -replace 'project\(korvo_template\)', 'project(korvo_demo)' |
  Set-Content -LiteralPath "$root\applications\esp32s31\korvo_demo\CMakeLists.txt" -Encoding utf8

# 3. 改 preset 的产物路径（模板在 applications\_template 是 ../../build\，下沉一层后要变成 ../../../build\ + 新工程名）
$presets = "$root\applications\esp32s31\korvo_demo\CMakePresets.json"
(Get-Content -LiteralPath $presets -Raw) `
  -replace '\.\./\.\./build/(esp32s3[1]?)/_template', '../../../build/$1/korvo_demo' |
  Set-Content -LiteralPath $presets -Encoding utf8

# 4. 先只配一遍，确认"三件套"指对了（不做完整编译，30~90 秒）
west build --cmake-only -b esp32s31_function_coreboard/esp32s31/hpcore `
           "$root\applications\esp32s31\korvo_demo" -d "$root\build\esp32s31\korvo_demo\s31-debug"
```

> ⚠️ 第 3 步必须做。`CMakePresets.json` 里的 `binaryDir` 是**相对源文件目录**写的：模板在 `applications\_template`（往上是两级到项目根），
> 抄到 `applications\<芯片>\<工程>`（往上三级）之后不换算，产物会落到 `applications\build\` 那种不该存在的位置。

## 新建后必须确认的三件事

```powershell
Select-String -LiteralPath "$root\build\esp32s31\korvo_demo\s31-debug\CMakeCache.txt" `
  -Pattern '^(BOARD:|DTC_OVERLAY_FILE:|CONF_FILE:|APPLICATION_SOURCE_DIR:)'
```

四个值应该分别指向：你的板名、**你自己** `boards\` 里那份 overlay、**你自己**的 `prj.conf`、**你自己**的 app 目录。
任何一个指到别的工程，说明 `-d` 构建目录或应用路径写错了。

## 这个模板里刻意留的两个"教材点"

1. `boards\..._hpcore.overlay` 里除了一行 `#include`，还加了一个 `my-pa = &pa_ctrl` 别名——
   用来演示"别名只是代码里的名字，硬件节点还是那个 `pa_ctrl`"。
2. `CMakePresets.json` 是第三课那套 IDE 入口的移植版（`s31-*` / `s3-*` 各两套）。
   **它和 `west build` 是两条并列入口，不要混用同一个构建目录**（presets 的产物落在 `build\<芯片>\<工程>\<preset>\` 下）。

```powershell
cd $root\applications\esp32s31\korvo_demo
cmake --preset s31-debug
cmake --build --preset s31-debug-build
```
