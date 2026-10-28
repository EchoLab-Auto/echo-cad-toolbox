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
#   IMAGE         镜像标签（默认 echo-cad-toolbox:1.0.0）
#   NAME          容器名（默认 echo-cad-toolbox）
#   PIP_INDEX_URL PyPI 镜像，**必须**通过 --build-arg 传递（Dockerfile 里是 ARG）
#   PYTHON_IMAGE  基础镜像来源（拉不到官方镜像时用加速源）
#   MEMORY        内存上限（默认 512m）
#   PULL          设为 1 时先 git 同步到 origin/main
#
# 例（腾讯云内网加速）：
#   PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple ./tools/deploy.sh 2228
# ---------------------------------------------------------------------------
set -e

cd "$(dirname "$0")/.."

PORT="${1:-${PORT:-8080}}"
IMAGE="${IMAGE:-echo-cad-toolbox:1.0.0}"
NAME="${NAME:-echo-cad-toolbox}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"
MEMORY="${MEMORY:-512m}"

if [ "${PULL:-0}" = "1" ]; then
  echo "[0/4] 同步源码"
  git fetch --depth 1 origin main
  git reset --hard origin/main
  git log --oneline -1
fi

echo "[1/4] 构建镜像 $IMAGE"
# 注意：PIP_INDEX_URL 必须在 Dockerfile 里声明为 ARG，只有 --build-arg 生效；
# 仅 export 环境变量对 docker build 无效（会退回 Dockerfile 默认的 pypi.org，极慢）。
docker build \
  --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
  --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE" \
  -t "$IMAGE" .

echo "[2/4] 替换容器 $NAME（宿主机 :$PORT -> 容器 :8080）"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -p "$PORT:8080" \
  --memory "$MEMORY" \
  --label "app=$NAME" \
  "$IMAGE" >/dev/null
echo "      容器已启动：$(docker inspect -f '{{.Id}}' "$NAME" | cut -c1-12)"

echo "[3/4] 等待健康检查"
i=0
until curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "      ✗ 健康检查超时，最近日志："
    docker logs --tail 30 "$NAME" || true
    exit 1
  fi
  sleep 2
done
echo "      ✓ $(curl -s "http://127.0.0.1:$PORT/api/health")"

echo "[4/4] 结果"
docker ps --filter "name=$NAME" --format '      {{.Names}}  {{.Status}}  {{.Ports}}'

# 打印可访问地址（尽力而为，失败不影响部署结果）
if command -v hostname >/dev/null 2>&1 && [ "$(uname -s)" = "Linux" ]; then
  echo "      可访问地址："
  ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 \
    | grep -vE '^(127\.|172\.1[7-9]\.|172\.2[0-9]\.|172\.3[01]\.)' \
    | while read -r ip; do echo "        http://$ip:$PORT/"; done
fi
