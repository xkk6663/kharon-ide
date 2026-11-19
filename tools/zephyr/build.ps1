# Fast build wrapper for this project: ccache + fixed -j (default 8) + timing.
# ASCII only on purpose, but it must run under **pwsh 7**: env.ps1 (which this sources)
# is UTF-8 without BOM and contains Chinese, so Windows PowerShell 5.1 mangles it.
#
# Usage:
#   .\tools\zephyr\build.ps1 -App .\applications\esp32s31\korvo_sys -Board esp32s3_devkitc/esp32s3/procpu
#       -> default build dir: build\esp32s3\korvo_sys\default
#   .\tools\zephyr\build.ps1 -App .\applications\esp32s31\korvo_sys -BuildDir .\build\esp32s31\korvo_sys\s31-debug
#   .\tools\zephyr\build.ps1 -App .\west\zephyr\samples\net\wifi\shell `
#       -ExtraDtcOverlay .\applications\esp32s3\korvo_wifi\boards\esp32s3_devkitc_esp32s3_procpu_usb_serial.overlay `
#       -BuildDir .\build\samples\esp32s3\shell\default
#   .\tools\zephyr\build.ps1 ... -Flash -Port COM11
#   .\tools\zephyr\build.ps1 ... -NoCcache          # measure without ccache
#   .\tools\zephyr\build.ps1 ... -Pristine          # pristine rebuild (slow: deletes build dir)
#   .\tools\zephyr\build.ps1 ... -AllowOutsideBuild # escape hatch: build dir outside build\ (refused by default)

param(
    [Parameter(Mandatory = $true)][string]$App,
    [string]$Board = "esp32s3_devkitc/esp32s3/procpu",
    [string]$BuildDir = "",
    [int]$Jobs = 8,
    [string]$ExtraDtcOverlay = "",
    [string]$ExtraConfFile = "",
    [switch]$NoCcache,
    [switch]$Pristine,
    [switch]$Flash,
    [string]$Port = "COM11",
    # Runner to use for -Flash: "esp" (default for ESP32 boards), "openocd" (DAP-Link/ST-Link),
    # "pyocd". Empty = infer from -Board.
    [string]$Runner = "",
    # Extra OpenOCD config file; empty = boards\common\dap_link_openocd.cfg for STM32 boards.
    [string]$OpenocdConfig = "",
    [switch]$AllowOutsideBuild,
    # Skip the post-build prune (clean-builds.ps1) that reclaims stale build dirs.
    [switch]$NoPrune
)

$ErrorActionPreference = "Stop"
# This script lives in <root>\tools\zephyr (moved there on 2026-11-18), so the repo
# root is two levels up. env.ps1 below re-derives it from kharon.host.yaml anyway.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# Single source of truth for the toolchain environment.
# Remember where the caller stood: env.ps1 cd's into the west workspace, so relative
# -App / -BuildDir must be resolved against the invocation directory, not zephyr_k.
$invokeDir = (Get-Location).Path
# env.ps1 sits next to this script; it re-derives $root from kharon.host.yaml,
# exposes the west workspace as $ws, and cd's into it.
. (Join-Path $PSScriptRoot "env.ps1") | Out-Null
if ($ws) { Set-Location -LiteralPath $ws }

