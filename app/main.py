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

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="echo-cad-toolbox · 摆线针轮轮廓生成与仿真器",
    version=__version__,
    description=(
        "交互式生成摆线针轮（RV/摆线减速器）齿廓：解析短幅外摆线等距曲线、"
        "运动学仿真、接触分析，并导出毫米级 DXF 图纸。"
    ),
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(api_router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
