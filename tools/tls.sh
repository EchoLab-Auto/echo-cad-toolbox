#!/bin/sh
# ---------------------------------------------------------------------------
# 给 echo-cad-toolbox 加一层 TLS 前置：同一个端口同时吃 http:// 和 https://
#
# 为什么要 https：WebGPU（navigator.gpu）是 [SecureContext] 接口，浏览器的安全
# 上下文判定只看 URL scheme —— http:// 的公网地址拿不到 navigator.gpu，工作台
# 会退回服务端生成（能用，但慢一到两个数量级）。换成 https:// 就能用本机显卡。
#
# 为什么必须用 https 才有 WebGPU：这不是本项目的限制，是 Web 平台规范。
#   http://localhost 和 file:// 算安全上下文，公网 http:// 不算。
#
# 用法（幂等，可重复执行）：
#   ./tools/tls.sh up                 # 生成（或复用）证书 + 起前置容器
#   ./tools/tls.sh down               # 拆掉前置容器（后端端口自己恢复）
#   ./tools/tls.sh status             # 看证书与前程状态
#   ./tools/tls.sh renew              # 强制重签证书
#
# 环境变量：
#   PORT          对外端口（默认 2228）—— 前置容器监听它
#   BACKEND_PORT  后端回环端口（默认 18080）—— 工具箱容器绑 127.0.0.1:这个
#   TLS_TERM_PORT 内部 TLS 终止端口（默认 = BACKEND_PORT + 617，只绑回环）
#   CERT_DIR      证书目录（默认 /etc/ssl/cad）
#   CERT_HOSTS    额外 SAN，逗号分隔（如 cad.example.com）
#   DAYS          证书有效期天数（默认 825）
#   FORCE         设为 1 时无条件重建容器
# ---------------------------------------------------------------------------
set -e

cd "$(dirname "$0")/.."

PORT="${PORT:-2228}"
BACKEND_PORT="${BACKEND_PORT:-18080}"
TLS_TERM_PORT="${TLS_TERM_PORT:-$((BACKEND_PORT + 617))}"
CERT_DIR="${CERT_DIR:-/etc/ssl/cad}"
NAME="${NAME:-echo-cad-toolbox}"
TLS_NAME="${TLS_NAME:-${NAME}-tls}"
IMG="${TLS_IMAGE:-nginx:alpine}"
DAYS="${DAYS:-825}"
CONF_DIR="${CONF_DIR:-/etc/cad-tls}"

# --------------------------------------------------------------------------- #
detect_public_ip() {
  # 证书 SAN 里必须有一个**用户实际会用的**地址，否则浏览器连"点继续前往"
  # 的机会都不给（SAN 不匹配 = 硬错误）。公网 IP 优先，退化到本机网卡。
  for u in https://ifconfig.me https://api.ipify.org https://ipinfo.io/ip; do
    ip=$(curl -s -m 5 "$u" 2>/dev/null | tr -d '\r\n ')
    case "$ip" in
      ""|*[!0-9.]*) continue ;;
    esac
    echo "$ip"
    return 0
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

gen_cert() {
  mkdir -p "$CERT_DIR"
  pub=$(detect_public_ip)
  san="IP:127.0.0.1,DNS:localhost"
  [ -n "$pub" ] && san="$san,IP:$pub"
  # 本机所有网卡地址也带上：内网/专线访问时同样不报 SAN 不匹配
  for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in 127.*) continue ;; esac
    case "$san" in *"IP:$ip"*) ;; *) san="$san,IP:$ip" ;; esac
  done
  if [ -n "${CERT_HOSTS:-}" ]; then
    for h in $(echo "$CERT_HOSTS" | tr ',' ' '); do san="$san,DNS:$h"; done
  fi

  echo "      生成自签证书 → $CERT_DIR（有效期 $DAYS 天）"
  echo "        SAN: $san"
  # -addext 需要 OpenSSL 1.1.1+；这里同时写 basicConstraints/keyUsage，
  # 否则部分浏览器对自签证书的用途检查更严。
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days "$DAYS" \
    -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" \
    -subj "/CN=echo-cad-toolbox/O=echo-cad-toolbox (self-signed)" \
    -addext "subjectAltName=$san" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" >/dev/null 2>&1
  chmod 644 "$CERT_DIR/privkey.pem"   # nginx worker 以非 root 跑，需可读
  chmod 644 "$CERT_DIR/fullchain.pem"
}

