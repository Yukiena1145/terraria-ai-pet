param(
    [string]$Destination = "F:\Terraria-Agent\save-backups",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$docs = [Environment]::GetFolderPath("MyDocuments")
$source = Join-Path $docs "My Games\Terraria"

if (-not (Test-Path -LiteralPath $source)) {
    throw "找不到 Terraria 存档目录: $source"
}

$running = Get-Process -Name "Terraria" -ErrorAction SilentlyContinue
if ($running -and -not $Force) {
    throw "Terraria 正在运行。请先完全退出游戏再备份，或使用 -Force 强制复制（可能复制到不完整的世界文件）。"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $Destination ("Terraria-saves-" + $stamp)
New-Item -ItemType Directory -Path $target -Force | Out-Null

$items = @(
    "Players",
    "Worlds",
    "config.json",
    "input profiles.json",
    "favorites.json",
    "achievements.dat"
)

foreach ($item in $items) {
    $src = Join-Path $source $item
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "跳过（不存在）: $item"
        continue
    }
    $dst = Join-Path $target $item
    Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    Write-Host "已备份: $item"
}

Write-Host ""
Write-Host "备份完成: $target"
Write-Host "文件列表:"
Get-ChildItem -LiteralPath $target -Recurse -File |
    Select-Object FullName, Length |
    Format-Table -AutoSize

Write-Host ""
Write-Host "恢复方法：退出 Terraria，把备份中的 Players / Worlds 覆盖回:"
Write-Host "  $source"
Write-Host "然后再启动游戏。"
