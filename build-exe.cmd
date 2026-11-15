@echo off
setlocal
set "HERE=%~dp0"
if not exist "%HERE%build-exe.ps1" goto missing

where powershell >nul 2>nul
if errorlevel 1 goto use_pwsh
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%build-exe.ps1" %*
exit /b %errorlevel%

:use_pwsh
pwsh -NoProfile -File "%HERE%build-exe.ps1" %*
exit /b %errorlevel%

:missing
echo [build] 找不到 build-exe.ps1
exit /b 1
