# 编译速度对照实验：默认并行 vs -j8 vs ccache
# 结果写到 logs\speed-report.txt

$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn"
$sdk  = "C:\Users\xiao1\zephyr-sdk-1.0.1"
$env:PATH = "$root\.venv\Scripts;$root\.tools\cmake\cmake-3.30.2-windows-x86_64\bin;$root\.tools\ninja;$root\.tools\ccache\ccache-4.14.3-windows-x86_64;$sdk\gnu\riscv64-zephyr-elf\bin;$sdk\gnu\xtensa-espressif_esp32s3_zephyr-elf\bin;$env:PATH"
$env:ZEPHYR_BASE = "$root\zephyr_k\zephyr"
$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR = $sdk
$env:CCACHE_DIR = "$root\.ccache"
$env:CCACHE_MAXSIZE = "3G"

$ninja = "$root\.tools\ninja\ninja.exe"
$sample = "$root\zephyr_k\zephyr\samples\hello_world"
$report = "$root\logs\speed-report.txt"
$lines = @()

Set-Location "$root\zephyr_k"

function Clean-Dir($p) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force } }

# ---------- A: 默认并行（ninja 默认 = 核数+2 = 18）----------
$dA = "$root\build\_scratch\speed-A"
Clean-Dir $dA
$tA1 = Measure-Command { & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dA --cmake-only *> "$root\logs\sp-A-cfg.txt" }
$tA2 = Measure-Command { & $ninja -C $dA *> "$root\logs\sp-A-build.txt" }
$okA = ($LASTEXITCODE -eq 0)
$lines += "A 默认并行: configure $([math]::Round($tA1.TotalSeconds,1))s + build $([math]::Round($tA2.TotalSeconds,1))s = $([math]::Round(($tA1+$tA2).TotalSeconds,1))s  成功=$okA"

# ---------- B: -j8 ----------
$dB = "$root\build\_scratch\speed-B"
Clean-Dir $dB
$tB1 = Measure-Command { & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dB --cmake-only *> "$root\logs\sp-B-cfg.txt" }
$tB2 = Measure-Command { & $ninja -C $dB -j 8 *> "$root\logs\sp-B-build.txt" }
$okB = ($LASTEXITCODE -eq 0)
$lines += "B -j8: configure $([math]::Round($tB1.TotalSeconds,1))s + build $([math]::Round($tB2.TotalSeconds,1))s = $([math]::Round(($tB1+$tB2).TotalSeconds,1))s  成功=$okB"

# ---------- C: ccache 第一次（填充缓存）----------
$dC = "$root\build\_scratch\speed-C"
Clean-Dir $dC
$lch = @("-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache", "-DCMAKE_ASM_COMPILER_LAUNCHER=ccache")
$tC1 = Measure-Command { & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dC --cmake-only @lch *> "$root\logs\sp-C-cfg.txt" }
$tC2 = Measure-Command { & $ninja -C $dC -j 8 *> "$root\logs\sp-C-build.txt" }
$okC = ($LASTEXITCODE -eq 0)
$lines += "C ccache 首编: configure $([math]::Round($tC1.TotalSeconds,1))s + build $([math]::Round($tC2.TotalSeconds,1))s = $([math]::Round(($tC1+$tC2).TotalSeconds,1))s  成功=$okC"

# ---------- D: ccache 第二次（全新构建目录，命中缓存）----------
$dD = "$root\build\_scratch\speed-D"
Clean-Dir $dD
$tD1 = Measure-Command { & west build -b esp32s3_devkitc/esp32s3/procpu $sample -d $dD --cmake-only @lch *> "$root\logs\sp-D-cfg.txt" }
$tD2 = Measure-Command { & $ninja -C $dD -j 8 *> "$root\logs\sp-D-build.txt" }
$okD = ($LASTEXITCODE -eq 0)
$lines += "D ccache 二编: configure $([math]::Round($tD1.TotalSeconds,1))s + build $([math]::Round($tD2.TotalSeconds,1))s = $([math]::Round(($tD1+$tD2).TotalSeconds,1))s  成功=$okD"

# ---------- ccache 统计 ----------
$stat = & "$root\.tools\ccache\ccache-4.14.3-windows-x86_64\ccache.exe" --show-stats 2>&1
$lines += "---- ccache stats ----"
$lines += $stat

Set-Content -LiteralPath $report -Encoding utf8 -Value $lines
Write-Output ($lines -join "`n")
