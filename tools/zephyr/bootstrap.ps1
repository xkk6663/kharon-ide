#!/usr/bin/env pwsh
# 从零搭起这套 Zephyr 环境（clone 完这个仓库之后，一条命令补齐所有上游依赖）。
#
#   pwsh -File .\tools\bootstrap.ps1 -DryRun        # 只看要做什么，不联网、不改文件
#   pwsh -File .\tools\bootstrap.ps1                # 真做（幂等：已就绪的步骤自动跳过）
#   pwsh -File .\tools\bootstrap.ps1 -SdkDir D:\zephyr-sdk-1.0.1
#
# 做四件事：
#   1. 前置检查：pwsh 7 / git / python / Zephyr SDK / cmake / ninja
#   2. .venv + west（pip 安装），如果 zephyr 源码还没拉就按钉住的 commit 拉一份
#   3. west init + west update 只拉这套工程真正用到的 6 个模块（不是 70 个）
#   4. 复原"只存在本地"的两样东西：自建板级 15 个文件（boards_local\）与 zephyr 的 mbedtls 本地补丁（patches\zephyr\）
#
# 不做的事：不编译、不烧录。装完用 .\tools\build.ps1 编。

param(
    [string]$SdkDir      = '',
    [string]$ZephyrUrl   = 'https://github.com/zephyrproject-rtos/zephyr',
    [string]$ZephyrCommit = '34e025569cbb27660b71db809627c3a7402e2111',
    [string]$ZephyrRef   = '',
    [string]$Proxy       = 'http://127.0.0.1:7890',
    [switch]$Direct,
    [switch]$DryRun,
    [switch]$SkipVenv,
    [int]$Retries        = 20,
    [int]$RetryDelaySec  = 6
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot          # 仓库根（本脚本在 tools\ 下）
$ws   = Join-Path $root 'zephyr_k'
$zr   = Join-Path $ws 'zephyr'
$venv = Join-Path $root '.venv'
$mods = @('hal_espressif', 'hal_stm32', 'cmsis_6', 'mbedtls', 'tf-psa-crypto', 'mcuboot')

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Bad  ($m) { Write-Host "  !!  $m" -ForegroundColor Yellow }

function Invoke-WithRetry {
    param([string]$Name, [scriptblock]$Action)
    for ($i = 1; $i -le $Retries; $i++) {
        try {
            & $Action
            return
        } catch {
            if ($i -eq $Retries) { throw "$Name 连续失败 $Retries 次：$($_.Exception.Message)" }
            Write-Host "  $Name 第 $i/$Retries 次失败：$($_.Exception.Message)" -ForegroundColor DarkYellow
            Start-Sleep -Seconds $RetryDelaySec
        }
    }
}

$gitCfg = @('-c', 'http.lowSpeedLimit=10240', '-c', 'http.lowSpeedTime=60', '-c', 'http.postBuffer=524288000')

Step '1/4 前置检查'

$pwshVer = $PSVersionTable.PSVersion
if ($pwshVer.Major -lt 7) { throw "需要 pwsh 7+（当前 $pwshVer）。Windows PowerShell 5.1 不行。" }
Ok "pwsh $pwshVer"

foreach ($cmd in @('git', 'python')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "PATH 里找不到 $cmd。请先装 git 与 python 3.12。" }
    Ok "$cmd -> $((Get-Command $cmd).Source)"
}

# SDK：参数 > 环境变量 > tools\local.env.ps1 里记的 > 常见默认位置
if (-not $SdkDir) {
    if ($env:ZEPHYR_SDK_INSTALL_DIR) { $SdkDir = $env:ZEPHYR_SDK_INSTALL_DIR }
    elseif (Test-Path -LiteralPath (Join-Path $root 'tools\local.env.ps1')) {
        . (Join-Path $root 'tools\local.env.ps1')
        if ($LocalSdkDir) { $SdkDir = $LocalSdkDir }
    }
}
if (-not $SdkDir) {
    foreach ($cand in (Get-ChildItem -Path 'C:\', "$env:USERPROFILE" -Directory -Filter 'zephyr-sdk-*' -ErrorAction SilentlyContinue)) {
        $SdkDir = $cand.FullName; break
    }
}
if (-not $SdkDir -or -not (Test-Path -LiteralPath $SdkDir)) {
    throw "找不到 Zephyr SDK。请装 1.0.1（https://github.com/zephyrproject-rtos/sdk-ng/releases）后用 -SdkDir 指定。"
}
Ok "SDK = $SdkDir"
foreach ($t in @('arm-zephyr-eabi', 'riscv64-zephyr-elf', 'xtensa-espressif_esp32s3_zephyr-elf')) {
    if (-not (Test-Path -LiteralPath (Join-Path $SdkDir "gnu\$t\bin"))) { Bad "SDK 里缺少 $t 工具链（只有你在意的那条线可以忽略）" }
}

foreach ($tool in @('cmake', 'ninja')) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) { Ok "$tool -> $((Get-Command $tool).Source)" }
    elseif (Test-Path -LiteralPath (Join-Path $root ".tools\$tool")) { Ok "$tool -> 项目内 .tools\$tool" }
    else { Bad "$tool 不在 PATH（west build 需要它）：winget install Kitware.CMake Ninja-build.Ninja" }
}