# Resolve paths to absolute ones before use (relative to where the caller ran the script).
$App = if ([IO.Path]::IsPathRooted($App)) { $App } else { Join-Path $invokeDir $App }
$App = (Resolve-Path -LiteralPath $App).Path
if ([string]::IsNullOrWhiteSpace($BuildDir)) {
    # All build output lives under <root>\build\<chip>\<app>\<variant> (samples: build\samples\<chip>\<sample>\<variant>).
    $chip = if ($Board -match 'esp32s31') { 'esp32s31' } elseif ($Board -match 'esp32s3') { 'esp32s3' } elseif ($Board -match 'stm32f1') { 'stm32f1' } elseif ($Board -match 'stm32|blackpill') { 'stm32f4' } else { 'unknown' }
    $appName = [IO.Path]::GetFileNameWithoutExtension($App)
    $sampleRoot = if ($ws) { $ws } else { Join-Path $root 'zephyr_k' }
    $BuildDir = if ($App.StartsWith($sampleRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Join-Path $root ("build\samples\$chip\$appName\default")
    } else {
        Join-Path $root ("build\$chip\$appName\default")
    }
} elseif (-not [IO.Path]::IsPathRooted($BuildDir)) {
    $BuildDir = Join-Path $invokeDir $BuildDir
}

# Single sink: every artifact must live under <root>\build.
# A build dir elsewhere silently recreates the old scattered layout, so refuse it
# unless the caller opts in explicitly for a throwaway experiment.
$buildRoot = [IO.Path]::GetFullPath((Join-Path $root 'build'))
$resolved = [IO.Path]::GetFullPath($BuildDir)
$sep = [IO.Path]::DirectorySeparatorChar
$inside = $resolved.StartsWith($buildRoot + $sep, [StringComparison]::OrdinalIgnoreCase)
if (-not $inside) {
    if ($AllowOutsideBuild) {
        Write-Warning "Build dir is OUTSIDE ${buildRoot}: $resolved"
    } else {
        throw "Build dir must be under $buildRoot (got: $resolved). Use -AllowOutsideBuild only for a throwaway experiment."
    }
}
$BuildDir = $resolved

$ccacheArgs = @()
if (-not $NoCcache) {
    if (Get-Command ccache -ErrorAction SilentlyContinue) {
        $ccacheArgs = @(
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_ASM_COMPILER_LAUNCHER=ccache"
        )
    } else {
        Write-Warning "ccache not on PATH, building without cache"
    }
}
if (-not [string]::IsNullOrWhiteSpace($ExtraDtcOverlay)) {
    $ExtraDtcOverlay = (Resolve-Path -LiteralPath $ExtraDtcOverlay).Path
    $ccacheArgs += "-DEXTRA_DTC_OVERLAY_FILE=$ExtraDtcOverlay"
}
if (-not [string]::IsNullOrWhiteSpace($ExtraConfFile)) {
    $ExtraConfFile = (Resolve-Path -LiteralPath $ExtraConfFile).Path
    $ccacheArgs += "-DEXTRA_CONF_FILE=$ExtraConfFile"
}

$sw = [Diagnostics.Stopwatch]::StartNew()
if ($Pristine) {
    Write-Host "== configure (pristine) =="
    & west build -b $Board $App -d $BuildDir -p always --cmake-only @ccacheArgs
} else {
    Write-Host "== configure =="
    & west build -b $Board $App -d $BuildDir --cmake-only @ccacheArgs
}
if ($LASTEXITCODE -ne 0) { throw "configure failed" }
$cfgSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
$sw.Restart()

Write-Host "== build (-j $Jobs) =="
& ninja -C $BuildDir -j $Jobs
if ($LASTEXITCODE -ne 0) { throw "build failed" }
$buildSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
$sw.Stop()

$bin = Join-Path $BuildDir "zephyr\zephyr.bin"
$binSize = if (Test-Path -LiteralPath $bin) { (Get-Item -LiteralPath $bin).Length } else { 0 }
Write-Host ("OK  configure={0}s  build={1}s  total={2}s  bin={3} B" -f $cfgSec, $buildSec, [math]::Round($cfgSec + $buildSec, 1), $binSize)
Write-Host ("    build dir: {0}" -f $BuildDir)

if ($Flash) {
    Write-Host "== flash =="
    if ([string]::IsNullOrWhiteSpace($Runner)) {
        $Runner = if ($Board -match 'stm32|blackpill') { 'openocd' } else { 'esp' }
    }
    if ($Runner -eq 'openocd') {
        # DAP-Link is a CMSIS-DAP probe, while the upstream board cfg in support\openocd.cfg
        # is ST-Link specific, so an explicit config is always required here.
        if ([string]::IsNullOrWhiteSpace($OpenocdConfig)) {
            $OpenocdConfig = if ($Board -match 'stm32f1') { "$root\applications\stm32f1\common\dap_link_openocd.cfg" } else { "$root\applications\stm32f4\common\dap_link_openocd.cfg" }
        }
        if (-not (Test-Path -LiteralPath $OpenocdConfig)) {
            throw "OpenOCD config not found: $OpenocdConfig"
        }
        & west flash -d $BuildDir --runner openocd --config $OpenocdConfig
    } elseif ($Runner -eq 'pyocd') {
        & west flash -d $BuildDir --runner pyocd
    } else {
        & west flash -d $BuildDir --esp-device $Port
    }
    if ($LASTEXITCODE -ne 0) { throw "flash failed" }
    Write-Host "flashed OK"
}

# Reclaim stale build dirs once the disk gets tight (direct delete: never the
# Recycle Bin). Keeping the just-built dir, and only doing work when free space is
# below -TriggerFreeGB, so a normal build pays almost nothing.
if (-not $NoPrune) {
    try {
        & (Join-Path $PSScriptRoot "clean-builds.ps1") -Root $root -KeepDays 7 -TriggerFreeGB 25 -Protect $BuildDir -Delete -Quiet
    } catch {
        Write-Warning ("prune skipped: " + $_.Exception.Message)
    }
}
