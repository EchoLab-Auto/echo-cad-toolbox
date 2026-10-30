"""FastAPI 应用入口：挂载 API 与前端静态资源。

前端结构（三个独立页面，各自一套 CSS / JS）：

=============================  ==========================  ==================
路径                            静态文件                     内容
=============================  ==========================  ==================
``/``                          ``static/home/``             工具箱首页（两个工具的入口）
``/cycloid``                   ``static/cycloid/``          摆线针轮设计器
``/lattice``                   ``static/lattice/``          晶格化工作台
=============================  ==========================  ==================

两个工具**互不依赖**：删除其中一个的目录，另一个仍可独立工作；共享的只有
``static/base.css``（设计令牌与基础组件）和 ``static/vendor/three``（三维库）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .lattice.api import router as lattice_router

STATIC_DIR = Path(__file__).parent / "static"
HOME_DIR = STATIC_DIR / "home"
CYCLOID_DIR = STATIC_DIR / "cycloid"
LATTICE_DIR = STATIC_DIR / "lattice"

app = FastAPI(
    title="echo-cad-toolbox · 摆线针轮设计与晶格化工具",
    version=__version__,
    description=(
        "两个彼此独立的工具：\n\n"
        "* **摆线针轮设计器**（`/cycloid`）—— 解析短幅外摆线齿廓、运动学仿真、接触分析、"
        "DXF 导出（前端 `analysis.js` 本地算派生量，无需往返）。\n"
        "* **晶格化工作台**（`/lattice`）—— STL / STEP 实体转晶格（TPMS / 桁架）结构，"
        "浏览器 GPU（WebGPU）实时生成，导出 STL / 3MF / OBJ / PLY。\n"
    ),
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(api_router)
app.include_router(lattice_router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """工具箱首页：两个独立工具的入口。"""
    return FileResponse(HOME_DIR / "index.html")


@app.get("/cycloid", include_in_schema=False)
def cycloid_page() -> FileResponse:
    """摆线针轮轮廓生成与仿真器。"""
    return FileResponse(CYCLOID_DIR / "index.html")


@app.get("/lattice", include_in_schema=False)
def lattice_page() -> FileResponse:
    """晶格化工作台（STL / STEP → 晶格结构）。"""
    return FileResponse(LATTICE_DIR / "index.html")


@app.get("/index.html", include_in_schema=False)
def legacy_index() -> RedirectResponse:
    """旧链接兼容：曾把设计器挂在 ``/index.html``。"""
    return RedirectResponse("/cycloid", status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    icon = STATIC_DIR / "favicon.svg"
    return FileResponse(icon, media_type="image/svg+xml")


def main() -> None:  # pragma: no cover - 供 `python -m app.main` 直接启动
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        reload=bool(os.environ.get("RELOAD")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