if ($DryRun) {
    Write-Host "`nDryRun 清单：" -ForegroundColor Cyan
    Say "工作区   ：$ws"
    Say "zephyr   ：$zr  @$($ZephyrCommit.Substring(0,12))  （$ZephyrUrl）"
    Say "venv     ：$venv"
    Say "west update：$($mods -join ' ')"
    Say "复原本地 ：boards_local\esp32s31_function_coreboard（15 文件） + patches\zephyr\mbedtls-legacy_support.patch"
    Say "代理     ：$(if ($Direct) { '直连' } else { $Proxy })"
    Write-Host "`nDryRun：没有联网、没有改动任何文件。" -ForegroundColor Cyan
    return
}

Step '2/4 venv + west + zephyr 源码'

if (-not $SkipVenv) {
    if (-not (Test-Path -LiteralPath "$venv\Scripts\python.exe")) {
        Say '创建 .venv'
        & python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'python -m venv 失败' }
    }
    $vpy = Join-Path $venv 'Scripts\python.exe'
    Say 'pip 安装 west'
    & $vpy -m pip install --upgrade pip *> $null
    & $vpy -m pip install west
    if ($LASTEXITCODE -ne 0) { throw 'pip install west 失败' }
    Ok "west 就绪（$vpy -m west）"
} else {
    $vpy = (Get-Command python).Source
}

if (-not $Direct) {
    $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy
    Say "git 走代理 $Proxy（直连加 -Direct）"
}

# zephyr 源码：没有就拉；有就只核对 commit
if (-not (Test-Path -LiteralPath "$zr\.git")) {
    Say "克隆 zephyr（无 blob + 浅）到 $zr"
    New-Item -ItemType Directory -Force -Path $ws | Out-Null
    Invoke-WithRetry 'blobless clone' {
        if (Test-Path -LiteralPath $zr) { Remove-Item -LiteralPath $zr -Recurse -Force }
        if ($ZephyrRef) {
            & git.exe @gitCfg clone --depth 1 --filter=blob:none --branch $ZephyrRef $ZephyrUrl $zr
            if ($LASTEXITCODE -ne 0) { throw "git clone --branch $ZephyrRef 退出码 $LASTEXITCODE" }
        } else {
            & git.exe @gitCfg clone --depth 1 --filter=blob:none --no-checkout $ZephyrUrl $zr
            if ($LASTEXITCODE -ne 0) { throw "git clone 退出码 $LASTEXITCODE" }
        }
    }
    if (-not $ZephyrRef) {
        Invoke-WithRetry 'fetch pin' {
            & git.exe -C $zr @gitCfg fetch --depth 1 --filter=blob:none origin $ZephyrCommit
            if ($LASTEXITCODE -ne 0) { throw "git fetch 退出码 $LASTEXITCODE" }
        }
        Invoke-WithRetry 'checkout pin' {
            & git.exe -C $zr checkout -q $ZephyrCommit
            if ($LASTEXITCODE -ne 0) { throw "git checkout 退出码 $LASTEXITCODE" }
        }
    } else {
        $ZephyrCommit = ((& git.exe -C $zr rev-parse HEAD) -join '').Trim()
    }
    Ok "zephyr @$($ZephyrCommit.Substring(0,12))"
} else {
    $head = (& git.exe -C $zr rev-parse HEAD) -join ''
    if (-not $head.StartsWith($ZephyrCommit.Substring(0, 12))) {
        Bad "zephyr 现有 HEAD=$($head.Substring(0,12)) 不是钉住的 $($ZephyrCommit.Substring(0,12))；如需对齐请手动切换"
    } else { Ok "zephyr HEAD 已对齐" }
}

# west init（幂等）
if (-not (Test-Path -LiteralPath "$ws\.west\config")) {
    Say 'west init -l zephyr_k\zephyr'
    & $vpy -m west init -l $zr
    if ($LASTEXITCODE -ne 0) { throw 'west init 失败' }
    Ok "工作区 $ws 建立"
} else { Ok 'west 工作区已存在' }

