# Full clean-build A/B: cold ccache vs warm ccache (ASCII only)
$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"
$sdk  = "C:\Users\xiao1\zephyr-sdk-1.0.1"
$env:PATH = "$root\.venv\Scripts;$root\.tools\cmake\cmake-3.30.2-windows-x86_64\bin;$root\.tools\ninja;$root\.tools\ccache\ccache-4.14.3-windows-x86_64;$sdk\gnu\riscv64-zephyr-elf\bin;$sdk\gnu\xtensa-espressif_esp32s3_zephyr-elf\bin;$env:PATH"
$env:ZEPHYR_BASE = "$root\zephyr_k\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR = $sdk
$env:CCACHE_DIR = "$root\.ccache"
$env:CCACHE_MAXSIZE = "3G"
$env:CCACHE_COMPRESS = "1"

$ninja  = "$root\.tools\ninja\ninja.exe"
$ccache = "$root\.tools\ccache\ccache-4.14.3-windows-x86_64\ccache.exe"
$sample = "$root\zephyr_k\zephyr\samples\hello_world"
$report = "$root\logs\speed-full.txt"

Set-Location "$root\zephyr_k"
Set-Content -LiteralPath $report -Encoding utf8 -Value ("START " + (Get-Date -Format "HH:mm:ss"))

# clear cache so S1 is a guaranteed all-miss run
& $ccache -C *> "$root\logs\cc-clear.txt"
& $ccache -z *> "$root\logs\cc-zero.txt"

function Step($tag, $dir, $log) {
    $lch = @("-DCMAKE_C_COMPILER_LAUNCHER=ccache","-DCMAKE_CXX_COMPILER_LAUNCHER=ccache","-DCMAKE_ASM_COMPILER_LAUNCHER=ccache")
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dir --cmake-only @lch *> ($log + "-cfg.txt")
    $cfg = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $sw.Restart()
    & $ninja -C $dir -j 8 *> ($log + "-build.txt")
    $bld = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $ok = ($LASTEXITCODE -eq 0)
    Add-Content -LiteralPath $report -Encoding utf8 -Value ("$tag cfg=${cfg}s build=${bld}s total=$([math]::Round($cfg+$bld,1))s ok=$ok")
    $st = & $ccache --show-stats | Select-String -Pattern "Hits:|Misses:|Cache size"
    Add-Content -LiteralPath $report -Encoding utf8 -Value ("  " + (($st | ForEach-Object { $_.Line.Trim() }) -join " | "))
}

Step "S1_cold_cache" "$root\build\_scratch\s1" "$root\logs\ss1"
Step "S2_warm_cache" "$root\build\_scratch\s2" "$root\logs\ss2"

Add-Content -LiteralPath $report -Encoding utf8 -Value ("DONE " + (Get-Date -Format "HH:mm:ss"))
