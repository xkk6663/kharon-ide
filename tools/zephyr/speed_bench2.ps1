# 独立后台跑的编译速度对照实验
# 结果逐行追加到 logs\speed-report.txt（用 Stopwatch，避免 Measure-Command 的管道开销）

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
$report = "$root\logs\speed-report.txt"
$lch = @("-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache", "-DCMAKE_ASM_COMPILER_LAUNCHER=ccache")

Set-Location "$root\zephyr_k"
Set-Content -LiteralPath $report -Encoding utf8 -Value ("START " + (Get-Date -Format "HH:mm:ss"))

function Step($tag, $dir, $jobs, $extra, $log) {
    if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dir --cmake-only @extra *> ($log + "-cfg.txt")
    $cfg = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $sw.Restart()
    & $ninja -C $dir -j $jobs *> ($log + "-build.txt")
    $bld = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $ok = ($LASTEXITCODE -eq 0)
    $sw.Stop()
    Add-Content -LiteralPath $report -Encoding utf8 -Value ("$tag configure=${cfg}s build=${bld}s total=$([math]::Round($cfg+$bld,1))s ok=$ok")
}

# A: ninja 默认并行（核数+2 = 18）
Step "A_default_j18" "$root\build\_scratch\speed-A" 18 @() "$root\logs\sp-A"
# B: -j8
Step "B_j8" "$root\build\_scratch\speed-B" 8 @() "$root\logs\sp-B"
# C: ccache 首次（填缓存），-j8
Step "C_ccache_first" "$root\build\_scratch\speed-C" 8 $lch "$root\logs\sp-C"
# D: ccache 二次（全新目录，命中缓存），-j8
Step "D_ccache_second" "$root\build\_scratch\speed-D" 8 $lch "$root\logs\sp-D"

Add-Content -LiteralPath $report -Encoding utf8 -Value "---- ccache stats ----"
& "$root\.tools\ccache\ccache-4.14.3-windows-x86_64\ccache.exe" --show-stats *>> $report
Add-Content -LiteralPath $report -Encoding utf8 -Value ("DONE " + (Get-Date -Format "HH:mm:ss"))