Step '3/4 west update（只拉用到的 6 个模块）'

Push-Location $ws
try { & $vpy -m west update @mods; $u = $LASTEXITCODE } finally { Pop-Location }
if ($u -ne 0) { throw "west update 失败（退出码 $u）。网络差时重跑本脚本即可，已下好的模块会跳过。" }
Ok 'west update 完成'

Step '4/4 复原"只在本地"的东西'

$boardSrc = Join-Path $root 'boards_local\esp32s31_function_coreboard'
$boardDst = Join-Path $zr 'boards\espressif\esp32s31_function_coreboard'
if (Test-Path -LiteralPath $boardSrc) {
    New-Item -ItemType Directory -Force -Path (Split-Path $boardDst) | Out-Null
    Copy-Item -LiteralPath $boardSrc -Destination $boardDst -Recurse -Force
    Ok "板级：复制 $(Get-ChildItem -LiteralPath $boardSrc -Recurse -File | Measure-Object).Count 个文件 -> boards\espressif\"
} else { Bad 'boards_local 不存在，S31 板会编不出来' }

$patch = Join-Path $root 'patches\zephyr\mbedtls-legacy_support.patch'
if (Test-Path -LiteralPath $patch) {
    & git.exe -C $zr apply --check $patch 2>$null
    if ($LASTEXITCODE -eq 0) {
        & git.exe -C $zr apply $patch
        if ($LASTEXITCODE -ne 0) { throw 'git apply 失败' }
        Ok 'mbedtls 补丁：已打上'
    } else {
        & git.exe -C $zr apply --reverse --check $patch 2>$null
        if ($LASTEXITCODE -eq 0) { Ok 'mbedtls 补丁：已在（跳过）' }
        else { Bad 'mbedtls 补丁状态不明（源码可能变过），请人工看 patches\zephyr\mbedtls-legacy_support.patch' }
    }
} else { Bad 'patches\zephyr\mbedtls-legacy_support.patch 不存在' }

# 记住这台机器的 SDK 路径，供 env.ps1 下次直接用
$localEnv = Join-Path $root 'tools\local.env.ps1'
$localTxt = "# 本机路径（不入库）：bootstrap 写入，env.ps1 读取`n`$LocalSdkDir = '$SdkDir'`n"
[IO.File]::WriteAllText($localEnv, $localTxt, (New-Object Text.UTF8Encoding($false)))
Ok "记下 SDK 路径 -> tools\local.env.ps1（已在 .gitignore）"

Write-Host "`n== 自检 ==" -ForegroundColor Cyan
$py = Join-Path $venv 'Scripts\python.exe'
Push-Location $ws
try { $list = & $py -m west list 2>&1 } finally { Pop-Location }
$hit = @($list | Where-Object { $_ -match 'hal_stm32|cmsis_6|mbedtls|tf-psa-crypto' })
Say "west list：$(if ($hit) { "$($hit.Count) 个目标模块在列" } else { '未列出（检查 west update）' })"
$checks = @(
    "$zr\west.yml",
    "$zr\boards\espressif\esp32s31_function_coreboard\board.yml",
    "$ws\modules\hal\stm32\stm32cube\stm32f4xx\drivers\src\stm32f4xx_hal.c",
    "$ws\modules\hal\stm32\stm32cube\stm32f1xx\drivers\src\stm32f1xx_hal.c",
    "$ws\modules\hal\cmsis_6\include\core_cm4.h",
    "$ws\modules\hal\cmsis_6\include\core_cm3.h",
    "$ws\modules\crypto\mbedtls\include\mbedtls\ssl.h",
    "$ws\modules\crypto\tf-psa-crypto\include\psa\crypto.h",
    "$ws\modules\hal\espressif\zephyr\module.yml"
)
$missing = 0
foreach ($p in $checks) { if (-not (Test-Path -LiteralPath $p)) { Bad "缺 $($p.Replace($root + '\', ''))"; $missing++ } }
if ($missing -eq 0) { Ok "关键路径 $($checks.Count)/$($checks.Count) 齐" }

Write-Host "`n下一步：" -ForegroundColor Cyan
Say '. .\env.ps1                                   # 或者直接从仓库根跑 build.ps1（它会自己 source）'
Say '.\tools\build.ps1 -App .\applications\stm32f1\f103_breath -Board stm32f103_mini -Pristine'
Say '.\tools\check-layout.ps1                      # 期望 5/5'
