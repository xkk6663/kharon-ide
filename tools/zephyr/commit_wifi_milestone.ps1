# 一次性本地提交脚本：Zephyr S3 Wi-Fi 配网门户（一档 + 二档）
# 用法：  pwsh -File tools\commit_wifi_milestone.ps1
# 说明：  按"固件能力 / 工具 / 文档"分成 3 个提交，只加明确列出的文件，
#         不碰 build-*、.tools、.venv、zephyr_k（它们本来就在 .gitignore 里）。
#         日志（logs\*.txt）刻意不提交：机器生成、体积大、可再抓。

$ErrorActionPreference = 'Stop'
$root = 'C:\Users\xiao1\Desktop\esp32\zephyr-learn'
Set-Location $root

Write-Host '==== 提交前的改动一览 ====' -ForegroundColor Cyan
git status --porcelain
Write-Host ''

function Invoke-Group {
    param(
        [string] $Title,
        [string] $Message,
        [string[]] $Files
    )

    $existing = @($Files | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) })
    if ($existing.Count -eq 0) {
        Write-Host "[跳过] $Title —— 列出的文件都不存在" -ForegroundColor Yellow
        return
    }

    # 只 add 明确列出的路径，绝不 git add -A。
    # 注意：必须写 $existing（数组自动展开），不能写 @existing ——
    # @ 开头是 splatting 语法，只对 PowerShell 命令有效，对 git.exe 会静默错位。
    git add -- $existing
    if ($LASTEXITCODE -ne 0) { throw "git add 失败：$Title" }

    # 多行提交信息走临时文件（-F），避免经命令行参数传递时被重新分词
    $msgFile = Join-Path $env:TEMP ("commit-msg-{0}.txt" -f ([guid]::NewGuid().ToString('N')))
    # git 按 UTF-8 读 -F 文件，这里必须不带 BOM，否则首行会带上 BOM 字符
    [IO.File]::WriteAllText($msgFile, $Message, (New-Object Text.UTF8Encoding($false)))
    try {
        git commit -F $msgFile
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[空提交] $Title —— 这些文件没有可提交的改动" -ForegroundColor Yellow
            return
        }
    } finally {
        Remove-Item -LiteralPath $msgFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[完成] $Title" -ForegroundColor Green
    Write-Host ''
}

Invoke-Group -Title '固件：运行期掉线重连 + 状态可观测' -Files @(
    'applications/esp32s3/s3_wifi_portal/src/main.c',
    'applications/esp32s3/s3_wifi_portal/prj.conf',
    'applications/esp32s3/s3_wifi_portal/README.md'
) -Message @'
feat(wifi): 补运行期掉线自动重连，并把链路状态暴露到网页

- 新增 g_rt_reconnect/g_rt_attempts，掉线后按 3→6→12→24 s 退避重试，上限 10 次
- 与上电重连（3 次/固定 3 s）分开计数；连接中掉线只由 CONNECT_RESULT 计一次
- 连满 30 s 才清零重连次数，避免信号边缘"连 2 秒又掉"绕过上限
- 超时和驱动拒收两条路径也接入退避，不再直接停红灯
- /api/status 增加 rssi/reconnects/uptime；网页显示信号与重连次数
- 新增 portal status / portal drop shell 命令，用于现场验证重连
- DHCP 服务池 4→16 槽、租约 86400→900 s，修复手机反复重连拿不到 IP
- LED：检查 led_strip_update_rgb 返回值并打日志，亮度上限 160，优先级 2，栈 2048
'@

Invoke-Group -Title '工具：Wi-Fi 吞吐基线与离线自测' -Files @(
    'tools/wifi_bench.py',
    'tools/selftest_wifi_bench.py',
    'tools/syntax_check_main.py'
) -Message @'
tools: 增加 Wi-Fi 应用层吞吐基线脚本

- wifi_bench.py：零第三方依赖，测 /api/status、/index.html、/api/scan，
  输出中位延迟与近似 kbit/s，并按页面带宽估算固件传输耗时（OTA 可行性判断）
- selftest_wifi_bench.py：起本地假 HTTP 服务跑通 wifi_bench.py，无需板子即可验证工具
- syntax_check_main.py：从 compile_commands.json 取参数做单文件 -fsyntax-only
'@

Invoke-Group -Title '文档：两套重连计数与 Wi-Fi 能力说明' -Files @(
    'docs/教程/17-Zephyr无线能力/README.md',
    'docs/教程/16-Zephyr外设进阶/S3能力地图.md',
    'docs/教程/README.md'
) -Message @'
docs: 补充运行期掉线重连与 Wi-Fi 能力条目

- 17 组新增 6.5.1：上电重连与运行期掉线重连是两套计数，含对照表、
  退避理由、不能"连上即清零"的原因、was_connecting 边界
- 16 组能力地图与教程总索引同步 Wi-Fi 当前实现状态
'@

Write-Host '==== 提交后的最近 3 条 ====' -ForegroundColor Cyan
git log --oneline -3
Write-Host ''
Write-Host '==== 仍未跟踪的文件（应只剩 logs 等你不想纳管的东西）====' -ForegroundColor Cyan
git status --porcelain
