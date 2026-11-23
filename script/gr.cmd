@echo off
rem Short alias for git-relay.
setlocal
set "HERE=%~dp0"
set "DIST=%HERE%"
if exist "%HERE%..\dist\" set "DIST=%HERE%..\dist\"
set "SRC=%HERE%..\src\git_relay.py"

rem 无参数：打开图形界面，不创建控制台窗口。
if "%~1"=="" goto gui

rem 有参数：交给命令行版本执行。
if exist "%DIST%git-relay-cli.exe" goto cli
goto use_python

:gui
if exist "%DIST%git-relay.exe" (
    start "" "%DIST%git-relay.exe"
    exit /b 0
)
goto use_python

:cli
"%DIST%git-relay-cli.exe" %*
exit /b %errorlevel%

:use_python
if not exist "%SRC%" (
    echo [git-relay] 找不到 %SRC%
    exit /b 1
)
where py >nul 2>nul
if errorlevel 1 goto use_python_cmd
py -3 "%SRC%" %*
exit /b %errorlevel%

:use_python_cmd
python "%SRC%" %*
exit /b %errorlevel%
