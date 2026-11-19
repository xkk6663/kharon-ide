# 把 STM32 / crypto 四个模块拉成"标准上游 git 仓"
#
# 背景：这棵树里 modules\hal\stm32、modules\hal\cmsis_6、modules\crypto\mbedtls、
#       modules\crypto\tf-psa-crypto 曾是手工离线复原的（不是 git 仓），`west update`
#       会因"目录非空"失败。本脚本按 west.yml 里钉住的 revision 取回真仓，
#       拉成功后才替换原目录，失败则原样保留（不会把树弄坏）。
#
# 它做五件事：
#   1) 从 zephyr\west.yml 读出四个项目的目标 revision（不写死，跟着清单走）
#   2) 给 git 带上代理（默认 127.0.0.1:7890，-Direct 可关），失败自动重试
#   3) 旧目录整体挪到 zephyr_k\_offline_bak_<时间戳>\（不删，编译验证过再手动清）
#   4) 大仓（hal_stm32 / tf-psa-crypto）默认走"无 blob 克隆 + 稀疏检出"：先只取
#      commit/tree，再按需要的路径分批取 blob。原因是这支代理对 20 MB 以上的单个
#      pack 会截断（early EOF），分批每批只有几 MB，能过。-Full 可以强制一次拉全。
#   5) 校验 HEAD == revision + 关键路径存在，并打印后续编译/烧录命令
#
# 用法：
#   pwsh -File .\tools\pull-stm32-modules.ps1 -DryRun          # 只打印清单，不联网
#   pwsh -File .\tools\pull-stm32-modules.ps1                  # 走默认代理（推荐）
#   pwsh -File .\tools\pull-stm32-modules.ps1 -Full            # 整个仓一次拉全（慢/易断）
#   pwsh -File .\tools\pull-stm32-modules.ps1 -Direct          # 不用代理（直连）
#   pwsh -File .\tools\pull-stm32-modules.ps1 -Proxy http://127.0.0.1:7897
#
# 注意：只拉这四个模块，不动 zephyr 主干、不动 espressif 那些模块。
#      稀疏检出留下的是"缺其它路径 blob"的仓（git 会在真用到时按需补），
#      想补齐全部内容：在模块目录里 `git sparse-checkout disable`（联网状态下）。