render_conf() {
  mkdir -p "$CONF_DIR"
  sed -e "s|__TLS_PORT__|$PORT|g" \
      -e "s|__BACKEND_PORT__|$BACKEND_PORT|g" \
      -e "s|__TLS_TERM_PORT__|$TLS_TERM_PORT|g" \
      -e "s|__CERT_DIR__|/etc/nginx/certs|g" \
      tools/tls/nginx.conf.template > "$CONF_DIR/nginx.conf"
}

# --------------------------------------------------------------------------- #
cmd="${1:-up}"

case "$cmd" in
  up)
    [ -f "$CERT_DIR/fullchain.pem" ] && [ -f "$CERT_DIR/privkey.pem" ] || gen_cert
    # 证书快过期了（<7 天）就顺手重签，避免哪天突然打不开
    if openssl x509 -checkend $((7 * 86400)) -noout -in "$CERT_DIR/fullchain.pem" >/dev/null 2>&1; then
      :
    else
      echo "      证书将在 7 天内过期，重签"
      gen_cert
    fi

    render_conf
    # 先验配置：nginx -t 不过就完全不碰运行中的实例
    if ! docker run --rm -v "$CONF_DIR/nginx.conf:/etc/nginx/nginx.conf:ro" \
         -v "$CERT_DIR:/etc/nginx/certs:ro" "$IMG" nginx -t >/tmp/cad-tls-check.log 2>&1; then
      echo "      ✗ nginx 配置校验失败，未改动运行中的实例："
      grep -E "emerg|error" /tmp/cad-tls-check.log | tail -5 | sed 's/^/        /'
      exit 1
    fi

    docker rm -f "$TLS_NAME" >/dev/null 2>&1 || true
    # host 网络：要够到 127.0.0.1 上的后端，也要直接绑宿主机端口
    docker run -d --name "$TLS_NAME" --restart unless-stopped --network host \
      -v "$CONF_DIR/nginx.conf:/etc/nginx/nginx.conf:ro" \
      -v "$CERT_DIR:/etc/nginx/certs:ro" \
      --label "app=$TLS_NAME" \
      "$IMG" >/dev/null
    sleep 2
    if ! docker ps --filter "name=$TLS_NAME" --filter "status=running" -q | grep -q .; then
      echo "      ✗ 前置容器起不来："
      docker logs --tail 15 "$TLS_NAME" 2>&1 | sed 's/^/        /'
      exit 1
    fi
    echo "      ✓ TLS 前置就绪：:$PORT 同时服务 http/https（后端 127.0.0.1:$BACKEND_PORT）"
    ;;

  down)
    docker rm -f "$TLS_NAME" >/dev/null 2>&1 || true
    echo "      已拆除 $TLS_NAME"
    ;;

  renew)
    gen_cert
    exec "$0" up
    ;;

  status)
    echo "证书：$CERT_DIR"
    if [ -f "$CERT_DIR/fullchain.pem" ]; then
      openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -dates -ext subjectAltName 2>/dev/null | sed 's/^/  /'
    else
      echo "  （还没有证书）"
    fi
    echo "前置容器："
    docker ps -a --filter "name=$TLS_NAME" --format '  {{.Names}}  {{.Image}}  {{.Status}}' || true
    ;;

  *)
    echo "用法：$0 {up|down|renew|status}" >&2
    exit 2
    ;;
esac
