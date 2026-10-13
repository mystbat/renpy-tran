<#
.SYNOPSIS
    清理已入库的敏感/大文件历史（git filter-repo）。

.DESCRIPTION
    本仓库纪律（ADR-0004、全局纪律 #5）要求：config.ini、*.rpy、*.rpyc、*.rpa、
    以及任何真实游戏数据 / 密钥都不得入库。若历史里曾经误提交过这些内容，
    用本脚本把它们从历史中彻底抹除。

    ⚠️ 警告（操作前必读）：
      1. git filter-repo 会**重写整段提交历史**（改所有受影响的 commit hash）。
         执行后所有协作者必须重新 clone / rebase，强推会破坏他人本地分支。
      2. 本脚本默认是“演练”（--dry-run 思路）：只列出将要清除的目标，
         并不真正改写。确认无误后，把 SCRIPT:DO_IT 设为 $true 再运行。
      3. 强烈建议：先对整个仓库做一次完整备份（复制整个文件夹 / 打 bundle）。
      4. 需要 git-filter-repo（Git 2.22+ 自带插件，或用 `pip install git-filter-repo`）。
      5. 不要在此脚本或任何被清洗文件里写入真实 API Key。

.EXAMPLE
    # 1) 先演练，看会清掉哪些文件：
    pwsh scripts/clean_git_history.ps1

    # 2) 备份后真正执行：
    $env:PYTRAN_DO_IT = '1'
    pwsh scripts/clean_git_history.ps1
#>

[CmdletBinding()]
param(
    # 设为 $true 才真正改写历史；默认只打印将要清除的目标。
    [switch] $DoIt
)

$EnvFlag = $env:PYTRAN_DO_IT
$ActuallyRun = $DoIt -or ($EnvFlag -eq '1' -or $EnvFlag -eq 'true')

$ErrorActionPreference = 'Stop'

# ---- 1. 校验环境 ----
function Assert-Command($cmd) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "缺少命令：$cmd 。请先安装 Git（含 filter-repo）或运行 pip install git-filter-repo"
    }
}
Assert-Command git
if (-not (git filter-repo --help 2>&1 | Select-String -Quiet 'filter-repo')) {
    throw "git filter-repo 不可用。安装：pip install git-filter-repo （或启用 Git 的 filter-repo 插件）"
}

$RepoRoot = if ($PSScriptRoot) { Resolve-Path (Join-Path $PSScriptRoot '..') } else { Resolve-Path '.' }
Set-Location $RepoRoot

# ---- 2. 定义要清除的路径 / 模式（与 .gitignore 敏感项对齐） ----
# 这些模式一旦曾在历史中出现，就应从所有历史提交中抹除。
$Patterns = @(
    'config.ini',            # ADR-0004：密钥永不入库存
    'glob:*.rpy',            # 用户游戏脚本
    'glob:*.rpyc',
    'glob:*.rpa',
    'glob:archive/**',       # 真实游戏/旧产物（gitignored，但历史可能漏过）
    'glob:split_csv/**',
    'glob:rpybak/**',
    'glob:csv/**',
    'glob:output/**',
    'glob:logs/**'
)

Write-Host ("=" * 70)
Write-Host ("模式数量：" + $Patterns.Count)
$Patterns | ForEach-Object { Write-Host "  清洗目标: $_" }
Write-Host ("=" * 70)

if (-not $ActuallyRun) {
    Write-Host "[演练模式] 未真正改写历史。以下命令将在确认后执行：`n" -ForegroundColor Yellow
    $replaceText = ($Patterns | ForEach-Object { "regex:^$_`$" }) -join "`n"
    Write-Host "  git filter-repo --invert-paths --path-glob '<pattern>' ..."
    Write-Host ""
    Write-Host "设置 `$env:PYTRAN_DO_IT='1' 或 -DoIt 后重新运行以真正执行。"
    Write-Host "备份示例： git bundle create ../py-tran-backup.bundle --all"
    exit 0
}

# ---- 3. 真正执行（已确认） ----
Write-Host "[执行模式] 正在重写历史……" -ForegroundColor Red

# --invert-paths：保留未匹配的路径，删除匹配到的（即把敏感文件从历史抹掉）。
$Args = @(
    '--invert-paths'
)
foreach ($p in $Patterns) {
    if ($p.StartsWith('glob:')) {
        $Args += '--path-glob'
        $Args += $p.Substring(5)
    } else {
        $Args += '--path'
        $Args += $p
    }
}
# 强制（已在上方警告过）；如需保留 clone 的 remote 引用，可去掉 --force。
$Args += '--force'

Write-Host "执行：git filter-repo $($Args -join ' ')"
git filter-repo @Args

Write-Host ""
Write-Host "完成。请验证：git log --all --stat | Select-String -Quiet 'config.ini|*.rpy'"
Write-Host "随后强推（仅限你有权限的远端，且已通知协作者）： git push --force --all"
