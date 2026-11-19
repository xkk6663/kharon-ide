@echo off
rem kharon.cmd -- Kharon (Zephyr mode) entry point
rem usage: kharon bootstrap / kharon deps / kharon run -p f411_breath --steps doctor,build
rem 本仓只保留 Kharon；classic 入口与 DSH 插件在旧仓 dsh-elab-flow。
setlocal
set HERE=%~dp0
if "%ELAB_PYTHON%"=="" (set PY=python) else (set PY=%ELAB_PYTHON%)
set PYTHONPATH=%HERE%services;%PYTHONPATH%
if "%ELAB_ROOT%"=="" set ELAB_ROOT=%HERE:~0,-1%
if "%ELAB_HOST%"=="" set ELAB_HOST=kharon
"%PY%" -m kharon %*
exit /b %ERRORLEVEL%
