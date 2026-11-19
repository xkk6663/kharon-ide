# 构建 s3_wifi_portal 固件（sysbuild + 镜像头版本注入 + 版本自校验）
#
# 为什么版本号不再写进 prj.conf
# ---------------------------------------------------------------
# 原来把 `CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS="--version=1.N.0"` 放在 prj.conf，
# 每改一次版本就整棵树全量重编：prj.conf 是 Kconfig 的输入，改它就触发
# CMake reconfigure → 重新生成 zephyr/.config 和 autoconf.h → autoconf.h 的
# mtime 变了 → 所有 .o 全部过期。实测 ccache 的 direct hit 只有 0.71%
# （preprocessed hit 99.3%），说明每个文件都要重跑一遍预处理器，
# 819 个文件的工程就是几十秒到一分钟的纯等待，改一个版本号不值这个钱。
#
# 现在改成"构建后自己签名"：
#   Zephyr 产出的是未签名的 zephyr.bin（已含 0x20 的 MCUboot 头区），
#   这里用 imgtool 以真实版本重签一份，覆盖 zephyr.signed.bin。
#   参数从构建目录里抓过一次原样照抄（见下面的 imgtool 命令），
#   所以镜像布局（header 0x20 / slot 1376256 / align 4）与 Zephyr 自己签的一致。
#
# 收益：改 OTA_LOCAL_VERSION 只重编 ota.c/main.c 这几个包含 ota.h 的文件，
#      再加一次约 0.4 s 的 RSA 签名；prj.conf 从此不动。
#
# 用法: pwsh -File tools\build_ota.ps1 [-Version <n>] [-Fresh]
#   不加 -Version 就用 src\l0\ota.h 里的 OTA_LOCAL_VERSION（单一事实来源）。

param(
    [int]$Version = 0,
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
$sw = [Diagnostics.Stopwatch]::StartNew()

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$app = Join-Path $root "applications\esp32s3\s3_wifi_portal"
$buildDir = Join-Path $root "build\esp32s3\s3ota"
$otaH = Join-Path $app "src\l0\ota.h"

if ($Version -eq 0) {
    $raw = Get-Content -LiteralPath $otaH -Raw
    $m = [regex]::Match($raw, '#define\s+OTA_LOCAL_VERSION\s+(\d+)')
    if (-not $m.Success) { throw "src\l0\ota.h 里找不到 OTA_LOCAL_VERSION" }
    $Version = [int]$m.Groups[1].Value
}
Write-Host "OTA_LOCAL_VERSION = $Version"

# prj.conf 里不该再有版本注入（有的话说明回退到旧做法，会重新触发全量编译）
$prj = Join-Path $app "prj.conf"
$prjRaw = Get-Content -LiteralPath $prj -Raw
if ($prjRaw -match '(?m)^\s*CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS\s*=') {
    Write-Host "警告: prj.conf 里仍有 CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS，"
    Write-Host "      它会强制每次改版本都全量重编；版本注入已改到本脚本的签名步骤。"
}

. (Join-Path $root "env.ps1") | Out-Null
$env:CCACHE_NOCOMPRESS = "1"
$env:CMAKE_BUILD_PARALLEL_LEVEL = "6"

# 先做页面这一步：user_page.html → src\l1\pages.h，并校验页面本身。
# 放在编译前而不是手写 C 字符串，是因为三层引号叠加的转义错误编译不报，只在手机上炸。
$pyTool = Join-Path $root ".venv\Scripts\python.exe"
& $pyTool (Join-Path $root "tools\gen_pages.py") | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "生成 pages.h 失败"; exit 1 }
$nodeExe = "C:\Users\xiao1\AppData\Local\Doubao\User Data\sandbox_runtime\bases\fa694f517e5995233c80a9a8db5212c2\node\node.exe"
& $pyTool (Join-Path $root "tools\page_check.py") --node $nodeExe | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "用户页校验失败（内容边界或 JS 语法），别编译了"; exit 1 }

$west = Join-Path $root ".venv\Scripts\west.exe"
$log = Join-Path $root "logs\build-$Version.log"
$args = @("build", "--sysbuild", "-b", "esp32s3_devkitc/esp32s3/procpu", "-d", $buildDir, $app)
if ($Fresh) { $args = @("build", "--sysbuild", "--pristine", "-b", "esp32s3_devkitc/esp32s3/procpu", "-d", $buildDir, $app) }

& $west @args *> $log
if ($LASTEXITCODE -ne 0) {
    Write-Host "BUILD FAILED (exit $LASTEXITCODE)，日志: $log"
    Select-String -LiteralPath $log -Pattern "error:|malformed" | Select-Object -First 6 |
        ForEach-Object { $_.Line.Trim() }
    exit 1
}
$buildSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)

# ---------------------------------------------------------------- 重签镜像
$py = Join-Path $root ".venv\Scripts\python.exe"
# 从 ZEPHYR_BASE 反推工作区根（env.ps1 里已按实际存在的目录名解析，别写死）
$ws = Split-Path -Parent $env:ZEPHYR_BASE
$imgtool = Join-Path $ws "bootloader\mcuboot\scripts\imgtool.py"
$key = Join-Path $ws "bootloader\mcuboot\root-rsa-2048.pem"
$zBin = Join-Path $buildDir "s3_wifi_portal\zephyr\zephyr.bin"
$signed = Join-Path $buildDir "s3_wifi_portal\zephyr\zephyr.signed.bin"

foreach ($p in @($imgtool, $key, $zBin)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "缺少签名要素: $p" }
}

# 参数照抄 Zephyr 自己那条 imgtool 命令（build.ninja 的 POST_BUILD 里抓的）：
#   未签名 zephyr.bin  →  已签 zephyr.signed.bin
#   header-size 0x20（= CONFIG_ROM_START_OFFSET）、slot-size 1376256（=0x150000）、align 4
$signOut = Join-Path $buildDir "zephyr.resigned.bin"
& $py $imgtool sign `
    --version "1.$Version.0+0" `
    --header-size 0x20 `
    --slot-size 1376256 `
    --align 4 `
    --key $key `
    $zBin $signOut *> (Join-Path $root "logs\sign-$Version.log")

if ($LASTEXITCODE -ne 0) {
    Write-Host "imgtool 签名失败 (exit $LASTEXITCODE)，日志: logs\sign-$Version.log"
    exit 1
}

# 覆盖成带真实版本的签名镜像，这样 `west flash` 烧的就是它
Copy-Item -LiteralPath $signOut -Destination $signed -Force
Remove-Item -LiteralPath $signOut -Force

$hdr = & $py (Join-Path $root "tools\img_hdr.py") $signed | Out-String
$hdr.Trim() | Write-Host
if ($hdr -notmatch "version   : 1\.$Version\.0") {
    Write-Host "版本自校验失败：镜像头不是 1.$Version.0，别推板子"
    exit 2
}
$copy = Join-Path $buildDir "fw_v$Version.signed.bin"
Copy-Item -LiteralPath $signed -Destination $copy -Force
Write-Host "OK: fw_v$Version.signed.bin  $( (Get-Item -LiteralPath $copy).Length ) B"
Write-Host ("耗时: 编译 {0} s / 总计 {1} s" -f $buildSec, [math]::Round($sw.Elapsed.TotalSeconds, 1))
