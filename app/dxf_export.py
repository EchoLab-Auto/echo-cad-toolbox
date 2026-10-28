"""DXF 导出：把摆线针轮副的几何写入可直接用于 CAM/CNC 的 DXF 文件。

图层规划
--------
============ ===========================================================
图层          内容
============ ===========================================================
PROFILE      摆线轮实际齿廓（闭合 LWPOLYLINE，加工用）
THEORY       理论齿廓（短幅外摆线，参考/校验用，虚线）
BORE         中心孔
HOLE         柱销孔
SLEEVE       柱销（输出盘）
PIN          针齿圆（针齿壳）
PIN_PITCH    针齿中心圆（参考，虚线）
RING         针齿壳内/外圆
CENTER       中心线（点划线）
TEXT         标注文字
============ ===========================================================

单位固定为毫米，比例 1:1。

零件排布
--------
* ``parts=["cycloid_disc"]``          单个零件，居中
* ``parts=["assembly"]``              装配视图（全部叠加在同一坐标系）
* ``parts=["cycloid_disc", ...]``     多个零件按 2.6×外径 间距横向排开（分件出图）
"""

from __future__ import annotations

import datetime as _dt
import io
import math
from typing import Any, Callable, Iterable, Sequence

import ezdxf
import numpy as np

from .geometry import (
    CycloidParams,
    actual_profile,
    output_layout,
    pin_centers,
    ring_radii,
    theoretical_profile,
)

#: 全部零件视图
ALL_PARTS = ("cycloid_disc", "pin_ring", "output_disc", "assembly")

_LAYERS: dict[str, dict[str, Any]] = {
    "PROFILE": {"color": 7, "lineweight": 35},
    "THEORY": {"color": 8, "linetype": "DASHED"},
    "BORE": {"color": 5, "lineweight": 25},
    "HOLE": {"color": 4, "lineweight": 25},
    "SLEEVE": {"color": 6},
    "PIN": {"color": 3, "lineweight": 30},
    "PIN_PITCH": {"color": 8, "linetype": "DASHED"},
    "RING": {"color": 2, "lineweight": 35},
    "CENTER": {"color": 1, "linetype": "CENTER"},
    "TEXT": {"color": 7},
}

Offset = tuple[float, float]


def _setup_layers(doc: "ezdxf.document.Drawing") -> None:
    for name, attrs in _LAYERS.items():
        layer = doc.layers.add(name)
        for key, value in attrs.items():
            if key == "color":
                layer.color = value
            elif key == "linetype" and value in doc.linetypes:
                layer.dxf.linetype = value
            elif key == "lineweight":
                layer.dxf.lineweight = value
        layer.dxf.plot = 1


def _circle(msp, center: Sequence[float], radius: float, layer: str, off: Offset = (0.0, 0.0)) -> None:
    if radius > 0:
        msp.add_circle(
            (float(center[0]) + off[0], float(center[1]) + off[1]),
            float(radius),
            dxfattribs={"layer": layer},
        )


def _line(msp, p1: Sequence[float], p2: Sequence[float], layer: str, off: Offset = (0.0, 0.0)) -> None:
    msp.add_line(
        (float(p1[0]) + off[0], float(p1[1]) + off[1]),
        (float(p2[0]) + off[0], float(p2[1]) + off[1]),
        dxfattribs={"layer": layer},
    )


def _polyline(msp, pts: np.ndarray, layer: str, off: Offset = (0.0, 0.0)) -> None:
    shifted = [(float(x) + off[0], float(y) + off[1]) for x, y in pts]
    msp.add_lwpolyline(shifted, close=True, dxfattribs={"layer": layer})


def _text(msp, text: str, pos: Sequence[float], off: Offset = (0.0, 0.0), height: float = 3.5) -> None:
    msp.add_text(
        text,
        height=height,
        dxfattribs={"layer": "TEXT", "insert": (float(pos[0]) + off[0], float(pos[1]) + off[1])},
    )


def _cross(msp, radius: float, off: Offset = (0.0, 0.0), overshoot: float = 4.0) -> None:
    ext = radius + overshoot
    _line(msp, (-ext, 0), (ext, 0), "CENTER", off)
    _line(msp, (0, -ext), (0, ext), "CENTER", off)


def _small_cross(msp, center: Sequence[float], radius: float, off: Offset = (0.0, 0.0)) -> None:
    c = (float(center[0]), float(center[1]))
    _line(msp, (c[0] - radius, c[1]), (c[0] + radius, c[1]), "CENTER", off)
    _line(msp, (c[0], c[1] - radius), (c[0], c[1] + radius), "CENTER", off)


# ---------------------------------------------------------------------------
# 零件绘制
# ---------------------------------------------------------------------------


