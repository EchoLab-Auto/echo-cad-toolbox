# ---------------------------------------------------------------------------
# echo-cad-toolbox —— 摆线针轮轮廓设计器 + 晶格化工作台
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
      org.opencontainers.image.description="Cycloidal pin-wheel designer (profile/simulation/DXF) + lattice infill workbench (STL/STEP to lattice)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080 \
    # ezdxf 会把字体缓存写到 ~/.cache，容器里 HOME 不可写会刷警告
    XDG_CACHE_HOME=/app/data/.cache

# gmsh 的 libgmsh.so 链接了 OpenGL / X11 / freetype（只用 OCC 曲面离散、不开 GUI，
# 但动态链接阶段仍然要能找到这些 .so），这里一次装齐。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libglx-mesa0 libopengl0 \
        libx11-6 libxext6 libxrender1 libxfixes3 libxcursor1 libxinerama1 \
        libxft2 libfontconfig1 libfreetype6 libpng16-16 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /app app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# 晶格化作业的产物目录（数据卷可挂到这里；容器重启后旧作业仍留在磁盘）
RUN mkdir -p /app/data/lattice_jobs /app/data/.cache && chown -R app:app /app/data
VOLUME ["/app/data"]

COPY --chown=app:app app ./app
COPY --chown=app:app tests ./tests
COPY --chown=app:app pyproject.toml README.md ./

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
