#!/bin/sh
# ---------------------------------------------------------------------------
# 安装 / 更新 edge-router（「域名前缀 → 本机端口」分流器）
#
#   sudo sh install.sh            # 安装到 /etc/esc/edge 并启动容器
#   sudo sh install.sh --reload   # 只重装配置并热加载
#
# 幂等：可以反复执行。
# ---------------------------------------------------------------------------
set -eu
DIR=/etc/esc/edge
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = "0" ] || { echo "请用 root 执行" >&2; exit 1; }

mkdir -p "$DIR" /etc/ssl/edge
[ -f "$DIR/routes.tsv" ] || : > "$DIR/routes.tsv"
[ -f "$DIR/tls.conf" ] || : > "$DIR/tls.conf"

install -m 644 "$SRC/nginx.conf" "$DIR/nginx.conf"
install -m 755 "$SRC/edge-route" /usr/local/bin/edge-route

echo "已安装：$DIR/nginx.conf, /usr/local/bin/edge-route"

/usr/local/bin/edge-route reload

if [ "${1:-}" != "--reload" ]; then
  echo
  echo "用法："
  echo "  edge-route add cad 2228   # cad.任意域名 -> 127.0.0.1:2228"
  echo "  edge-route list"
fi
