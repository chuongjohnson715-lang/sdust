<#
.SYNOPSIS
    从空白骨架实例化一门课程的「五件套」产物目录（机械复制，不做内容推断）。

.DESCRIPTION
    1) 复制 templates/课程复盘五件套/ 到目标目录（不复制模板自用的 README.md）
    2) 全文替换 {{课程名}}
    3) 列出剩余占位符与说明注释数量
    4) 打印填写顺序提示

    格式契约见仓库根 AGENTS.md 第 6 节。
    注意：本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会把中文读成乱码。

.PARAMETER Target
    目标目录，相对当前目录或绝对路径。建议命名 <课程简称>-<学期>。

.PARAMETER CourseName
    课程全名，用于替换模板中的 {{课程名}}。

.PARAMETER Force
    目标已存在时强制覆盖。默认拒绝，避免误删已填内容。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\templates\new-course.ps1 -Target ".\示例课程-2026秋" -CourseName "示例课程"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$CourseName,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$extAllow = @('.md', '.csv', '.html')
$templateDir = Join-Path $PSScriptRoot '课程复盘五件套'

if (-not (Test-Path -LiteralPath $templateDir)) {
    throw "找不到骨架目录：$templateDir"
}
if ($CourseName -match '\{\{|\}\}') {
    throw "CourseName 不能包含 {{ 或 }}：$CourseName"
}

$targetDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Target)
if ((Test-Path -LiteralPath $targetDir) -and (-not $Force)) {
    throw "目标已存在：$targetDir`n确认要覆盖请追加 -Force（会删除该目录内全部内容）"
}

# ---- 1) 复制骨架 ----
if (Test-Path -LiteralPath $targetDir) {
    Remove-Item -LiteralPath $targetDir -Recurse -Force
}
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
Copy-Item -Path (Join-Path $templateDir '*') -Destination $targetDir -Recurse -Force
Remove-Item -LiteralPath (Join-Path $targetDir 'README.md') -ErrorAction SilentlyContinue

# ---- 2) 替换课程名并统计 ----
$files = Get-ChildItem -LiteralPath $targetDir -Recurse -File |
    Where-Object { $extAllow -contains $_.Extension.ToLowerInvariant() }

$stat = @{}
$commentCount = 0
$slug = Split-Path -Leaf $targetDir
foreach ($f in $files) {
    $text = [System.IO.File]::ReadAllText($f.FullName, $utf8NoBom)
    $text = $text.Replace('{{课程名}}', $CourseName).Replace('{{课程简称}}', $slug)
    # CSV 写出时保留 BOM：中文 Windows 的 Excel 双击打开才不乱码
    $enc = if ($f.Extension -ieq '.csv') { $utf8Bom } else { $utf8NoBom }
    [System.IO.File]::WriteAllText($f.FullName, $text, $enc)

    foreach ($m in [regex]::Matches($text, '\{\{([^{}]+)\}\}')) {
        $key = $m.Groups[1].Value
        if ($stat.ContainsKey($key)) { $stat[$key] = $stat[$key] + 1 } else { $stat[$key] = 1 }
    }
    $commentCount += ([regex]::Matches($text, '<!--')).Count
}

# ---- 3) 报告 ----
$total = 0
foreach ($v in $stat.Values) { $total += $v }

Write-Host ''
Write-Host ("已实例化：{0}" -f $targetDir)
Write-Host ("已替换：{{课程名}} -> {0}；{{课程简称}} -> {1}" -f $CourseName, $slug)
Write-Host ("文件数：{0}" -f $files.Count)
Write-Host ''
Write-Host ("剩余占位符：{0} 处，去重 {1} 种（下列按出现次数取前 15）" -f $total, $stat.Count)
$top = $stat.GetEnumerator() | Sort-Object -Property Value -Descending | Select-Object -First 15
foreach ($e in $top) {
    Write-Host ('  {{' + $e.Key + '}} x ' + $e.Value)
}
if ($stat.Count -gt 15) { Write-Host ("  …其余 {0} 种略" -f ($stat.Count - 15)) }
Write-Host ("填写说明注释：{0} 处（定稿前全部删除）" -f $commentCount)
Write-Host ''
Write-Host '填充顺序（不可颠倒）：'
Write-Host '  04 证据分册 -> 04 索引（视图三->二->一）-> 02 逐讲 -> 03 知识体系 -> 01 考情 -> 00 使用说明 -> assets 脑图 -> 自检'
Write-Host ''
Write-Host '验收（必须为 0）：'
Write-Host "  Get-ChildItem -Recurse -File | Where-Object { '.md','.csv','.html' -contains `$_.Extension } | Select-String -Pattern '\{\{|<!--' | Measure-Object"