def _draw_cycloid_disc(
    msp,
    params: CycloidParams,
    out: dict[str, Any],
    include_theoretical: bool,
    include_centerlines: bool,
    label: bool,
    off: Offset = (0.0, 0.0),
) -> None:
    act = actual_profile(params)
    radii = np.hypot(act[:, 0], act[:, 1])
    tip = float(radii.max())

    _polyline(msp, act, "PROFILE", off)
    if include_theoretical:
        _polyline(msp, theoretical_profile(params), "THEORY", off)

    _circle(msp, (0, 0), out["center_bore_radius"], "BORE", off)
    for center in out["hole_centers"]:
        _circle(msp, center, out["hole_radius"], "HOLE", off)

    if include_centerlines:
        _cross(msp, tip, off)
        for center in out["hole_centers"]:
            _small_cross(msp, center, out["hole_radius"] + 2.0, off)

    if label:
        y = tip + 8.0
        _text(msp, "CYCLOID DISC / 摆线轮", (-tip, y + 12.0), off, height=6.0)
        _text(
            msp,
            f"Zc={params.tooth_count}  Zp={params.pin_count}  e={params.eccentricity:g}  "
            f"Rz={params.pin_circle_radius:g}  rp={params.pin_radius:g}",
            (-tip, y),
            off,
        )
        _text(
            msp,
            f"tipR={tip:.3f}  rootR={float(radii.min()):.3f}  "
            f"drp={params.equidistant_offset:g}  dRz={params.radial_offset:g}  i={params.ratio:g}",
            (-tip, y - 6.0),
            off,
        )


def _draw_pin_ring(
    msp, params: CycloidParams, include_centerlines: bool, label: bool, off: Offset = (0.0, 0.0)
) -> None:
    ring = ring_radii(params)
    for center in pin_centers(params):
        _circle(msp, center, params.pin_radius, "PIN", off)

    _circle(msp, (0, 0), params.pin_circle_radius, "PIN_PITCH", off)
    _circle(msp, (0, 0), ring["inner"], "RING", off)
    _circle(msp, (0, 0), ring["outer"], "RING", off)
    if include_centerlines:
        _cross(msp, ring["outer"], off)

    if label:
        top = ring["outer"] + 8.0
        _text(msp, "PIN RING / 针齿壳", (-ring["outer"], top + 12.0), off, height=6.0)
        _text(
            msp,
            f"Zp={params.pin_count}  Rz={params.pin_circle_radius:g}  rp={params.pin_radius:g}  "
            f"innerR={ring['inner']:.2f}  outerR={ring['outer']:.2f}",
            (-ring["outer"], top),
            off,
        )


def _draw_output_disc(
    msp,
    params: CycloidParams,
    out: dict[str, Any],
    include_centerlines: bool,
    label: bool,
    off: Offset = (0.0, 0.0),
) -> None:
    plate = out["circle_radius"] + out["sleeve_radius"] + 4.0
    for center in out["hole_centers"]:
        _circle(msp, center, out["sleeve_radius"], "SLEEVE", off)
    _circle(msp, (0, 0), plate, "RING", off)
    _circle(msp, (0, 0), out["center_bore_radius"], "BORE", off)
    if include_centerlines:
        _cross(msp, plate, off)
        for center in out["hole_centers"]:
            _small_cross(msp, center, out["sleeve_radius"] + 2.0, off)

    if label:
        top = plate + 8.0
        _text(msp, "OUTPUT DISC / 输出盘（柱销）", (-plate, top + 12.0), off, height=6.0)
        _text(
            msp,
            f"Zw={out['count']}  Rw={out['circle_radius']:.2f}  rw={out['sleeve_radius']:.2f}  "
            f"holeR={out['hole_radius']:.2f}",
            (-plate, top),
            off,
        )


def _draw_assembly(
    msp,
    params: CycloidParams,
    out: dict[str, Any],
    include_theoretical: bool,
    include_centerlines: bool,
    label: bool,
    off: Offset = (0.0, 0.0),
) -> None:
    ring = ring_radii(params)
    _polyline(msp, actual_profile(params), "PROFILE", off)
    if include_theoretical:
        _polyline(msp, theoretical_profile(params), "THEORY", off)
    for center in pin_centers(params):
        _circle(msp, center, params.pin_radius, "PIN", off)
    _circle(msp, (0, 0), params.pin_circle_radius, "PIN_PITCH", off)
    _circle(msp, (0, 0), ring["inner"], "RING", off)
    _circle(msp, (0, 0), ring["outer"], "RING", off)
    for center in out["hole_centers"]:
        _circle(msp, center, out["hole_radius"], "HOLE", off)
        _circle(msp, center, out["sleeve_radius"], "SLEEVE", off)
    _circle(msp, (0, 0), out["center_bore_radius"], "BORE", off)
    if include_centerlines:
        _cross(msp, ring["outer"], off)
    if label:
        _text(msp, "ASSEMBLY / 装配", (-ring["outer"], ring["outer"] + 20.0), off, height=6.0)


