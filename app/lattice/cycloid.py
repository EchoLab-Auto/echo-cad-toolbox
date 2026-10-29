"""从摆线针轮参数直接生成 3D 摆线轮盘实体。

这是晶格化工具与摆线齿轮设计器的衔接点：设计器算出的 2D 齿廓
（:func:`app.geometry.actual_profile`）在这里被拉伸成带孔的圆柱盘，
直接作为晶格化的输入模型，省掉"导出 DXF → 建模 → 导出 STL"的绕行。

生成的实体包含：

* 外轮廓 = 摆线齿廓（含修形，与实际加工的齿廓一致）
* 柱销孔   = :func:`app.geometry.output_layout` 给出的均布通孔
* 中心孔   = 中心通孔

单位 mm，Z 轴为盘厚度方向，盘体关于 ``z = 0`` 对称（便于和摆线轮系对齐）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import trimesh
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from ..geometry import CycloidParams, actual_profile, output_layout

#: 盘厚默认值（mm）。摆线轮盘常见的宽径比约 0.1~0.25，39/40 规格常取 12~20。
DEFAULT_DISC_WIDTH = 14.0

MIN_WIDTH = 1.0
MAX_WIDTH = 120.0


class CycloidSolidError(RuntimeError):
    """无法由参数构建实体（几何退化等）。"""


def disc_polygon(params: CycloidParams, samples: int | None = None) -> tuple[Polygon, dict[str, Any]]:
    """摆线轮盘的截面多边形（外齿廓减去柱销孔与中心孔）。"""
    profile = actual_profile(params, samples=samples)
    if profile is None or len(profile) < 3:
        raise CycloidSolidError("齿廓采样点不足，无法成面")

    outer = Polygon(profile)
    if not outer.is_valid:
        # 齿廓是解析采样点，偶有自交；buffer(0) 做一次拓扑修复
        outer = outer.buffer(0)
    if outer.is_empty or outer.area <= 0:
        raise CycloidSolidError("齿廓退化（面积为零），请检查 K1 = e·Zp/Rz 是否过大")
    if outer.geom_type == "MultiPolygon":
        outer = max(outer.geoms, key=lambda g: g.area)

    layout = output_layout(params)
    cutters = []

    hole_radius = float(layout["hole_radius"])
    if hole_radius > 0:
        for cx, cy in np.asarray(layout["hole_centers"]):
            cutters.append(Point(float(cx), float(cy)).buffer(hole_radius, quad_segs=32))

    bore = float(layout["center_bore_radius"])
    if bore > 0:
        cutters.append(Point(0.0, 0.0).buffer(bore, quad_segs=64))

    section = outer
    if cutters:
        section = outer.difference(unary_union(cutters))

    if section.is_empty:
        raise CycloidSolidError("齿廓被孔完全切除，请减小柱销孔/中心孔尺寸")
    if section.geom_type == "MultiPolygon":
        # 保留主体（面积最大的环），其余是数值碎片
        section = max(section.geoms, key=lambda g: g.area)

    info = {
        "outer_area": float(outer.area),
        "section_area": float(section.area),
        "hole_count": len(layout["hole_centers"]),
        "hole_radius": hole_radius,
        "center_bore_radius": bore,
        "root_radius": float(layout["root_radius"]),
        "tip_radius": float(layout["tip_radius"]),
    }
    return section, info


def build_disc_mesh(
    params: CycloidParams,
    width: float = DEFAULT_DISC_WIDTH,
    samples: int | None = None,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """把摆线轮参数变成可晶格化的 3D 实体。

    Parameters
    ----------
    params:
        摆线针轮副参数。
    width:
        盘厚（mm），沿 Z 轴。
    samples:
        齿廓采样点数，``None`` 用参数里的 ``samples_per_tooth``。

    Returns
    -------
    (mesh, info)
        ``mesh`` 是闭合实体（Z 方向关于 0 对称），``info`` 含齿廓/孔位统计。
    """
    width = float(np.clip(width, MIN_WIDTH, MAX_WIDTH))
    section, info = disc_polygon(params, samples=samples)

    try:
        mesh = trimesh.creation.extrude_polygon(section, height=width)
    except Exception as exc:
        raise CycloidSolidError(f"截面拉伸失败：{exc}") from exc

    if mesh is None or len(mesh.faces) == 0:
        raise CycloidSolidError("拉伸结果为空")
    if not mesh.is_watertight:
        # 拉伸出的实体偶尔需要一次法向/拓扑整理才算闭合
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_normals(mesh)
    if not mesh.is_watertight:
        raise CycloidSolidError("拉伸结果不闭合，请调整参数后重试")

    # Z 方向关于 0 对称，方便和摆线轮的实际传动位置对应
    mesh.apply_translation((0.0, 0.0, -width / 2.0))

    # 让网格的法向朝外且体积为正
    if mesh.volume < 0:
        mesh.invert()

    info.update(
        width=width,
        volume=float(mesh.volume),
        faces=len(mesh.faces),
        watertight=bool(mesh.is_watertight),
        tooth_count=int(params.tooth_count),
        pin_count=int(params.pin_count),
        eccentricity=float(params.eccentricity),
        pin_circle_radius=float(params.pin_circle_radius),
        pin_radius=float(params.pin_radius),
    )
    return mesh, info


def describe(params: CycloidParams, width: float, info: dict[str, Any]) -> dict[str, Any]:
    """给前端用的摘要（含质量估算，钢件取 7.85 g/cm³）。"""
    volume_mm3 = float(info.get("volume", 0.0))
    info = dict(info)
    info["volume_mm3"] = volume_mm3
    info["mass_kg_steel"] = volume_mm3 * 7.85e-6
    info["ratio"] = float(params.ratio)
    return info
