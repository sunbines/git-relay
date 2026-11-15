@echo off
setlocal
set "HERE=%~dp0"

rem 无参数：打开图形界面，不创建控制台窗口。
if "%~1"=="" goto gui

rem 有参数：交给命令行版本执行。
if exist "%HERE%git-relay-cli.exe" goto cli
goto use_python

:gui
if exist "%HERE%git-relay.exe" (
    start "" "%HERE%git-relay.exe"
    exit /b 0
)
goto use_python

:cli
"%HERE%git-relay-cli.exe" %*
exit /b %errorlevel%

:use_python
set "SCRIPT=%HERE%git_relay.py"
where py >nul 2>nul
if errorlevel 1 goto use_python_cmd
py -3 "%SCRIPT%" %*
exit /b %errorlevel%

:use_python_cmd
python "%SCRIPT%" %*
exit /b %errorlevel%