# ---------------------------------------------------------------------------
# 文档构建
# ---------------------------------------------------------------------------


def build_document(
    params: CycloidParams,
    parts: Iterable[str] = ALL_PARTS,
    include_theoretical: bool = True,
    include_centerlines: bool = True,
    label: bool = True,
) -> "ezdxf.document.Drawing":
    """构建 DXF 文档对象（测试与自定义后处理用）。"""
    wanted = tuple(p for p in parts if p in ALL_PARTS) or tuple(ALL_PARTS)

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    _setup_layers(doc)

    msp = doc.modelspace()
    out = output_layout(params)
    ring = ring_radii(params)

    # 排布：装配视图独占原点；其余零件按间距横向排布
    spacing = 2.6 * ring["outer"]
    is_assembly = "assembly" in wanted
    drawable = [p for p in wanted if p != "assembly"]

    placements: list[tuple[str, Offset]] = []
    if is_assembly:
        placements.append(("assembly", (0.0, 0.0)))
    total = len(drawable)
    for i, part in enumerate(drawable):
        if is_assembly:
            offset = ((i - (total - 1) / 2.0) * spacing, -(2.6 * ring["outer"]))
        else:
            offset = ((i - (total - 1) / 2.0) * spacing, 0.0)
        placements.append((part, offset))

    for part, off in placements:
        if part == "cycloid_disc":
            _draw_cycloid_disc(msp, params, out, include_theoretical, include_centerlines, label, off)
        elif part == "pin_ring":
            _draw_pin_ring(msp, params, include_centerlines, label, off)
        elif part == "output_disc":
            _draw_output_disc(msp, params, out, include_centerlines, label, off)
        elif part == "assembly":
            _draw_assembly(msp, params, out, include_theoretical, include_centerlines, label, off)

    width = max(spacing * max(total - 1, 0), 2 * ring["outer"]) * 1.15
    height = (2.6 * ring["outer"] if is_assembly else 0) + 2 * ring["outer"] * 1.15
    _set_vport(doc, width, height)
    _update_extents(doc, msp)
    _stamp_metadata(doc, params)
    return doc


def _set_vport(doc: "ezdxf.document.Drawing", width: float, height: float) -> None:
    center = (0.0, 0.0)
    try:
        doc.set_modelspace_vport(height=max(width, height), center=center)
    except Exception:  # pragma: no cover
        pass


def _update_extents(doc: "ezdxf.document.Drawing", msp) -> None:
    try:
        from ezdxf import bbox

        ext = bbox.extents(msp)
        if ext.has_data:
            doc.header["$EXTMIN"] = ext.extmin
            doc.header["$EXTMAX"] = ext.extmax
    except Exception:  # pragma: no cover
        pass


def _stamp_metadata(doc: "ezdxf.document.Drawing", params: CycloidParams) -> None:
    """写入文件元信息（参数留档，便于追溯）。"""
    try:
        doc.header.custom_vars.append("PROJECT", "echo-cad-toolbox cycloidal drive")
        doc.header.custom_vars.append(
            "PARAMS",
            f"Zc={params.tooth_count} Zp={params.pin_count} e={params.eccentricity} "
            f"Rz={params.pin_circle_radius} rp={params.pin_radius} "
            f"drp={params.equidistant_offset} dRz={params.radial_offset}",
        )
        doc.header.custom_vars.append("GENERATED", _dt.datetime.now().isoformat(timespec="seconds"))
    except Exception:  # pragma: no cover
        pass


def dxf_bytes(
    params: CycloidParams,
    parts: Iterable[str] = ALL_PARTS,
    include_theoretical: bool = True,
    include_centerlines: bool = True,
    label: bool = True,
) -> bytes:
    """生成 DXF 文件内容（ASCII DXF，UTF-8）。"""
    doc = build_document(params, parts, include_theoretical, include_centerlines, label)
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def suggested_filename(params: CycloidParams, parts: Iterable[str] | None = None) -> str:
    """建议的下载文件名。"""
    wanted = [p for p in (parts or ALL_PARTS) if p in ALL_PARTS]
    if "assembly" in wanted:
        tag = "assembly"
    elif len(wanted) == 1:
        tag = wanted[0]
    else:
        tag = "parts"
    return (
        f"cycloid_{tag}_Zc{params.tooth_count}_Zp{params.pin_count}"
        f"_e{params.eccentricity:g}_Rz{params.pin_circle_radius:g}.dxf"
    )
