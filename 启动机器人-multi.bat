@echo off
:: 本文件需以 UTF-8 编码保存
chcp 65001 >nul
cd /d "%~dp0"
title 小黑盒AI自动回复

echo ================================================
echo   小黑盒AI自动回复
echo ================================================
echo.

:: 检查 Python 是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo Python 检测通过 ✓

:: 安装依赖
echo.
python -c "import flask, flask_cors, requests, PIL, tavily"
if %errorlevel% equ 0 (
    echo 依赖已安装 ✓
) else (
    echo 正在安装依赖（首次运行可能需要几十秒）...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=120
    if %errorlevel% neq 0 (
        echo [提示] 国内镜像安装失败，尝试默认源...
        python -m pip install -r requirements.txt --default-timeout=120
        if %errorlevel% neq 0 (
            echo [警告] 依赖安装失败，请手动执行: python -m pip install -r requirements.txt
            echo 按任意键继续尝试启动...
            pause >nul
        )
    )
    echo 依赖安装完成 ✓
)

echo.
echo 正在启动服务...
echo.

python app.py

if %errorlevel% neq 0 (
    echo.
    echo ================================================
    echo   [错误] 服务启动失败!
    echo ================================================
    echo 可能的原因:
    echo 1. 端口 5500 已被占用（请先关闭原版程序）
    echo 2. 缺少项目文件
    echo 3. Python 环境配置问题
    echo.
    echo 错误代码: %errorlevel%
    echo ================================================
    echo.
)

echo.
echo 服务已停止，按任意键关闭窗口...
pause >nul