#!/bin/sh
# ---------------------------------------------------------------------------
# 服务器部署脚本（纯 docker，无需 docker compose）
#
# 用法：
#   ./tools/deploy.sh                 # 默认端口 8080
#   ./tools/deploy.sh 2228            # 指定宿主机端口
#   PORT=2228 ./tools/deploy.sh       # 同上
#
# 环境变量：
#   PORT          宿主机端口（默认 8080）
#   IMAGE         镜像标签（默认 echo-cad-toolbox:1.2.0）
#   NAME          容器名（默认 echo-cad-toolbox）
#   PIP_INDEX_URL PyPI 镜像，**必须**通过 --build-arg 传递（Dockerfile 里是 ARG）
#   PYTHON_IMAGE  基础镜像来源（拉不到官方镜像时用加速源）
#   APT_MIRROR    Debian 软件源镜像（默认官方；国内建议 mirrors.cloud.tencent.com，
#                 否则拉 mesa 的 libllvm19 极慢，构建会卡十几分钟）
#   MEMORY        容器内存上限（默认取机器物理内存的一半，上限 2g）。
#                 晶格化的峰值随体素分辨率上涨：160→~550MB、200→~830MB。
#                 服务端会按这个上限自动裁剪可用分辨率，不会把容器 OOM 掉。
#   PULL          设为 1 时先 git 同步到 origin/main
#   TLS           设为 1 时在 PORT 上加一层 TLS 前置：同一个端口同时服务
#                 http:// 和 https://（详见 tools/tls.sh）。后端会挪到
#                 127.0.0.1:BACKEND_PORT，不再直接暴露。
#                 **公网访问要用 WebGPU 就必须开**：navigator.gpu 是
#                 [SecureContext] 接口，http:// 的公网地址拿不到它。
#   BACKEND_PORT  开 TLS 时的后端回环端口（默认 18080）
#   CERT_DIR      证书目录（默认 /etc/ssl/cad，不存在会自动签一张自签证书）
#   CERT_HOSTS    额外 SAN（如 cad.example.com），有域名时填上
#
# 例（腾讯云内网加速）：
#   PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
#   PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
#   APT_MIRROR=mirrors.cloud.tencent.com \
#   ./tools/deploy.sh 2228
# ---------------------------------------------------------------------------
set -e

cd "$(dirname "$0")/.."

# 同步源码（必须在**读取任何默认值之前**做）：PULL=1 会把脚本自身换成新版本，
# 若先求值 IMAGE 再 reset，新版本代码会顶着旧 tag 构建（"鸡生蛋"问题）。
# 同步完 exec 一次自己，让新脚本参与真正的部署流程。
if [ "${PULL:-0}" = "1" ] && [ -z "${_DEPLOY_RESYNCED:-}" ]; then
  echo "[0/$([ "${TLS:-0}" = "1" ] && echo 5 || echo 4)] 同步源码"
  git fetch --depth 1 origin main
  git reset --hard origin/main
  git log --oneline -1
  _DEPLOY_RESYNCED=1 exec env _DEPLOY_RESYNCED=1 PULL=0 "$0" "$@"
fi

PORT="${1:-${PORT:-8080}}"
IMAGE="${IMAGE:-echo-cad-toolbox:1.2.0}"
NAME="${NAME:-echo-cad-toolbox}"
TLS="${TLS:-0}"
BACKEND_PORT="${BACKEND_PORT:-18080}"
if [ "$TLS" = "1" ]; then
  STEPS=5
  PUBLISH="127.0.0.1:${BACKEND_PORT}:8080"     # 只绑回环，公网靠前置
  HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/api/health"
else
  STEPS=4
  PUBLISH="${PORT}:8080"
  HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
fi
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"
APT_MIRROR="${APT_MIRROR:-}"
if [ -z "${MEMORY:-}" ]; then
  # 默认给物理内存的一半（最多 2g）：1.9G 的小机器 → 约 1g，32G 的工作站 → 2g
  HALF_MB=$(( $(awk '/^MemTotal:/{print int($2/1024/2)}' /proc/meminfo) ))
  [ "$HALF_MB" -gt 2048 ] && HALF_MB=2048
  MEMORY="${HALF_MB}m"
fi

echo "[1/$STEPS] 构建镜像 $IMAGE"
# 注意：PIP_INDEX_URL 必须在 Dockerfile 里声明为 ARG，只有 --build-arg 生效；
# 仅 export 环境变量对 docker build 无效（会退回 Dockerfile 默认的 pypi.org，极慢）。
docker build \
  --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
  --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE" \
  --build-arg "APT_MIRROR=$APT_MIRROR" \
  -t "$IMAGE" .

echo "[2/$STEPS] 替换容器 $NAME（容器 :8080）"
docker rm -f "$NAME" >/dev/null 2>&1 || true
# 晶格化作业产物放在 data 卷里，容器重建不丢（纯设计器用法可省略）
docker volume create "${NAME}-data" >/dev/null 2>&1 || true

docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -p "$PUBLISH" \
  --memory "$MEMORY" \
  -v "${NAME}-data:/app/data" \
  --label "app=$NAME" \
  "$IMAGE" >/dev/null
echo "      容器已启动：$(docker inspect -f '{{.Id}}' "$NAME" | cut -c1-12)"

echo "[3/$STEPS] 等待健康检查"
i=0
until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "      ✗ 健康检查超时，最近日志："
    docker logs --tail 30 "$NAME" || true
    exit 1
  fi
  sleep 2
done
echo "      ✓ $(curl -s "$HEALTH_URL")"

if [ "$TLS" = "1" ]; then
  echo "[4/$STEPS] TLS 前置（:$PORT 同时服务 http/https）"
  # 后端已经只听 127.0.0.1:$BACKEND_PORT，前置容器用 host 网络够到它并绑公网端口。
  # 证书不存在时 tls.sh 会自动签一张自签证书（SAN 含本机公网 IP）。
  PORT="$PORT" BACKEND_PORT="$BACKEND_PORT" NAME="$NAME" \
    sh tools/tls.sh up
fi

echo "[$STEPS/$STEPS] 结果"
docker ps --filter "name=$NAME" --format '      {{.Names}}  {{.Status}}  {{.Ports}}'

# 打印可访问地址（尽力而为，失败不影响部署结果）
if command -v hostname >/dev/null 2>&1 && [ "$(uname -s)" = "Linux" ]; then
  echo "      可访问地址："
  ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 \
    | grep -vE '^(127\.|172\.1[7-9]\.|172\.2[0-9]\.|172\.3[01]\.)' \
    | while read -r ip; do
        echo "        http://$ip:$PORT/"
        [ "$TLS" = "1" ] && echo "        https://$ip:$PORT/   ← 用这个才有 WebGPU（自签证书，首次需点『继续前往』）"
      done
fi
