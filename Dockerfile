# ---------------------------------------------------------------------------
# echo-cad-toolbox —— 摆线针轮轮廓生成与仿真器
#
# 多阶段构建：先在 builder 中安装依赖并预编译，再把纯净的运行环境复制到
# 最终镜像，镜像体积小、无编译工具链。
# ---------------------------------------------------------------------------
# 允许替换基础镜像来源（如国内加速：docker.m.daocloud.io/library/python:3.12-slim）
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# 允许构建时指定 PyPI 镜像（国内网络环境更快）
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="echo-cad-toolbox" \
      org.opencontainers.image.description="Cycloidal pin-wheel profile generator, simulator and DXF exporter" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080

# 非 root 运行
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /app app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app tests ./tests
COPY --chown=app:app pyproject.toml README.md ./

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
