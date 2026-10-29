"""���格化工具的 HTTP 接口（挂载在 ``/api/lattice`` 下）。

设计上沿用工具箱既有约定：所有长度单位为毫米、作业式异步生成、错误用
``HTTPException(detail=...)`` 返回中文提示。

作业状态机：``new → preparing（体素化）→ ready → running（生成）→ done | error``。
"""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..geometry import CycloidParams
from . import meshio, pipeline
from .cycloid import DEFAULT_DISC_WIDTH, CycloidSolidError, build_disc_mesh, describe
from .jobs import JobError, JobStore, sample_meshes

router = APIRouter(prefix="/api/lattice", tags=["lattice"])

store = JobStore(workers=2)

mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/stl", ".stl")

_DOWNLOAD_MIME = {
    "glb": "model/gltf-binary",
    "stl": "model/stl",
    "obj": "text/plain",
    "3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    "ply": "application/octet-stream",
}


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
@router.get("/health", summary="晶格化工具健康检查")
def health() -> dict[str, Any]:
    return {"status": "ok", "tool": "lattice"}


@router.get("/config", summary="晶格类型、应用方式、参数区间、支持格式")
def config() -> dict[str, Any]:
    return {
        "lattices": pipeline.KIND_INFO,
        "modes": pipeline.MODES,
        "defaults": pipeline.DEFAULT_PARAMS,
        "limits": pipeline.PARAM_LIMITS,
        "export_formats": sorted(meshio.EXPORT_FORMATS),
        "upload_formats": sorted(meshio.SUPPORTED_EXTS),
        "samples": list(sample_meshes()),
        "cycloid": {
            "default_width": DEFAULT_DISC_WIDTH,
            "min_width": 1.0,
            "max_width": 120.0,
        },
    }


# ---------------------------------------------------------------------------
# 作业
# ---------------------------------------------------------------------------
@router.get("/jobs", summary="作业列表")
def list_jobs() -> dict[str, Any]:
    store.gc()
    return {"jobs": store.list_jobs()}


@router.post("/jobs", summary="上传 STL / STEP 等模型建立作业")
async def upload_job(
    file: UploadFile = File(...),
    max_edge: float | None = Form(default=None),
) -> JSONResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        job = store.create_from_bytes(data, file.filename or "model.stl", max_edge=max_edge)
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(job.snapshot())


@router.post("/jobs/from-cycloid", summary="用摆线轮参数直接生成 3D 盘并建立作业")
def job_from_cycloid(payload: dict[str, Any] = Body(default={})) -> JSONResponse:
    raw = payload.get("params") or payload
    params = CycloidParams.from_mapping(raw)
    width = float(payload.get("width", DEFAULT_DISC_WIDTH) or DEFAULT_DISC_WIDTH)
    try:
        mesh, info = build_disc_mesh(params, width=width)
    except CycloidSolidError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"摆线轮实体生成失败：{exc}") from exc

    name = f"cycloid_Zc{params.tooth_count}_e{params.eccentricity:g}.stl"
    try:
        job = store.create_from_mesh(mesh, name)
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.info["cycloid"] = describe(params, width, info)
    job.save_meta()
    snapshot = job.snapshot()
    snapshot["info"] = job.info
    return JSONResponse(snapshot)


@router.post("/samples/{name}", summary="用内置样例建立作业")
def create_sample(name: str) -> JSONResponse:
    makers = sample_meshes()
    if name not in makers:
        raise HTTPException(status_code=404, detail=f"没有样例 {name}")
    try:
        job = store.create_from_mesh(makers[name](), f"{name}.stl")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(job.snapshot())


@router.get("/jobs/{job_id}", summary="作业状态")
def job_state(job_id: str) -> dict[str, Any]:
    try:
        return store.get(job_id).snapshot()
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/generate", summary="按参数生成晶格")
def generate(job_id: str, params: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return store.generate(job_id, params)
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/jobs/{job_id}", summary="删除作业")
def delete_job(job_id: str) -> dict[str, Any]:
    store.delete(job_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}/preview/{kind}", summary="GLB 预览（original | lattice）")
def preview(job_id: str, kind: str, v: str | None = None):
    """返回 GLB 预览。

    ``v`` 是前端用来穿透浏览器缓存的版本标记（形如 ``3-1793264757956``），
    这里只做透传、不参与任何判断，所以声明成 ``str``：一旦声明为 ``int``，
    FastAPI 会对这类复合值返回 422，直接表现为前端「预览加载失败」。
    """
    try:
        job = store.get(job_id)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if kind == "original":
        path = job.original_glb
    elif kind == "lattice":
        path = job.lattice_glb
    else:
        raise HTTPException(status_code=404, detail="未知预览类型")

    if not path.exists():
        raise HTTPException(status_code=404, detail="预览尚未生成")
    return FileResponse(
        str(path),
        media_type="model/gltf-binary",
        headers={"Cache-Control": "no-store", "X-Lattice-Version": str(job.version)},
    )


@router.get("/jobs/{job_id}/download/{fmt}", summary="下载 stl / 3mf / obj / ply")
def download(job_id: str, fmt: str):
    try:
        path, filename = store.export_path(job_id, fmt)
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        str(path),
        media_type=_DOWNLOAD_MIME.get(fmt.lower(), "application/octet-stream"),
        filename=filename,
    )
