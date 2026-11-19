# Layout guard for this repo ("single sink" rules).
#
#   [1] applications\**\build              must NOT exist      (products belong to <root>\build)
#   [2] <root>\build-*             must NOT exist      (legacy flat build dirs)
#   [3] zephyr_k\build-*         must NOT exist
#   [4] build\**\CMakeCache.txt    APPLICATION_SOURCE_DIR must be inside applications\ or the west
#                                  workspace (zephyr_k), which also covers sysbuild's own
#                                  sub-caches (zephyr\share\sysbuild\template, mcuboot\boot\zephyr)
#   [5] applications\**\CMakePresets.json  binaryDir must resolve inside <root>\build
#   [warn] build\_unsorted\        someone bypassed the entry point with raw `west build`
#
# Read-only: it never builds, never deletes. Exit code 1 when a hard violation is found.
#
# Usage:
#   pwsh -File .\tools\check-layout.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$apps = Join-Path $root 'apps'
$ws = Join-Path $root 'zephyr_k'
$buildRoot = Join-Path $root 'build'

$fail = @()
$warn = @()
$pass = 0

function Rel([string]$p) { return $p.Replace($root + '\', '') }

# [1] build directories inside applications\
$p = $fail.Count
if (Test-Path -LiteralPath $apps) {
    $stray = Get-ChildItem -LiteralPath $apps -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'build' }
    foreach ($s in $stray) { $fail += ("apps 内有构建目录（产物只进 build\）: " + (Rel $s.FullName)) }
}
if ($fail.Count -eq $p) { $pass++ }

# [2] flat build-* at repo root
$p = $fail.Count
foreach ($f in (Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'build-*' })) {
    $fail += ("仓库根仍有扁平构建目录: " + $f.Name)
}
if ($fail.Count -eq $p) { $pass++ }

# [3] flat build-* inside the west workspace
$p = $fail.Count
if (Test-Path -LiteralPath $ws) {
    foreach ($f in (Get-ChildItem -LiteralPath $ws -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'build-*' })) {
        $fail += ("zephyr_k 下仍有扁平构建目录: " + $f.Name)
    }
}
if ($fail.Count -eq $p) { $pass++ }

# [4] every CMakeCache must point at an app inside this repo (or an upstream sample)
$p = $fail.Count
$cacheCount = 0
$allowedTop = @('esp32s31', 'esp32s3', 'stm32f1', 'stm32f4', 'samples', '_legacy', '_unsorted')
if (Test-Path -LiteralPath $buildRoot) {
    $sep = [IO.Path]::DirectorySeparatorChar
    $caches = Get-ChildItem -LiteralPath $buildRoot -Recurse -File -Force -ErrorAction SilentlyContinue -Filter 'CMakeCache.txt'
    foreach ($c in $caches) {
        $cacheCount++
        $line = (Select-String -LiteralPath $c.FullName -Pattern '^APPLICATION_SOURCE_DIR:PATH=' -List)
        $src = if ($line) { ($line.Line -split '=', 2)[1].Trim() } else { '' }
        if (-not $src) { $fail += ("CMakeCache 缺 APPLICATION_SOURCE_DIR: " + (Rel $c.DirectoryName)); continue }
        $src = $src -replace '/', '\'
        $inApps = $src.StartsWith($apps + $sep, [StringComparison]::OrdinalIgnoreCase)
        # 上游源码一律放行：samples 之外还有 sysbuild 自己的子缓存
        # （zephyr\share\sysbuild\template、bootloader\mcuboot\boot\zephyr），它们不是"仓库外的源码"
        $inUpstream = $src.StartsWith($ws + $sep, [StringComparison]::OrdinalIgnoreCase)
        if (-not ($inApps -or $inUpstream)) {
            $fail += ("构建目录指向本仓库外的源码: " + (Rel $c.DirectoryName) + "  ->  " + $src)
        }
        $parts = (Rel $c.DirectoryName) -split '\\'
        $isLegacy = ($parts.Length -ge 2 -and $parts[1] -eq '_legacy')
        if (-not $isLegacy -and -not (Test-Path -LiteralPath $src)) {
            $fail += ("构建目录的源码路径已不存在（重构前遗留？）: " + (Rel $c.DirectoryName) + "  ->  " + $src)
        }
        if ($parts.Length -ge 2 -and ($parts[1] -notin $allowedTop)) {
            $warn += ("构建目录层级不合规（应为 build\<芯片>\<工程>\<配置>）: " + (Rel $c.DirectoryName))
        }
    }
}
if ($fail.Count -eq $p) { $pass++ }

# [5] preset binaryDir must climb out to <root>\build
$p = $fail.Count
$presetCount = 0
if (Test-Path -LiteralPath $apps) {
    $sep = [IO.Path]::DirectorySeparatorChar
    $presets = Get-ChildItem -LiteralPath $apps -Recurse -File -Force -Filter 'CMakePresets.json' -ErrorAction SilentlyContinue
    foreach ($pf in $presets) {
        $presetCount++
        try {
            $json = [IO.File]::ReadAllText($pf.FullName, [Text.Encoding]::UTF8) | ConvertFrom-Json
        } catch {
            $fail += ("CMakePresets.json 解析失败: " + (Rel $pf.FullName)); continue
        }
        foreach ($cp in $json.configurePresets) {
            # 子 preset 通常靠 inherits 继承 binaryDir，缺省不算错
            if (-not $cp.binaryDir) { continue }
            $raw = $cp.binaryDir
            if ($raw -match '\$\{sourceDir\}') {
                $raw = $raw -replace '\$\{sourceDir\}', $pf.DirectoryName
            } elseif (-not [IO.Path]::IsPathRooted($raw)) {
                # 相对写法（../../../build/...）是相对工程目录，不是相对当前工作目录
                $raw = Join-Path $pf.DirectoryName $raw
            }
            $raw = $raw -replace '\$\{presetName\}', 'x'
            $full = [IO.Path]::GetFullPath($raw)
            if (-not $full.StartsWith($buildRoot + $sep, [StringComparison]::OrdinalIgnoreCase)) {
                $fail += ((Rel $pf.FullName) + " : preset '" + $cp.name + "' 的 binaryDir 不落在 build\ 下 -> " + $cp.binaryDir)
            }
        }
    }
}
if ($fail.Count -eq $p) { $pass++ }

# [warn] _unsorted residue = entry point was bypassed
$unsorted = Join-Path $buildRoot '_unsorted'
if (Test-Path -LiteralPath $unsorted) {
    $subs = Get-ChildItem -LiteralPath $unsorted -Directory -Force -ErrorAction SilentlyContinue
    $warn += ("build\_unsorted 存在（裸调 west build 的产物），" + $subs.Count + " 项；确认无用可整目录删")
}

# report
"== 目录巡检 =="
"checks passed: $pass/5   caches: $cacheCount   presets: $presetCount"
""
if ($fail.Count -gt 0) {
    "FAIL ($($fail.Count)):"
    $fail | ForEach-Object { "  - $_" }
    ""
}
if ($warn.Count -gt 0) {
    "WARN ($($warn.Count)):"
    $warn | ForEach-Object { "  - $_" }
    ""
}
if ($fail.Count -eq 0) { "OK: 布局合规" } else { "布局不合规，先修 FAIL 项" }

exit ($(if ($fail.Count -gt 0) { 1 } else { 0 }))
