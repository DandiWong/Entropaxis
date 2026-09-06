@echo off
chcp 65001 >nul
rem -------------------------------------------------------------
rem 工作区一键初始化与自愈脚本 · Windows（双击即可运行）
rem macOS / Linux 请改用同目录下的 .command 脚本
rem -------------------------------------------------------------
cd /d "%~dp0..\.." || exit /b 1

echo ========================================================
echo   正在为您初始化智能工作区环境...
echo ========================================================
echo.

rem Windows 上 python3 通常不在 PATH，优先用官方启动器 py -3
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python3 >nul 2>nul && set "PY=python3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [X] 未检测到 Python 3。请先从 https://www.python.org/downloads/ 安装
    echo     并勾选 "Add python.exe to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

%PY% .system\tools\bootstrap.py
if errorlevel 1 (
    echo.
    echo [X] 初始化失败。请把上方报错整段复制给 AI 助手处理。
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   初始化完成！可直接在 Claude Code / OMP 中开始对话。
echo ========================================================
echo.
pause
