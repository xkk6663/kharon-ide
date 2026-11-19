# Zephyr 统一环境（pwsh 7）：. .\env.ps1
# 一个 SDK、三个目标：riscv64-zephyr-elf（Korvo-1 / S31）+ xtensa-espressif_esp32s3_zephyr-elf（ESP32-S3）
#                    + arm-zephyr-eabi（STM32F411CEU6 / F103C8T6，Cortex-M4 / M3）
# 三块板共用这一套变量，不需要来回切；只要确认 CROSS_COMPILE / TOOLCHAIN_HOME 是清掉的。
#
# 换机器怎么用：先跑 kharon bootstrap（重建 west 工作区），再点源本文件。
# 本文件不含任何个人绝对路径：仓库根从这里推出的，SDK 从环境变量 / tools\local.env.ps1 / kharon.host.yaml / 常见位置找。

# 仓库根：本脚本住在 <root>\tools\zephyr\env.ps1，所以"上两级"就是仓库根。
# 被 `. .\env.ps1` 点源时 $PSCommandPath 仍指向本文件，比 $PSScriptRoot 可靠。
$PSScriptPath = $PSCommandPath
function Find-RepoRoot {
    $cands = @()
    # <root>\tools\zephyr\env.ps1 → 上三级才是仓库根（file → zephyr → tools → root）
    if ($PSScriptPath) { $cands += (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptPath))) }
    foreach ($start in @($PSScriptPath, $PSScriptRoot, (Get-Location).Path)) {
        if (-not $start) { continue }
        $p = $start
        if (-not (Test-Path -LiteralPath $p -PathType Container)) { $p = Split-Path -Parent $p }
        for ($i = 0; $i -lt 8 -and $p; $i++) {
            if (Test-Path -LiteralPath (Join-Path $p 'kharon.host.yaml') -PathType Leaf) { $cands += $p }
            $p = Split-Path -Parent $p
        }
    }
    # 必须真的带 kharon.host.yaml 才算仓库根（否则退化成 tools\ 这种半截路径）
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath (Join-Path $c 'kharon.host.yaml') -PathType Leaf)) { return $c }
    }
    return $null
}
$root = Find-RepoRoot
if (-not $root) { throw "找不到仓库根（认 kharon.host.yaml 那一层）：请先 cd 到仓库根再 . .\tools\zephyr\env.ps1" }

# ── west 工作区：以 `.west/config` 为唯一判据（拓扑由清单决定，不认目录名）──
#   顺序：环境变量 > <root>\west（现役） > 旧名兼容（zephyr_k / zephyr-s31）
$ws = $env:ZEPHYR_WORKSPACE
if (-not $ws) {
    foreach ($n in @('west', 'zephyr_k', 'zephyr-s31')) {
        $cand = Join-Path $root $n
        if (Test-Path -LiteralPath (Join-Path $cand '.west\config') -PathType Leaf) { $ws = $cand; break }
        if (Test-Path -LiteralPath (Join-Path $cand 'zephyr\west.yml') -PathType Leaf) { $ws = $cand; break }
    }
}
if (-not $ws) { throw "找不到 west 工作区：$root 下没有带 .west\config 的目录（先跑 kharon bootstrap，或设 `$env:ZEPHYR_WORKSPACE）" }

# ── Zephyr SDK：环境变量 > tools\local.env.ps1 > kharon.host.yaml > 常见安装位置 ──
$sdk = $env:ZEPHYR_SDK_INSTALL_DIR
if (-not $sdk -and (Test-Path -LiteralPath (Join-Path $root 'tools\local.env.ps1'))) {
    . (Join-Path $root 'tools\local.env.ps1')
    if ($LocalSdkDir) { $sdk = $LocalSdkDir }
}
# 主机事实源（kharon.host.yaml）的 sdk 是权威值；只读那一行，不解析整份 YAML
$hostYaml = Join-Path $root 'kharon.host.yaml'
if (-not $sdk -and (Test-Path -LiteralPath $hostYaml -PathType Leaf)) {
    $m = Select-String -LiteralPath $hostYaml -Pattern '^\s*sdk:\s*([^\s#]+)' | Select-Object -First 1
    if ($m) {
        $sdk = $m.Matches[0].Groups[1].Value.Trim()
        $sdk = $sdk.Replace('${ELAB_ROOT}', $root)
    }
}
if (-not $sdk) {
    foreach ($base in @('C:\', $env:USERPROFILE, "$env:USERPROFILE\zephyr-sdk")) {
        $hit = Get-ChildItem -LiteralPath $base -Directory -Filter 'zephyr-sdk-*' -ErrorAction SilentlyContinue |
               Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'gnu\arm-zephyr-eabi\bin') } |
               Select-Object -First 1
        if ($hit) { $sdk = $hit.FullName; break }
    }
}
if (-not $sdk) { Write-Warning "没找到 Zephyr SDK：装 1.0.1 后跑一次 kharon bootstrap，或设 `$env:ZEPHYR_SDK_INSTALL_DIR" }

