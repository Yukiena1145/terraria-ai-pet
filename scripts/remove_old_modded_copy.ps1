param(
    [string]$Target = "F:\terraria\【PC】Terraria v1.4.5.8",
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Target)) {
    Write-Host "目标不存在，无需删除: $Target"
    exit 0
}

if (Get-Process -Name "Terraria" -ErrorAction SilentlyContinue) {
    throw "Terraria 正在运行，请先完全退出。"
}

$backupRoot = "F:\Terraria-Agent\save-backups"
$backups = Get-ChildItem -LiteralPath $backupRoot -Directory -Filter "Terraria-saves-*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending

if (-not $backups) {
    throw "没有找到存档备份。请先运行 scripts\backup_terraria_saves.ps1。"
}

Write-Host "已找到存档备份: $($backups[0].FullName)"
Write-Host "准备删除旧整合包目录:"
Write-Host "  $Target"
Write-Host ""
Write-Host "不会删除:"
Write-Host "  $env:USERPROFILE\Documents\My Games\Terraria"
Write-Host ""

if (-not $Confirm) {
    Write-Host "当前为预览模式。确认无误后运行:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Confirm"
    exit 0
}

Remove-Item -LiteralPath $Target -Recurse -Force
Write-Host "已删除: $Target"
