"""HTTP API：几何分析、接触表、DXF/SVG 导出。

所有长度单位为毫米，角度输入单位为度。
"""

from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import __version__
from .dxf_export import ALL_PARTS, dxf_bytes, suggested_filename
from .geometry import CycloidParams, analyze, contact_table
from .svg_export import svg_bytes

router = APIRouter(prefix="/api", tags=["cycloid"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ParamsIn(BaseModel):
    """摆线针轮副参数（全部长度单位 mm）。"""

    tooth_count: int = Field(39, ge=6, le=200, description="摆线轮齿数 Zc（针齿数 Zp=Zc+1）")
    eccentricity: float = Field(1.43, gt=0, le=200, description="偏心距 e")
    pin_circle_radius: float = Field(88.0, gt=0, le=10000, description="针齿中心圆半径 Rz")
    pin_radius: float = Field(5.5, gt=0, le=1000, description="针齿（套）半径 rp")

    equidistant_offset: float = Field(0.02, ge=-2.0, le=5.0, description="等距修形 Δrp（正值加大侧隙）")
    radial_offset: float = Field(0.0, ge=-2.0, le=5.0, description="移距修形 ΔRz（正值加大侧隙）")
    rotary_offset_deg: float = Field(0.0, ge=-5.0, le=5.0, description="转角修形（度）")

    ring_wall: float = Field(12.0, ge=1.0, le=1000.0, description="针齿壳壁厚")

    pin_hole_count: int = Field(0, ge=0, le=64, description="柱销数，0=自动")
    pin_hole_circle_radius: float = Field(0.0, ge=0.0, le=10000.0, description="柱销分布圆半径，0=自动")
    pin_sleeve_radius: float = Field(0.0, ge=0.0, le=1000.0, description="柱销套半径，0=自动")
    clearance: float = Field(0.0, ge=0.0, le=5.0, description="柱销孔单边间隙")
    center_bore_radius: float = Field(0.0, ge=0.0, le=10000.0, description="中心孔半径，0=自动")

    samples_per_tooth: int = Field(64, ge=8, le=512, description="每齿齿廓采样点数")

    def to_params(self) -> CycloidParams:
        return CycloidParams(**self.model_dump())


class AnalyzeRequest(BaseModel):
    params: ParamsIn = Field(default_factory=ParamsIn)
    contact_steps: int = Field(72, ge=0, le=1440, description="接触表采样步数，0 表示不返回")


class ExportRequest(BaseModel):
    params: ParamsIn = Field(default_factory=ParamsIn)
    parts: list[str] = Field(default_factory=lambda: list(ALL_PARTS), description="要导出的零件视图")
    include_theoretical: bool = Field(True, description="是否绘制理论齿廓（参考层）")
    include_centerlines: bool = Field(True, description="是否绘制中心线")
    label: bool = Field(True, description="是否绘制标题与参数文字")


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------

PRESETS: list[dict[str, Any]] = [
    {
        "id": "rv40",
        "name": "RV-40 量级 · 39/40",
        "note": "通用中等规格，K1≈0.65",
        "params": {"tooth_count": 39, "eccentricity": 1.43, "pin_circle_radius": 88.0, "pin_radius": 5.5},
    },
    {
        "id": "compact",
        "name": "紧凑型 · 11/12",
        "note": "小减速比 11:1，结构紧凑",
        "params": {"tooth_count": 11, "eccentricity": 1.86, "pin_circle_radius": 42.0, "pin_radius": 4.5},
    },
    {
        "id": "small",
        "name": "小型 · 19/20",
        "note": "小型关节模组常用",
        "params": {"tooth_count": 19, "eccentricity": 1.6, "pin_circle_radius": 50.0, "pin_radius": 3.2},
    },
    {
        "id": "high_ratio",
        "name": "大速比 · 59/60",
        "note": "59:1 大速比，齿数多、齿高小",
        "params": {"tooth_count": 59, "eccentricity": 1.4, "pin_circle_radius": 120.0, "pin_radius": 6.0},
    },
    {
        "id": "thin",
        "name": "细针齿 · 39/40 · 轻载",
        "note": "针齿更细，齿廓更接近理论摆线",
        "params": {
            "tooth_count": 39,
            "eccentricity": 1.6,
            "pin_circle_radius": 88.0,
            "pin_radius": 3.5,
            "equidistant_offset": 0.035,
        },
    },
]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "service": "echo-cad-toolbox"}


@router.get("/presets", summary="参数预设")
def get_presets() -> dict[str, Any]:
    return {"presets": PRESETS}


@router.post("/analyze", summary="几何分析（轮廓 + 派生量 + 告警 + 接触表）")
def post_analyze(req: AnalyzeRequest) -> dict[str, Any]:
    params = req.params.to_params()
    try:
        result = analyze(params)
    except Exception as exc:  # 参数非法时不返回 500
        raise HTTPException(status_code=400, detail=f"几何计算失败：{exc}") from exc

    if req.contact_steps:
        result["contacts"] = contact_table(params, req.contact_steps)

    _round_points(result)
    result.pop("tau", None)
    result["profile"].pop("tau", None)
    return result


@router.get("/export/dxf", summary="导出 DXF（查询参数版，便于 wget/curl 直接下载）")
def get_dxf(
    params: Annotated[ParamsIn, Query()],
    parts: Annotated[list[str] | None, Query(description="零件：cycloid_disc/pin_ring/output_disc/assembly")] = None,
    include_theoretical: bool = True,
    include_centerlines: bool = True,
) -> Response:
    return _dxf_response(
        ExportRequest(
            params=params,
            parts=parts or list(ALL_PARTS),
            include_theoretical=include_theoretical,
            include_centerlines=include_centerlines,
        )
    )


@router.post("/export/dxf", summary="导出 DXF")
def post_dxf(req: ExportRequest) -> Response:
    return _dxf_response(req)


@router.post("/export/svg", summary="导出 SVG（矢量预览图）")
def post_svg(req: ExportRequest) -> Response:
    params = req.params.to_params()
    try:
        data = svg_bytes(params, req.parts, req.include_theoretical, req.label)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SVG 生成失败：{exc}") from exc
    return Response(
        content=data,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{_svg_name(params)}"'},
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _dxf_response(req: ExportRequest) -> Response:
    params = req.params.to_params()
    parts = [p for p in req.parts if p in ALL_PARTS] or list(ALL_PARTS)
    try:
        data = dxf_bytes(params, parts, req.include_theoretical, req.include_centerlines, req.label)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"DXF 生成失败：{exc}") from exc

    import urllib.parse

    filename = suggested_filename(params, parts)
    quoted = urllib.parse.quote(filename)
    return Response(
        content=data,
        media_type="application/dxf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}",
            "X-Filename": quoted,
        },
    )


def _svg_name(params: CycloidParams) -> str:
    return f"cycloid_Zc{params.tooth_count}_e{params.eccentricity:g}.svg"


def _round_points(result: dict[str, Any], ndigits: int = 4) -> None:
    """压缩浮点位数，减小 JSON 体积（0.1 µm 精度足够渲染）。"""
    for key in ("actual", "theoretical"):
        pts = result["profile"].get(key)
        if pts:
            result["profile"][key] = [[round(x, ndigits), round(y, ndigits)] for x, y in pts]
    pins = result["pin_ring"].get("pin_centers")
    if pins:
        result["pin_ring"]["pin_centers"] = [[round(x, ndigits), round(y, ndigits)] for x, y in pins]
    holes = result["output"].get("hole_centers")
    if holes:
        result["output"]["hole_centers"] = [[round(x, ndigits), round(y, ndigits)] for x, y in holes]
    contacts = result.get("contacts")
    if contacts:
        contacts["center"] = [[round(x, ndigits), round(y, ndigits)] for x, y in contacts["center"]]