# ── PATH：只加真实存在的目录；先认现行 .deps\，再回退旧 .venv\ / .tools\ ──
$pathParts = @()
foreach ($p in @("$root\.deps\venv\Scripts", "$root\.venv\Scripts",
                 "$root\.deps\tools\cmake\cmake-3.30.2-windows-x86_64\bin", "$root\.tools\cmake\cmake-3.30.2-windows-x86_64\bin",
                 "$root\.deps\tools\ninja", "$root\.tools\ninja",
                 "$root\.deps\tools\ccache\ccache-4.14.3-windows-x86_64", "$root\.tools\ccache\ccache-4.14.3-windows-x86_64")) {
    if (Test-Path -LiteralPath $p) { $pathParts += $p }
}
if ($sdk) {
    foreach ($t in @('riscv64-zephyr-elf', 'xtensa-espressif_esp32s3_zephyr-elf', 'arm-zephyr-eabi')) {
        $p = Join-Path $sdk "gnu\$t\bin"
        if (Test-Path -LiteralPath $p) { $pathParts += $p }
    }
}
foreach ($p in $pathParts) { if (Test-Path -LiteralPath $p) { $env:PATH = "$p;$env:PATH" } }

$env:ZEPHYR_BASE = "$ws\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = 'zephyr'
if ($sdk) { $env:ZEPHYR_SDK_INSTALL_DIR = $sdk }

# ccache：跨构建目录复用 .o；不设的话默认落在 %LOCALAPPDATA%，这里固定到项目内便于清理/统计
$ccacheDir = if (Test-Path -LiteralPath "$root\.deps") { "$root\.deps\ccache" } else { "$root\.ccache" }
$env:CCACHE_DIR = $ccacheDir
$env:CCACHE_MAXSIZE = '3G'
$env:CCACHE_COMPRESS = '1'

# 编译并行度：默认 8；内存小的机器用 $env:ZEPHYR_BUILD_JOBS 覆盖（-j18 会触发 cc1 OOM，见 docs\参考\编译速度优化.md）
if (-not $env:ZEPHYR_BUILD_JOBS) { $env:ZEPHYR_BUILD_JOBS = '8' }

# 关键：只要残留这两个变量，CMake 会退回 cross-compile 模式
Remove-Item Env:\CROSS_COMPILE  -ErrorAction SilentlyContinue
Remove-Item Env:\TOOLCHAIN_HOME -ErrorAction SilentlyContinue

# DAP-Link（CMSIS-DAP）烧录用的 OpenOCD：xPack 发行版自带 libusb，不需要 ST-Link 驱动。
# 没装也不影响编译，只影响 `west flash --runner openocd`。
$openocdBin = $env:OPENOCD_DIR
if (-not $openocdBin) {
    $hit = Get-ChildItem -LiteralPath 'C:\' -Directory -Filter 'xpack-openocd-*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $openocdBin = Join-Path $hit.FullName 'bin' }
}
$hasOpenocd = $openocdBin -and (Test-Path -LiteralPath (Join-Path $openocdBin 'openocd.exe'))
if ($hasOpenocd) { $env:PATH = "$openocdBin;$env:PATH" }

Set-Location -LiteralPath $ws

Write-Host "Zephyr env ready"
Write-Host "  WEST_WORKSPACE           = $ws"
Write-Host "  ZEPHYR_BASE              = $env:ZEPHYR_BASE"
Write-Host "  ZEPHYR_TOOLCHAIN_VARIANT = $env:ZEPHYR_TOOLCHAIN_VARIANT"
Write-Host "  ZEPHYR_SDK_INSTALL_DIR   = $(if ($sdk) { $sdk } else { '(未找到)' })"
Write-Host "  CCACHE_DIR               = $env:CCACHE_DIR"
Write-Host "  ZEPHYR_BUILD_JOBS        = $env:ZEPHYR_BUILD_JOBS"
Write-Host "  OPENOCD (DAP-Link)       = $(if ($hasOpenocd) { "$openocdBin\openocd.exe" } else { 'not found (only needed for west flash)' })"

# 常用构建（三块板同一套环境）；产物统一落在项目根的 build\<芯片>\<工程>\<配置>\
#   Korvo-1 : west build -b esp32s31_function_coreboard/esp32s31/hpcore ..\applications\esp32s31\korvo_sys -d ..\build\esp32s31\korvo_sys\s31-debug
#   S3      : west build -b esp32s3_devkitc/esp32s3/procpu          ..\applications\esp32s31\korvo_sys -d ..\build\esp32s3\korvo_sys\s3-debug
#   F411    : west build -b blackpill_f411ce                        ..\applications\stm32f4\f411_breath -d ..\build\stm32f4\f411_breath\default
#
# 推荐用带 ccache + 固定并行度的封装脚本（会自动计时、可选烧录）：
#   .\tools\build.ps1 -App .\applications\esp32s31\korvo_sys -Board esp32s3_devkitc/esp32s3/procpu
#   .\tools\build.ps1 -App .\applications\stm32f4\f411_breath -Board blackpill_f411ce -Flash   # DAP-Link → openocd
