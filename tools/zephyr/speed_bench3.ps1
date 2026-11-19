# Build speed A/B: clean build -j8 with and without ccache (ASCII only)
$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"
$sdk  = "C:\Users\xiao1\zephyr-sdk-1.0.1"
$env:PATH = "$root\.venv\Scripts;$root\.tools\cmake\cmake-3.30.2-windows-x86_64\bin;$root\.tools\ninja;$root\.tools\ccache\ccache-4.14.3-windows-x86_64;$sdk\gnu\riscv64-zephyr-elf\bin;$sdk\gnu\xtensa-espressif_esp32s3_zephyr-elf\bin;$env:PATH"
$env:ZEPHYR_BASE = "$root\zephyr_k\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR = $sdk
$env:CCACHE_DIR = "$root\.ccache"
$env:CCACHE_MAXSIZE = "3G"

$ninja  = "$root\.tools\ninja\ninja.exe"
$sample = "$root\zephyr_k\zephyr\samples\hello_world"
$report = "$root\logs\speed-r.txt"
$lch = @("-DCMAKE_C_COMPILER_LAUNCHER=ccache","-DCMAKE_CXX_COMPILER_LAUNCHER=ccache","-DCMAKE_ASM_COMPILER_LAUNCHER=ccache")

Set-Location "$root\zephyr_k"
Set-Content -LiteralPath $report -Encoding utf8 -Value ("START " + (Get-Date -Format "HH:mm:ss"))

function Step($tag, $dir, $extra, $log) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dir --cmake-only @extra *> ($log + "-cfg.txt")
    $cfg = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $sw.Restart()
    & $ninja -C $dir -j 8 *> ($log + "-build.txt")
    $bld = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $ok = ($LASTEXITCODE -eq 0)
    Add-Content -LiteralPath $report -Encoding utf8 -Value ("$tag cfg=${cfg}s build=${bld}s total=$([math]::Round($cfg+$bld,1))s ok=$ok")
    if (Test-Path -LiteralPath "$dir\zephyr\zephyr.bin") {
        Add-Content -LiteralPath $report -Encoding utf8 -Value ("  bin=" + (Get-Item -LiteralPath "$dir\zephyr\zephyr.bin").Length + " B")
    }
}

Step "R1_j8_nocache"    "$root\build\_scratch\r1" @()  "$root\logs\sr1"
Step "R2_ccache_first"  "$root\build\_scratch\r2" $lch "$root\logs\sr2"
Step "R3_ccache_second" "$root\build\_scratch\r3" $lch "$root\logs\sr3"

Add-Content -LiteralPath $report -Encoding utf8 -Value "---- ccache stats ----"
& "$root\.tools\ccache\ccache-4.14.3-windows-x86_64\ccache.exe" --show-stats *>> $report
Add-Content -LiteralPath $report -Encoding utf8 -Value ("DONE " + (Get-Date -Format "HH:mm:ss"))
