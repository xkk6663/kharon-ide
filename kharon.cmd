@echo off
rem kharon.cmd -- Kharon (Zephyr mode) entry point
rem usage: kharon bootstrap / kharon deps / kharon run -p f411_breath --steps doctor,build
rem 本仓只保留 Kharon；classic 入口与 DSH 插件在旧仓 dsh-elab-flow。
setlocal
set HERE=%~dp0
if "%ELAB_PYTHON%"=="" (set PY=python) else (set PY=%ELAB_PYTHON%)
set PYTHONPATH=%HERE%services;%PYTHONPATH%
rem 双根：IDE_ROOT = 本脚本目录；ZEPHYR_ROOT = 工作区根。
rem ELAB_ROOT 仅作兼容别名 = IDE_ROOT，新代码用 IDE_ROOT / ZEPHYR_ROOT。
if "%IDE_ROOT%"=="" if not "%ELAB_ROOT%"=="" set IDE_ROOT=%ELAB_ROOT%
if "%IDE_ROOT%"=="" set IDE_ROOT=%HERE:~0,-1%
if "%ELAB_ROOT%"=="" set ELAB_ROOT=%IDE_ROOT%
if "%ZEPHYR_ROOT%"=="" if not "%ELAB_ZEPHYR_ROOT%"=="" set ZEPHYR_ROOT=%ELAB_ZEPHYR_ROOT%
if "%ZEPHYR_ROOT%"=="" if exist "%IDE_ROOT%\..\zephyr-workspace\.west\config" set ZEPHYR_ROOT=%IDE_ROOT%\..\zephyr-workspace
if "%ZEPHYR_ROOT%"=="" set ZEPHYR_ROOT=%IDE_ROOT%\west
if "%ELAB_HOST%"=="" set ELAB_HOST=kharon
"%PY%" -m kharon %*
exit /b %ERRORLEVEL%
