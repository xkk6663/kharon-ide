# Reclaim space from Zephyr build directories WITHOUT sending anything to the Recycle Bin.
# ASCII only so it also runs under Windows PowerShell 5.1.
#
# Why this exists: Explorer / VS Code "Delete" goes through the shell, so a single
# 40+ GB build tree becomes millions of Recycle Bin entries and the bin stops opening.
# On this machine deletes under a Desktop folder are intercepted even when issued
# through the filesystem API or cmd rd -- they still land in the Recycle Bin and
# hold the disk space. So a plain Directory::Delete is NOT enough: each target is
# first renamed into %TEMP% (same volume, a pure directory rename) and then deleted
# there, where deletes are not intercepted. Verified: bin entry count stays flat.
#
# Safety: only paths strictly under <root>\build are ever deleted; -Protect keeps the
# build dir you are currently using. Default is a DRY RUN.
#
# Usage:
#   pwsh -File .\tools\zephyr\clean-builds.ps1                          # dry run: list candidates
#   pwsh -File .\tools\zephyr\clean-builds.ps1 -Delete                  # delete dirs older than 7 days
#   pwsh -File .\tools\zephyr\clean-builds.ps1 -Delete -KeepDays 3
#   pwsh -File .\tools\zephyr\clean-builds.ps1 -Delete -All             # every build dir except -Protect
#   pwsh -File .\tools\zephyr\clean-builds.ps1 -Delete -Protect .\build\esp32s3\s3_wifi_portal\default
#
# build.ps1 calls this after a successful build with -KeepDays 7 -TriggerFreeGB 25,
# so it only does work when the disk is actually getting tight. Pass -NoPrune to skip.

param(
    [string]$Root = "",
    [int]$KeepDays = 7,
    [double]$TriggerFreeGB = 0,
    [switch]$All,
    [switch]$Delete,
    [switch]$Quiet,
    [string[]]$Protect = @()
)

$ErrorActionPreference = "Stop"

# tools\zephyr -> tools -> repo root
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $Root "build"))
if (-not [System.IO.Directory]::Exists($buildRoot)) {
    if (-not $Quiet) { Write-Host ("clean-builds: no build dir (" + $buildRoot + ")") }
    return
}

# Cheap gate used by build.ps1: do nothing while the volume still has room.
if ($TriggerFreeGB -gt 0) {
    $freeGB = -1.0
    try {
        $vol = Get-Volume -DriveLetter $Root.Substring(0, 1)
        $freeGB = [math]::Round($vol.SizeRemaining / 1GB, 2)
    } catch {
        $freeGB = -1.0
    }
    if ($freeGB -ge 0 -and $freeGB -ge $TriggerFreeGB) { return }
}

function Get-BuildTopDirs {
    param([string]$Path, [int]$Depth)
    $out = @()
    if ([System.IO.File]::Exists((Join-Path $Path "CMakeCache.txt"))) {
        return @($Path)
    }
    if ($Depth -ge 4) { return @() }
    foreach ($d in [System.IO.Directory]::GetDirectories($Path)) {
        $out += Get-BuildTopDirs -Path $d -Depth ($Depth + 1)
    }
    return $out
}

function Get-LastBuildTime {
    param([string]$Path)
    foreach ($n in @("build.ninja", ".ninja_log", "CMakeCache.txt")) {
        $f = Join-Path $Path $n
        if ([System.IO.File]::Exists($f)) { return [System.IO.File]::GetLastWriteTime($f) }
    }
    return [System.IO.Directory]::GetLastWriteTime($Path)
}

function Test-UnderBuildRoot {
    param([string]$Path)
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $full = [System.IO.Path]::GetFullPath($Path)
    return $full.StartsWith($buildRoot + $sep, [System.StringComparison]::OrdinalIgnoreCase)
}

# ---- collect candidates ---------------------------------------------------
$protectFull = @()
foreach ($p in $Protect) {
    if ([string]::IsNullOrWhiteSpace($p)) { continue }
    $protectFull += [System.IO.Path]::GetFullPath($p)
}

$targets = @()
foreach ($dir in (Get-BuildTopDirs -Path $buildRoot -Depth 0)) {
    if (-not (Test-UnderBuildRoot $dir)) { continue }
    $skip = $false
    foreach ($p in $protectFull) {
        if ($dir.Equals($p, [System.StringComparison]::OrdinalIgnoreCase)) { $skip = $true }
    }
    if ($skip) { continue }
    $targets += [pscustomobject]@{
        Path      = $dir
        Kind      = "build"
        LastBuild = (Get-LastBuildTime $dir)
    }
}

