"""SVG 导出：生成可直接用于文档/报告的矢量预览图。"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from .geometry import (
    CycloidParams,
    actual_profile,
    output_layout,
    pin_centers,
    ring_radii,
    theoretical_profile,
)


def _polyline(points: np.ndarray, attrs: str) -> str:
    d = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in points) + " Z"
    return f'<path d="{d}" {attrs} />'


def _circle(cx: float, cy: float, r: float, attrs: str) -> str:
    return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" {attrs} />'


def svg_bytes(
    params: CycloidParams,
    parts: Iterable[str] = ("assembly",),
    include_theoretical: bool = True,
    label: bool = True,
) -> bytes:
    """生成装配视图 SVG。"""
    ring = ring_radii(params)
    out = output_layout(params)
    extent = ring["outer"] + 12.0
    size = 1000.0
    scale = size / (2.0 * extent)

    def P(pts: np.ndarray) -> np.ndarray:
        # 世界坐标（y 向上）→ SVG 坐标（y 向下）
        return np.column_stack((size / 2 + pts[:, 0] * scale, size / 2 - pts[:, 1] * scale))

    act = actual_profile(params)
    items: list[str] = []
    items.append(
        f'<rect x="0" y="0" width="{size}" height="{size}" fill="#0d1117" />'
    )

    # 针齿壳
    items.append(_circle(size / 2, size / 2, ring["inner"] * scale, 'fill="none" stroke="#3b6ea5" stroke-width="1.4"'))
    items.append(_circle(size / 2, size / 2, ring["outer"] * scale, 'fill="none" stroke="#3b6ea5" stroke-width="2.2"'))
    items.append(
        _circle(
            size / 2,
            size / 2,
            params.pin_circle_radius * scale,
            'fill="none" stroke="#4a5568" stroke-width="0.8" stroke-dasharray="6 5"',
        )
    )
    for center in pin_centers(params):
        c = (size / 2 + center[0] * scale, size / 2 - center[1] * scale)
        items.append(_circle(c[0], c[1], params.pin_radius * scale, 'fill="#1f3a52" stroke="#66b3ff" stroke-width="1"'))

    # 摆线轮
    items.append(_polyline(P(act), 'fill="#123a2b" fill-opacity="0.85" stroke="#39d353" stroke-width="1.6"'))
    items.append(
        _polyline(
            P(theoretical_profile(params)),
            'fill="none" stroke="#8b949e" stroke-width="0.7" stroke-dasharray="4 4"',
        )
    )
    if include_theoretical:
        pass

    # 柱销孔
    for center in out["hole_centers"]:
        c = (size / 2 + center[0] * scale, size / 2 - center[1] * scale)
        items.append(_circle(c[0], c[1], out["hole_radius"] * scale, 'fill="#0d1117" stroke="#d29922" stroke-width="1.2"'))
    items.append(_circle(size / 2, size / 2, out["center_bore_radius"] * scale, 'fill="#0d1117" stroke="#d29922" stroke-width="1.2"'))

    # 中心线
    items.append(
        f'<line x1="{size / 2 - extent * scale}" y1="{size / 2}" x2="{size / 2 + extent * scale}" y2="{size / 2}" '
        'stroke="#f85149" stroke-width="0.6" stroke-dasharray="14 4 3 4" />'
    )
    items.append(
        f'<line x1="{size / 2}" y1="{size / 2 - extent * scale}" x2="{size / 2}" y2="{size / 2 + extent * scale}" '
        'stroke="#f85149" stroke-width="0.6" stroke-dasharray="14 4 3 4" />'
    )

    if label:
        text = (
            f"Zc={params.tooth_count} Zp={params.pin_count} e={params.eccentricity:g} "
            f"Rz={params.pin_circle_radius:g} rp={params.pin_radius:g} i={params.ratio:g}"
        )
        items.append(
            f'<text x="24" y="{size - 22}" fill="#c9d1d9" font-family="monospace" font-size="16">{text}</text>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">' + "".join(items) + "</svg>"
    )
    return svg.encode("utf-8")
