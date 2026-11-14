$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Write-Step($text) {
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan
}

Write-Step "Terraria-Agent 一键安装"

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$pythonInstaller = Join-Path $Root "downloads\python-3.11.9-amd64.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "没有找到项目虚拟环境，准备 Python 3.11..." -ForegroundColor Yellow

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $pythonOk = $false
    if ($pyLauncher) {
        try {
            & py -3.11 -c "import sys; print(sys.version)" | Out-Null
            $pythonOk = $true
        } catch {
            $pythonOk = $false
        }
    }

    if (-not $pythonOk) {
        if (Test-Path -LiteralPath $pythonInstaller) {
            Write-Host "正在安装 Python 3.11（当前用户，不需要管理员）..."
            Start-Process -FilePath $pythonInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
        } else {
            $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
            $out = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
            Write-Host "正在下载 Python 3.11..."
            Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
            Start-Process -FilePath $out -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
        }
    }

    Write-Host "创建项目虚拟环境..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv .venv
    } else {
        & python -m venv .venv
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python 虚拟环境创建失败。请手动安装 Python 3.11 后重试。"
}

Write-Step "安装项目依赖（第一次可能 1-3 分钟）"
& $venvPython -m pip install --disable-pip-version-check --upgrade pip
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")

Write-Step "查找官方 Terraria"
$candidates = @(
    "E:\SteamLibrary\steamapps\common\Terraria\Terraria.exe",
    "D:\SteamLibrary\steamapps\common\Terraria\Terraria.exe",
    "C:\Program Files (x86)\Steam\steamapps\common\Terraria\Terraria.exe",
    "C:\Program Files\Steam\steamapps\common\Terraria\Terraria.exe"
)

$steamKeys = @(
    "HKCU:\Software\Valve\Steam",
    "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam",
    "HKLM:\SOFTWARE\Valve\Steam"
)
foreach ($key in $steamKeys) {
    try {
        $steamPath = (Get-ItemProperty -Path $key -ErrorAction Stop).SteamPath
        if ($steamPath) {
            $candidates += (Join-Path $steamPath "steamapps\common\Terraria\Terraria.exe")
        }
    } catch { }
}

$gamePath = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        $gamePath = $candidate
        break
    }
}

if (-not $gamePath) {
    Write-Host "没有自动找到 Terraria.exe，请手动选择。" -ForegroundColor Yellow
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择官方 Terraria.exe"
    $dialog.Filter = "Terraria.exe|Terraria.exe|所有程序|*.exe"
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $gamePath = $dialog.FileName
    }
}

if (-not $gamePath) {
    throw "没有选择 Terraria.exe，安装中止。"
}

Write-Host "游戏路径: $gamePath" -ForegroundColor Green

Write-Step "写入配置"
$configPath = Join-Path $Root "config.json"
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    $config = Get-Content -LiteralPath (Join-Path $Root "config.example.json") -Raw -Encoding UTF8 | ConvertFrom-Json
}

$config.game.exe_path = $gamePath
$config.game.window_titles = @("Terraria", "泰拉瑞亚")
$config.input_backend = "keyboard"
$config.memory.enabled = $true
$config.memory.tiles_enabled = $true
$config.server.host = "0.0.0.0"
$config | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $configPath -Encoding UTF8

Write-Step "创建桌面快捷方式"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Terraria AI.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $Root "一键启动.bat"
$shortcut.WorkingDirectory = $Root
$shortcut.Description = "启动 Terraria AI"
$shortcut.Save()
Write-Host "已创建: $shortcutPath" -ForegroundColor Green

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
$answer = Read-Host "现在启动吗？(Y/n)"
if ($answer -eq "" -or $answer -match "^[Yy]") {
    Start-Process -FilePath (Join-Path $Root "一键启动.bat") -WorkingDirectory $Root
}
