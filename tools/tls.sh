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
# 证书是两段式的（本地 CA + 服务端叶子）：不改客户端也能用（首次点一次
# 『继续前往』），想彻底没有警告就把 ca.pem 装进系统/浏览器的信任库。
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

build_san() {
  # SAN 里必须包含用户**实际会用的**地址，否则浏览器连"点继续前往"的机会都不给
  # （SAN 不匹配是硬错误，不给点）。公网 IP 优先，再加所有本机网卡和 localhost。
  _pub=$(detect_public_ip)
  _san="IP:127.0.0.1,DNS:localhost"
  [ -n "$_pub" ] && _san="$_san,IP:$_pub"
  for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in 127.*) continue ;; esac
    case "$_san" in *"IP:$ip"*) ;; *) _san="$_san,IP:$ip" ;; esac
  done
  if [ -n "${CERT_HOSTS:-}" ]; then
    for h in $(echo "$CERT_HOSTS" | tr ',' ' '); do _san="$_san,DNS:$h"; done
  fi
  echo "$_san"
}

gen_cert() {
  mkdir -p "$CERT_DIR"
  san=$(build_san)

  # 两段式签发：自建 CA + 服务端证书。
  #
  # 为什么不直接签一张自签叶子证书：只是"能用"的话一张自签叶子就够了（浏览器
  # 点一次继续前往）。但想**彻底没有警告**就必须把根证书装进客户端信任库，
  # 而信任库只接受 CA:TRUE 的证书 —— 自签叶子是 CA:FALSE，装进去 Chrome 会报
  # "certificate is not a CA"。所以按 mkcert 的路子走：本地 CA 签叶子，
  # 客户端只需装 ca.pem 这一张，换 IP/续签叶子时都不用再动客户端。
  echo "      生成证书 → $CERT_DIR（叶子 $DAYS 天 / CA 10 年）"
  echo "        SAN: $san"

  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -keyout "$CERT_DIR/ca.key" -out "$CERT_DIR/ca.pem" \
    -subj "/CN=echo-cad-toolbox local CA/O=echo-cad-toolbox" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1

  openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/leaf.csr" \
    -subj "/CN=echo-cad-toolbox/O=echo-cad-toolbox" >/dev/null 2>&1

  cat > "$CERT_DIR/leaf.ext" <<EOF
subjectAltName=$san
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF

  openssl x509 -req -in "$CERT_DIR/leaf.csr" -sha256 -days "$DAYS" \
    -CA "$CERT_DIR/ca.pem" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
    -extfile "$CERT_DIR/leaf.ext" -out "$CERT_DIR/leaf.pem" >/dev/null 2>&1

  cat "$CERT_DIR/leaf.pem" "$CERT_DIR/ca.pem" > "$CERT_DIR/fullchain.pem"
  rm -f "$CERT_DIR/leaf.csr" "$CERT_DIR/leaf.ext" "$CERT_DIR/ca.srl"

  chmod 644 "$CERT_DIR/privkey.pem"   # nginx worker 以非 root 跑，需可读
  chmod 644 "$CERT_DIR/fullchain.pem" "$CERT_DIR/ca.pem" "$CERT_DIR/leaf.pem"
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
    # 老版本签的是"单张自签叶子"，信任库装不进去（CA:FALSE），升级后自动重签
    if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ] \
       || [ ! -f "$CERT_DIR/ca.pem" ] || [ ! -f "$CERT_DIR/leaf.pem" ]; then
      gen_cert
    fi
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
    # 只查 docker ps 是不够的：bind 失败时容器状态在 Up / Restarting 之间抖，
    # 恰好落在 Up 上就会误报成功（2228 被别的实例占着时就是这个现象）。
    # 所以两条路都真打一次：明文透传 + TLS 终止。
    ok_http=0
    ok_tls=0
    i=0
    while [ "$i" -lt 20 ]; do
      curl -sf -m 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && ok_http=1
      curl -sfk -m 3 "https://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && ok_tls=1
      [ "$ok_http" = "1" ] && [ "$ok_tls" = "1" ] && break
      i=$((i + 1)); sleep 1
    done
    if [ "$ok_http" != "1" ] || [ "$ok_tls" != "1" ]; then
      echo "      ✗ 前置没通（http=$ok_http https=$ok_tls），容器日志："
      docker logs --tail 15 "$TLS_NAME" 2>&1 | sed 's/^/        /'
      ss -tlnp 2>/dev/null | grep -E ":$PORT " | sed 's/^/        端口占用: /'
      docker rm -f "$TLS_NAME" >/dev/null 2>&1 || true
      exit 1
    fi
    echo "      ✓ TLS 前置就绪：:$PORT 同时服务 http/https（后端 127.0.0.1:$BACKEND_PORT）"
    ;;

  down)
    # 光删前置是不够的：后端此时只发布在 127.0.0.1:$BACKEND_PORT，公网端口
    # 会变成没人监听 —— 看起来"拆成功"，实际全站不可达。必须把后端重新发布
    # 到公网端口，才算真的回到"纯 http 直连"的现状。
    BACKEND="${NAME:-echo-cad-toolbox}"
    release_backend() {
      docker inspect "$BACKEND" >/dev/null 2>&1 || return 0
      local img mem bcip
      img=$(docker inspect "$BACKEND" --format '{{.Config.Image}}')
      mem=$(docker inspect "$BACKEND" --format '{{.HostConfig.Memory}}')
      bcip=$(docker inspect "$BACKEND" --format \
        '{{range $p,$v := .HostConfig.PortBindings}}{{range $v}}{{.HostIp}}{{end}}{{end}}')
      [ "$bcip" = "127.0.0.1" ] || return 0          # 已经直连了，不用动
      echo "      后端当前只在回环上，重新发布到 :$PORT 上（会重启后端，约 5s）"
      memopt=""
      [ -n "$mem" ] && [ "$mem" != "0" ] && memopt="--memory $mem"
      docker rm -f "$BACKEND" >/dev/null 2>&1 || true
      # shellcheck disable=SC2086  # memopt 有意不加引号（要拆成两个参数）
      docker run -d --name "$BACKEND" --restart unless-stopped \
        -p "$PORT:8080" $memopt \
        -v "${BACKEND}-data:/app/data" --label "app=$BACKEND" \
        "$img" >/dev/null
      echo "      后端已直接监听 :$PORT（容器仍叫 $BACKEND）"
    }
    docker rm -f "$TLS_NAME" >/dev/null 2>&1 || true
    echo "      已拆除 $TLS_NAME"
    release_backend
    echo "      现在只有 http://<host>:$PORT（没有 GPU 加速，WebGPU 需要 https）"
    ;;

  renew)
    gen_cert
    exec "$0" up
    ;;

  status)
    if [ -f "$CERT_DIR/leaf.pem" ]; then
      echo "服务端证书（$CERT_DIR/leaf.pem）"
      openssl x509 -in "$CERT_DIR/leaf.pem" -noout -subject -dates -ext subjectAltName 2>/dev/null | sed 's/^/  /'
      echo "本地根证书（$CERT_DIR/ca.pem）—— 装到客户端就不再有任何警告"
      openssl x509 -in "$CERT_DIR/ca.pem" -noout -fingerprint -sha256 2>/dev/null | sed 's/^/  /'
      echo "  客户端取根证书（链上第 2 张，第 1 张是服务端叶子）："
      echo "    openssl s_client -showcerts -connect <host>:$PORT </dev/null 2>/dev/null \\"
      echo "      | awk '/BEGIN CERTIFICATE/{n++} n==2' > cad-ca.pem"
    else
      echo "  （还没有证书，跑 \"$0 up\" 生成）"
    fi
    echo "前置容器："
    docker ps -a --filter "name=$TLS_NAME" --format '  {{.Names}}  {{.Image}}  {{.Status}}' || true
    ;;

  *)
    echo "用法：$0 {up|down|renew|status}" >&2
    exit 2
    ;;
esac