# Scratch / unsorted layers are system-owned and safe to drop whole.
foreach ($name in @("_scratch", "_unsorted")) {
    $p = Join-Path $buildRoot $name
    if (-not [System.IO.Directory]::Exists($p)) { continue }
    $skip = $false
    foreach ($q in $protectFull) {
        if ([System.IO.Path]::GetFullPath($p).Equals($q, [System.StringComparison]::OrdinalIgnoreCase)) { $skip = $true }
        elseif ($q.StartsWith([System.IO.Path]::GetFullPath($p) + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) { $skip = $true }
    }
    if ($skip) { continue }
    $targets += [pscustomobject]@{
        Path      = $p
        Kind      = $name.TrimStart("_")
        LastBuild = (Get-LastBuildTime $p)
    }
}

# ---- decide ---------------------------------------------------------------
$cutoff = (Get-Date).AddDays(-$KeepDays)
$toDelete = @()
foreach ($t in $targets) {
    if ($All) { $toDelete += $t; continue }
    if ($t.LastBuild -lt $cutoff) { $toDelete += $t }
}

if (-not $Quiet) {
    Write-Host ""
    Write-Host ("build root : " + $buildRoot)
    Write-Host ("keep days  : " + $KeepDays + "   cutoff: " + $cutoff.ToString("yyyy-MM-dd HH:mm"))
    if ($protectFull.Count -gt 0) {
        foreach ($p in $protectFull) { Write-Host ("protected  : " + $p.Substring($buildRoot.Length).TrimStart("\")) }
    }
    Write-Host ""
    if ($targets.Count -eq 0) {
        Write-Host "no build dirs found"
    } else {
        $targets |
            Sort-Object LastBuild |
            ForEach-Object {
                $mark = if ($toDelete -contains $_) { "DELETE" } else { "keep  " }
                $rel = $_.Path.Substring($buildRoot.Length).TrimStart("\")
                "{0}  {1,-8}  {2}  {3}" -f $mark, $_.LastBuild.ToString("yyyy-MM-dd HH:mm"), $_.Kind, $rel
            }
    }
    Write-Host ""
}

if ($toDelete.Count -eq 0) {
    if (-not $Quiet) { Write-Host "nothing to delete" }
    return
}

if (-not $Delete) {
    if (-not $Quiet) {
        Write-Host ("dry run: " + $toDelete.Count + " dir(s) would be deleted (direct delete, no Recycle Bin)")
        Write-Host "re-run with -Delete to actually remove them"
    }
    return
}

# ---- delete: stage into %TEMP%, then remove -----------------------------
# Rename first (same volume => pure metadata op, not a delete), then delete inside
# %TEMP% where the interception does not apply. Falls back to a direct delete if
# the rename cannot be done (e.g. TEMP on another volume) and says so loudly.
$freeBefore = -1.0
try {
    $v0 = Get-Volume -DriveLetter $Root.Substring(0, 1)
    $freeBefore = [math]::Round($v0.SizeRemaining / 1GB, 2)
} catch { $freeBefore = -1.0 }

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("elab-prune-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 6))
$null = [System.IO.Directory]::CreateDirectory($stagingRoot)

$ok = 0
$stagedOut = 0
$directDeleted = 0
$failed = @()
foreach ($t in $toDelete) {
    if (-not (Test-UnderBuildRoot $t.Path)) {
        $failed += ($t.Path + " -> refused: outside build root")
        continue
    }
    $rel = $t.Path.Substring($buildRoot.Length).TrimStart("\")
    $staged = Join-Path $stagingRoot ([Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.Directory]::Move($t.Path, $staged)
        [System.IO.Directory]::Delete($staged, $true)
        $stagedOut++
        $ok++
        if (-not $Quiet) { Write-Host ("deleted: " + $rel) }
    } catch {
        $msg = $_.Exception.Message
        # fallback: direct delete. This one may land in the Recycle Bin.
        try {
            [System.IO.Directory]::Delete($t.Path, $true)
            $directDeleted++
            $ok++
            if (-not $Quiet) { Write-Host ("deleted (direct, may hit Recycle Bin): " + $rel) }
        } catch {
            $failed += ($rel + " -> " + $msg)
        }
    }
}
try { [System.IO.Directory]::Delete($stagingRoot, $false) } catch {}

$freeAfter = -1.0
try {
    $v1 = Get-Volume -DriveLetter $Root.Substring(0, 1)
    $freeAfter = [math]::Round($v1.SizeRemaining / 1GB, 2)
} catch { $freeAfter = -1.0 }

Write-Host ""
Write-Host ("clean-builds: deleted " + $ok + " dir(s) (" + $stagedOut + " staged, " + $directDeleted + " direct), failed " + $failed.Count)
if ($directDeleted -gt 0) {
    Write-Host ("  NOTE: " + $directDeleted + " dir(s) went through a direct delete; if free space did not grow, empty the Recycle Bin")
}
if ($freeBefore -ge 0 -and $freeAfter -ge 0) {
    Write-Host ("free space: " + $freeBefore + " GB -> " + $freeAfter + " GB")
}
foreach ($f in $failed) { Write-Host ("  FAILED " + $f) }
