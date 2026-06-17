#!/bin/bash
# 一键启动:自动装依赖 → 引导配置 → 运行
# 用法:
#   bash start.sh              # 首次会引导选模式,之后直接启动
#   bash start.sh --reconfig   # 重新选择邮箱模式
set -e
cd "$(dirname "$0")"

# 1) 依赖:没有 venv 就自动安装
if [ ! -d .venv ]; then
    echo "[*] 首次运行,安装依赖..."
    bash setup.sh
fi

# 2) 配置:无 .env 或显式 --reconfig 时进入引导
if [ ! -f .env ] || [ "${1:-}" = "--reconfig" ]; then
    echo ""
    echo "选择邮箱模式:"
    echo "  [1] 免费临时邮箱           (默认 · 零配置 · 直接回车 · 多 provider 自动 fallback)"
    echo "  [2] 自建域名邮箱           (需 Cloudflare Email Routing + 本地 webhook)"
    read -rp "输入 1 或 2 [1]: " mode || mode=1
    if [ "$mode" = "2" ]; then
        read -rp "  你的域名 (如 example.com): " domain
        read -rp "  webhook 地址 [http://127.0.0.1:8080]: " api
        api=${api:-http://127.0.0.1:8080}
        cat > .env <<ENV
EMAIL_MODE=custom
EMAIL_DOMAIN=${domain}
EMAIL_API=${api}
# 资源上限(可选,留空=自动)
# MAX_SLOTS=
# CPU_TARGET=85
# MIN_FREE_MEM_MB=500
ENV
        echo ""
        echo "[!] custom 模式还需在另一终端运行收信服务:"
        echo "      .venv/bin/python email_server.py"
        echo "    并按 README「自建邮箱模式」配置 Cloudflare Email Worker。"
    else
        echo "EMAIL_MODE=tempmail" > .env
    fi
    echo "[*] 已写入 .env"
fi

# --reconfig 不传给 register.py
[ "${1:-}" = "--reconfig" ] && shift || true

# 3) 运行
echo "[*] 启动注册机... (Ctrl-C 停止;成功账号写入 keys/accounts.txt)"
exec .venv/bin/python register.py "$@"
