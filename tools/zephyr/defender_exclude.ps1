# Add Windows Defender exclusions for this Zephyr project (requires admin).
# ASCII only so it also runs under Windows PowerShell 5.1.
#
# Why: Defender real-time scanning re-scans every .o/.d/.a written by the build.
# A full Zephyr build writes tens of thousands of small files, so excluding the
# build output dirs, the ccache dir and the SDK is the single biggest speed win.
#
# Usage (normal PowerShell is fine, the script will ask for elevation):
#   powershell -ExecutionPolicy Bypass -File .\tools\defender_exclude.ps1

$ErrorActionPreference = "Continue"
$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"
$sdk  = "C:\Users\xiao1\zephyr-sdk-1.0.1"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This window is NOT elevated."
    Write-Host "Opening a second, elevated PowerShell window and running this script there."
    Write-Host "If no UAC prompt shows up, just open PowerShell yourself with:"
    Write-Host "  right-click Start -> 'Terminal (Admin)' or 'Windows PowerShell (Admin)'"
    Write-Host "then run:"
    Write-Host ("  powershell -ExecutionPolicy Bypass -File " + $PSCommandPath)
    Write-Host ""
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", $PSCommandPath
        ) -ErrorAction Stop
        Write-Host "Elevated run finished (see the other window for the added/skipped list)."
    } catch {
        Write-Host ("Elevation cancelled or failed: " + $_.Exception.Message)
    }
    Write-Host "Note: (Get-MpPreference).ExclusionPath also needs admin, so it cannot be verified from this window."
    return
}

# Defender exclusions do NOT support wildcards, so enumerate the real dirs.
$paths = @()
Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "build*" -or $_.Name -like "tmp-*" } |
    ForEach-Object { $paths += $_.FullName }

$paths += (Join-Path $root ".ccache")
$paths += (Join-Path $root ".tools\ccache")
$paths += (Join-Path $root "zephyr_k")
$paths += $sdk

foreach ($p in $paths) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host ("skip (missing): " + $p)
        continue
    }
    try {
        Add-MpPreference -ExclusionPath $p -ErrorAction Stop
        Write-Host ("added: " + $p)
    } catch {
        Write-Host ("FAILED: " + $p + "  ->  " + $_.Exception.Message)
    }
}

Write-Host ""
Write-Host "=== current exclusions ==="
$cur = (Get-MpPreference).ExclusionPath
if ($cur) { $cur | ForEach-Object { Write-Host ("  " + $_) } } else { Write-Host "  (none)" }
Write-Host ""
Write-Host ("Real-time protection = " + $(if ((Get-MpPreference).DisableRealtimeMonitoring) { "OFF (not recommended)" } else { "ON (exclusions still apply, this is the normal state)" }))
Write-Host ""
Write-Host "Done. To undo one:  Remove-MpPreference -ExclusionPath '<path>'"
