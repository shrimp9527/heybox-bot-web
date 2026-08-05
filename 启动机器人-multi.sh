#!/usr/bin/env bash
# 本文件需以 UTF-8 编码保存
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  小黑盒AI自动回复"
echo "================================================"
echo

# 检查 Python 是否安装
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未检测到 python3，请先安装 Python 3.9+"
    echo "Debian/Ubuntu: sudo apt install python3 python3-pip"
    exit 1
fi

# 检查 Python 版本（3.9+）
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
    echo "[错误] Python 版本过低，需要 3.9+，当前: $(python3 --version 2>&1)"
    exit 1
fi

echo "Python 检测通过 ✓ ($(python3 --version 2>&1))"

# 检查依赖（缺失时尝试安装；app.py 启动时也会自动补装缺失依赖）
echo
if python3 -c "import flask, flask_cors, requests, PIL, tavily" 2>/dev/null; then
    echo "依赖已安装 ✓"
else
    echo "正在安装依赖（首次运行可能需要几十秒）..."
    if python3 -m pip install -r requirements.txt --default-timeout=120; then
        echo "依赖安装完成 ✓"
    else
        echo "[警告] 依赖安装失败，启动时将由 app.py 再次尝试自动补装"
    fi
fi

echo
echo "正在启动服务..."
echo

if ! python3 app.py; then
    echo
    echo "================================================"
    echo "  [错误] 服务启动失败!"
    echo "================================================"
    echo "可能的原因:"
    echo "1. 端口 5500 已被占用（请先关闭其他实例）"
    echo "2. 缺少项目文件"
    echo "3. Python 环境配置问题"
    echo "================================================"
    exit 1
fi
