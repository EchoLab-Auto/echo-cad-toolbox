"""FastAPI 应用入口：挂载 API 与前端静态资源。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .lattice.api import router as lattice_router

STATIC_DIR = Path(__file__).parent / "static"
LATTICE_DIR = STATIC_DIR / "lattice"

app = FastAPI(
    title="echo-cad-toolbox · 摆线针轮设计与晶格化工具",
    version=__version__,
    description=(
        "两个工具合为一体：\n\n"
        "* **摆线针轮设计器**（`/`）—— 解析短幅外摆线齿廓、运动学仿真、接触分析、DXF 导出。\n"
        "* **晶格化工作台**（`/lattice`）—— STL / STEP 实体转晶格（TPMS / 桁架）结构，"
        "支持直接用摆线轮参数生成轮盘再减重晶格化，并导出 STL / 3MF / OBJ / PLY。"
    ),
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(api_router)
app.include_router(lattice_router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """摆线针轮轮廓生成与仿真器。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/lattice", include_in_schema=False)
def lattice_page() -> FileResponse:
    """晶格化工作台（STL / STEP → 晶格结构）。"""
    return FileResponse(LATTICE_DIR / "index.html")


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
