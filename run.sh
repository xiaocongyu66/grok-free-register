#!/bin/bash
# 运行注册机(高级入口;新手用 bash start.sh)
# 配置走 .env(见 .env.example),CLI 可加 --max-mem 6G
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "首次运行,请先执行: bash setup.sh   (或直接 bash start.sh)"
    exit 1
fi
exec .venv/bin/python register.py "$@"