param(
    [string]$Proxy = 'http://127.0.0.1:7890',
    [switch]$Direct,
    [switch]$DryRun,
    [switch]$Full,
    [int]$Retries = 20,
    [int]$RetryDelaySec = 6
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$ws = Join-Path $root 'zephyr_k'
$westYml = Join-Path $ws 'zephyr\west.yml'

if (-not (Test-Path -LiteralPath $westYml)) {
    throw "找不到 west.yml：$westYml（env.ps1 里 ZEPHYR_BASE 坏了吗？）"
}

# ---------------------------------------------------------------- 读 revision
function Get-Pin([string]$project) {
    $lines = Get-Content -LiteralPath $westYml -Encoding UTF8
    $hit = $false
    foreach ($l in $lines) {
        if ($l -match ('^\s*-\s*name:\s*' + [regex]::Escape($project) + '\s*$')) { $hit = $true; continue }
        if ($hit -and $l -match '^\s*revision:\s*([0-9a-f]{7,40})') { return $Matches[1] }
        if ($hit -and $l -match '^\s*-\s*name:') { break }
    }
    throw "west.yml 里没找到 $project 的 revision"
}

# 稀疏检出需要的路径（只够编 STM32 F1/F4 + 这两块板涉及的 dts/引脚库）
$halPaths = @(
    '/CMakeLists.txt', '/zephyr/', '/LICENSES/',
    '/stm32cube/CMakeLists.txt', '/stm32cube/series_list.cmake', '/stm32cube/common_ll/',
    '/stm32cube/stm32f1xx/', '/stm32cube/stm32f4xx/',
    '/dts/'
)

$plan = @(
    [pscustomobject]@{
        name     = 'hal_stm32'
        path     = Join-Path $ws 'modules\hal\stm32'
        url      = 'https://github.com/zephyrproject-rtos/hal_stm32'
        what     = 'STM32Cube HAL/LL 全系列源码 + dts\st\* 引脚库 + common_ll 统一入口'
        sparse   = $halPaths
        big      = $true
        revision = ''
        isGit    = $false
        head     = ''
        state    = ''
    },
    [pscustomobject]@{
        name     = 'cmsis_6'
        path     = Join-Path $ws 'modules\hal\cmsis_6'
        url      = 'https://github.com/zephyrproject-rtos/CMSIS_6'
        what     = 'CMSIS Core 内核头（F1 要 core_cm3.h、F4 要 core_cm4.h）'
        sparse   = @()          # 包只有 18 MB，整仓一次拉更省事
        big      = $false
        revision = ''
        isGit    = $false
        head     = ''
        state    = ''
    },
    [pscustomobject]@{
        name     = 'mbedtls'
        path     = Join-Path $ws 'modules\crypto\mbedtls'
        url      = 'https://github.com/zephyrproject-rtos/mbedtls'
        what     = 'TLS/PSA 上层（S3 的 HTTPS / WPA 原语要用）'
        sparse   = @()          # 工作区 8 MB，整仓够小
        big      = $false
        revision = ''
        isGit    = $false
        head     = ''
        state    = ''
    },
    [pscustomobject]@{
        name     = 'tf-psa-crypto'
        path     = Join-Path $ws 'modules\crypto\tf-psa-crypto'
        url      = 'https://github.com/zephyrproject-rtos/tf-psa-crypto'
        what     = 'PSA Crypto 加密原语（4.x 的 wpa_supplicant / 硬件加密要用）'
        sparse   = @()          # 24 MB，整仓够小；实测走稀疏会在 checkout 阶段按需抓 blob，代理一抖就 128
        big      = $false
        revision = ''
        isGit    = $false
        head     = ''
        state    = ''
    }
)

foreach ($p in $plan) {
    $p.revision = Get-Pin $p.name
    $p.isGit = (Test-Path -LiteralPath (Join-Path $p.path '.git'))
    $p.head = if ($p.isGit) { (& git.exe -C $p.path rev-parse HEAD 2>$null) -join '' } else { '' }
    $p.state = if (-not (Test-Path -LiteralPath $p.path)) { '缺失' }
               elseif (-not $p.isGit) { '非 git（手工复原）' }
               elseif ($p.head.StartsWith($p.revision)) { '已对齐' }
               else { "git 但指向 $($p.head.Substring(0, [Math]::Min(8, $p.head.Length)))" }
}

# ---------------------------------------------------------------- 打印清单
Write-Host ''
Write-Host '待拉齐清单（来源：zephyr_k\zephyr\west.yml）' -ForegroundColor Cyan
Write-Host ('  {0,-11} {1,-24} {2,-12} {3}' -f '项目', '落点', 'revision', '现状')
foreach ($p in $plan) {
    Write-Host ('  {0,-13} {1,-26} {2,-12} {3}' -f $p.name, $p.path.Replace($ws + '\', ''), $p.revision.Substring(0, 10), $p.state)
}
Write-Host ''
foreach ($p in $plan) {
    $way = if ($Full -or -not $p.big) { '整仓浅克隆' } else { '无 blob 克隆 + 稀疏检出' }
    Write-Host ("  {0}  ->  {1}" -f $p.name, $p.url)
    Write-Host ("      包含：{0}" -f $p.what)
    Write-Host ("      方式：{0}" -f $way)
}
Write-Host ''
Write-Host ("  代理：       {0}" -f $(if ($Direct) { '(不用)' } else { $Proxy }))
Write-Host ("  备份去向：   zephyr_k\_offline_bak_<yyyyMMdd-HHmmss>\")
Write-Host ("  重试：       最多 {0} 次，间隔 {1}s" -f $Retries, $RetryDelaySec)
Write-Host ''

if ($DryRun) {
    Write-Host 'DryRun：只打印清单，没有联网、没有改任何文件。' -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- git 环境
if (-not $Direct) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    Write-Host "git 走代理：$Proxy" -ForegroundColor DarkGray
} else {
    Remove-Item Env:\HTTP_PROXY  -ErrorAction SilentlyContinue
    Remove-Item Env:\HTTPS_PROXY -ErrorAction SilentlyContinue
    Write-Host 'git 直连（不走代理）' -ForegroundColor DarkGray
}

# lowSpeedTime=60：卡住 60 秒就自己断，交给外层重试；不要设成 999999（会挂死无法自愈）
$gitCommon = @('-c', 'http.postBuffer=524288000', '-c', 'http.lowSpeedLimit=10240', '-c', 'http.lowSpeedTime=60')

function Clear-TempRepo([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return }
    try { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction Stop }
    catch {
        # 上一次失败的 git 进程可能还攥着 pack/shallow.lock
        Get-Process -Name git -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Retry([string]$label, [int]$tries, [scriptblock]$body) {
    for ($i = 1; $i -le $tries; $i++) {
        try { & $body; return }
        catch {
            if ($i -ge $tries) { throw }
            Write-Host ("  {0} 第 {1}/{2} 次失败：{3}" -f $label, $i, $tries, $_.Exception.Message) -ForegroundColor DarkYellow
            Start-Sleep -Seconds $RetryDelaySec
        }
    }
}

# ---------------------------------------------------------------- 备份 + 拉取
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bakDir = Join-Path $ws "_offline_bak_$stamp"
$tmpRoot = Join-Path $env:TEMP "pull-stm32-$stamp"

foreach ($p in $plan) {
    Write-Host ''
    Write-Host ("== {0} ==" -f $p.name) -ForegroundColor Cyan

    if ($p.state -eq '已对齐') {
        Write-Host "  已是目标 revision（$($p.revision.Substring(0,10))），跳过。" -ForegroundColor Green
        continue
    }

    # 旧目录先挪走（不删）
    if (Test-Path -LiteralPath $p.path) {
        if (-not (Test-Path -LiteralPath $bakDir)) { New-Item -ItemType Directory -Path $bakDir | Out-Null }
        Move-Item -LiteralPath $p.path -Destination (Join-Path $bakDir $p.name)
        Write-Host "  旧目录已备份 -> $(Join-Path $bakDir $p.name)"
    }

    $tmp = Join-Path $tmpRoot $p.name
    if (-not (Test-Path -LiteralPath $tmpRoot)) { New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null }
    Clear-TempRepo $tmp

    $useSparse = (-not $Full) -and $p.big

    try {
        if (-not $useSparse) {
            Invoke-Retry 'clone' $Retries {
                Clear-TempRepo $tmp
                $sw = [Diagnostics.Stopwatch]::StartNew()
                & git.exe @gitCommon clone --depth 1 --progress $p.url $tmp 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "git clone 退出码 $LASTEXITCODE" }
            }
            Write-Host "  整仓浅克隆完成" -ForegroundColor Green

            # 默认分支 HEAD 未必是钉住的 revision（crypto 两个仓就是这样）→ 按 SHA 补取一次
            $h = (& git.exe -C $tmp rev-parse HEAD) -join ''
            if (-not $h.StartsWith($p.revision)) {
                Write-Host ("  HEAD=$($h.Substring(0,8)) 不是 pin，补 fetch --depth 1 + checkout")
                Invoke-Retry 'fetch pin' $Retries {
                    & git.exe -C $tmp @gitCommon fetch --depth 1 origin $p.revision 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw "fetch $($p.revision) 退出码 $LASTEXITCODE" }
                    & git.exe -C $tmp checkout -q $p.revision 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw "checkout $($p.revision) 退出码 $LASTEXITCODE" }
                }
            }
        } else {
            # 1) 只要 commit + tree（无 blob），几百 KB；这个量级的请求不会触发截断
            Invoke-Retry 'blobless clone' $Retries {
                Clear-TempRepo $tmp
                & git.exe @gitCommon clone --depth 1 --filter=blob:none --no-checkout $p.url $tmp 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "git clone(blobless) 退出码 $LASTEXITCODE" }
            }
            Write-Host "  无 blob 克隆完成（只有目录树）" -ForegroundColor Green

            # 2) 把钉住的那个 commit 单独取回来（同样不带 blob）
            Invoke-Retry 'fetch pin' $Retries {
                & git.exe -C $tmp @gitCommon fetch --depth 1 --filter=blob:none origin $p.revision 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "fetch $($p.revision) 退出码 $LASTEXITCODE" }
            }

            # 3) 先声明要哪些路径，再检出（这样 checkout 只拉这些路径的 blob）
            & git.exe -C $tmp sparse-checkout init --no-cone 2>&1 | Out-Null
            & git.exe -C $tmp checkout -q $p.revision 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "checkout $($p.revision) 退出码 $LASTEXITCODE" }

            # 4) 分批补 blob：每批几 MB，单批失败就整批重试（不是续传）
            #    有预置路径清单（hal_stm32）就按它来；没有（tf-psa-crypto）就按顶层目录一批一个
            $groups = @()
            if ($p.sparse.Count -gt 0) {
                $groups = @(
                    @('/CMakeLists.txt', '/zephyr/', '/LICENSES/'),
                    @('/stm32cube/CMakeLists.txt', '/stm32cube/series_list.cmake', '/stm32cube/common_ll/'),
                    @('/stm32cube/stm32f1xx/'),
                    @('/stm32cube/stm32f4xx/'),
                    @('/dts/')
                )
            } else {
                $dirs = @(& git.exe -C $tmp ls-tree -d --name-only HEAD)
                $all = @(& git.exe -C $tmp ls-tree --name-only HEAD)
                $rootFiles = @($all | Where-Object { $dirs -notcontains $_ })
                if ($rootFiles.Count -gt 0) { $groups += , @($rootFiles | ForEach-Object { "/$_" }) }
                foreach ($d in $dirs) {
                    if ($d -in @('.github', '.gitlab')) { continue }
                    $groups += , @("/$d/")
                }
            }
            foreach ($g in $groups) {
                $label = ($g -join ' ')
                Invoke-Retry "sparse $label" $Retries {
                    & git.exe -C $tmp @gitCommon sparse-checkout add $g 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw "sparse-checkout add 退出码 $LASTEXITCODE" }
                }
                Write-Host ("  已补 {0}" -f $label) -ForegroundColor Green
            }
        }

        # 5) HEAD 必须等于钉住的 revision
        $head = (& git.exe -C $tmp rev-parse HEAD) -join ''
        if (-not $head.StartsWith($p.revision)) { throw "HEAD=$head 与 revision=$($p.revision) 不一致" }
        & git.exe -C $tmp status --short | Out-Null
        Move-Item -LiteralPath $tmp -Destination $p.path
        Write-Host ("  落地：" + $p.path)
        Write-Host ("  HEAD = " + $head) -ForegroundColor Green
    } catch {
        # 失败：把备份挪回去，保持树可用
        Clear-TempRepo $tmp
        if (Test-Path -LiteralPath (Join-Path $bakDir $p.name)) {
            Move-Item -LiteralPath (Join-Path $bakDir $p.name) -Destination $p.path
            Write-Host '  拉取失败，已把备份挪回原位（树仍可编译）。' -ForegroundColor Yellow
        }
        throw "拉取 $($p.name) 失败：$($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 收尾校验
Write-Host ''
Write-Host '== 校验 ==' -ForegroundColor Cyan
$checks = @(
    @('hal_stm32 全系列 HAL',  'zephyr_k\modules\hal\stm32\stm32cube\stm32f1xx\drivers\include\stm32f1xx_hal_conf.h'),
    @('F1 启动文件',           'zephyr_k\modules\hal\stm32\stm32cube\stm32f1xx\soc\startup_stm32f103xe.s'),
    @('F1 引脚库',             'zephyr_k\modules\hal\stm32\dts\st\f1\stm32f103r(c-d-e)tx-pinctrl.dtsi'),
    @('F1 LL 统一入口',        'zephyr_k\modules\hal\stm32\stm32cube\common_ll\include\stm32_ll_gpio.h'),
    @('core_cm3.h（F1）',      'zephyr_k\modules\hal\cmsis_6\CMSIS\Core\Include\core_cm3.h'),
    @('core_cm4.h（F4）',      'zephyr_k\modules\hal\cmsis_6\CMSIS\Core\Include\core_cm4.h'),
    @('mbedtls 顶层',          'zephyr_k\modules\crypto\mbedtls\include\mbedtls\ssl.h'),
    @('tf-psa-crypto 顶层',    'zephyr_k\modules\crypto\tf-psa-crypto\core\psa_crypto_core.h')
)
foreach ($c in $checks) {
    $abs = Join-Path $root $c[1]
    $mark = if (Test-Path -LiteralPath $abs) { 'OK  ' } else { 'MISS' }
    $col = if ($mark -eq 'OK  ') { 'Green' } else { 'Red' }
    Write-Host ("  {0} {1}" -f $mark, $c[0]) -ForegroundColor $col
}

if (Test-Path -LiteralPath $bakDir) { Write-Host ("`n旧目录备份留在：" + $bakDir) -ForegroundColor DarkGray }

Write-Host ''
Write-Host '下一步（STM32 两条 + 换了 crypto 之后 ESP32 必须复编）：' -ForegroundColor Cyan
Write-Host '  .\tools\build.ps1 -App .\applications\stm32f1\f103_breath -Board stm32f103_mini -Pristine'
Write-Host '  .\tools\build.ps1 -App .\applications\stm32f4\f411_breath -Board blackpill_f411ce -Pristine'
Write-Host '  # 都通过后再烧：把上面命令换成 -Flash（同一支 DAP-Link，探针 cfg 按系列自动分流）'
Write-Host '  # crypto 换了来源，ESP32 侧也要复编（zephyr 里有 modules/mbedtls/legacy_support.cmake 本地补丁）'
Write-Host '  # 顺便确认 west 也认这些目录：west list | Select-String "hal_stm32|cmsis_6|mbedtls|tf-psa"'
