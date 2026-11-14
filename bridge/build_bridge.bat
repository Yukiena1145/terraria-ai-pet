@echo off
setlocal
cd /d "%~dp0"
set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo [ERROR] .NET Framework 4.8 csc.exe not found.
    pause
    exit /b 1
)
"%CSC%" -nologo -platform:x86 ^
  -r:"Microsoft.Diagnostics.Runtime.dll" ^
  -out:"clrmd_probe32.exe" ^
  "probe.cs"
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)
echo Built bridge\clrmd_probe32.exe
pause
