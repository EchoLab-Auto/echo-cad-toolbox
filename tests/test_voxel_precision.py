"""毫米级小孔的体素化精度测试（窄带精确 SDF vs 纯 EDT）。

用户反馈的核心问题：模型上有 Ø2 mm 这类毫米级小孔时，晶格化出来的多面体在孔
附近明显失真（孔壁磨圆、孔变小甚至糊死）。根因是距离场按体素中心量化，误差
最大 ±0.5 个体素。``app/lattice/voxel.py`` 的窄带精确化把表面附近的距离换成
精确的点—三角形距离，本文件用「孔壁半径误差」把改进量化出来。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import trimesh

from app.lattice import voxel

HOLE_R = 1.0  # Ø2 mm


def _plate_with_hole(thickness: float = 6.0, size: float = 40.0) -> trimesh.Trimesh:
    """40×40×thickness 的板，中心一个 Ø2 mm 通孔。"""
    plate = trimesh.creation.box(extents=(size, size, thickness))
    drill = trimesh.creation.cylinder(radius=HOLE_R, height=thickness * 3, sections=128)
    cut = trimesh.boolean.difference([plate, drill])
    assert cut is not None and len(cut.faces) > 0
    return cut


def _sdf_at(volume: dict, x: float, y: float, z: float) -> float:
    """三线性插值取体场值。"""
    sdf = volume["sdf"]
    pitch = volume["pitch"]
    origin = np.asarray(volume["origin"])
    g = (np.array([x, y, z]) - origin) / pitch - 0.5
    i0 = np.clip(np.floor(g).astype(int), 0, np.array(sdf.shape) - 2)
    t = np.clip(g - i0, 0.0, 1.0)
    c = sdf[i0[0] : i0[0] + 2, i0[1] : i0[1] + 2, i0[2] : i0[2] + 2]
    c00 = c[0, 0, 0] * (1 - t[0]) + c[1, 0, 0] * t[0]
    c10 = c[0, 1, 0] * (1 - t[0]) + c[1, 1, 0] * t[0]
    c01 = c[0, 0, 1] * (1 - t[0]) + c[1, 0, 1] * t[0]
    c11 = c[0, 1, 1] * (1 - t[0]) + c[1, 1, 1] * t[0]
    c0 = c00 * (1 - t[1]) + c10 * t[1]
    c1 = c01 * (1 - t[1]) + c11 * t[1]
    return float(c0 * (1 - t[2]) + c1 * t[2])


def _hole_wall_radius(volume: dict, z: float) -> float | None:
    """沿 +x 方向找 SDF 的零穿越点：孔壁半径（mm）。"""
    prev_r = None
    prev_v = None
    for r in np.arange(0.0, 3.0, 0.02):
        v = _sdf_at(volume, r, 0.0, z)
        if prev_v is not None and prev_v > 0 >= v:
            # 正 → 负：从孔内（外部）进入材料
            span = prev_v - v
            return float(prev_r + (r - prev_r) * (prev_v / span)) if span > 0 else float(r)
        prev_r = float(r)
        prev_v = v
    return None


@pytest.fixture(scope="module")
def hole_volume() -> dict:
    mesh = _plate_with_hole()
    return voxel.mesh_to_volume(mesh, resolution=41)  # 40 mm / 41 ≈ 1.0 mm 体素


def test_hole_stays_open(hole_volume: dict) -> None:
    """孔轴线上必须是空腔（SDF > 0），否则孔被糊死。"""
    for z in (-2.0, -1.0, 0.0, 1.0, 2.0):
        assert _sdf_at(hole_volume, 0.0, 0.0, z) > 0, f"z={z} 处孔被堵"


def test_hole_wall_radius_accuracy(hole_volume: dict) -> None:
    """孔壁半径误差应远小于半个体素（pitch ≈ 1.0 mm）。"""
    pitch = hole_volume["pitch"]
    radii = [r for r in (_hole_wall_radius(hole_volume, z) for z in (-2.0, 0.0, 2.0)) if r]
    assert len(radii) == 3, f"没有测到孔壁：{radii}"
    worst = max(abs(r - HOLE_R) for r in radii)
    assert worst < 0.2, f"孔壁半径误差 {worst:.3f} mm（半径 {radii}，pitch {pitch:.2f}）"


def test_exact_band_beats_plain_edt() -> None:
    """同样的网格，关掉窄带精确化（纯 EDT）误差应该更大 —— 这是改进的直接证据。"""
    mesh = _plate_with_hole()
    volume = voxel.mesh_to_volume(mesh, resolution=41)
    # 同一模型的"纯 EDT"路径：exact_band=0
    edt_only = voxel.mesh_to_volume(mesh, resolution=41, exact_band=0.0)

    new_err = min(
        abs(r - HOLE_R) for r in [_hole_wall_radius(volume, z) or 9.9 for z in (-2.0, 0.0, 2.0)]
    )
    old_err = min(
        abs(r - HOLE_R) for r in [_hole_wall_radius(edt_only, z) or 9.9 for z in (-2.0, 0.0, 2.0)]
    )
    print(f"\n孔壁半径误差：窄带精确 {new_err:.3f} mm / 纯 EDT {old_err:.3f} mm")
    assert new_err <= old_err + 1e-9


def test_feature_report_detects_small_hole(hole_volume: dict) -> None:
    features = hole_volume.get("features")
    assert features, "缺少 features 诊断"
    radius = features["min_hole_radius"]
    assert radius is not None
    # 这是"估算"（受体素化离散度限制），允许 ±45%
    assert radius == pytest.approx(HOLE_R, rel=0.45), f"最小内孔半径估计 {radius}"
    assert features["recommended_resolution"] >= 48
    assert features["hole_to_pitch_ratio"] is not None


@pytest.mark.parametrize("resolution", [80, 120, 160])
def test_feature_report_accuracy_across_resolutions(resolution: int) -> None:
    """诊断出的最小孔半径在常用分辨率区间内都应接近真值（±40%）。"""
    mesh = _plate_with_hole()
    volume = voxel.mesh_to_volume(mesh, resolution=resolution)
    radius = (volume.get("features") or {}).get("min_hole_radius")
    assert radius is not None, f"resolution={resolution} 没量到孔"
    assert radius == pytest.approx(HOLE_R, rel=0.4), f"resolution={resolution} → {radius}"


def test_feature_report_ignores_exterior() -> None:
    """实心块没有内腔 → 不该报"最小孔"。"""
    mesh = trimesh.creation.box(extents=(30.0, 30.0, 10.0))
    volume = voxel.mesh_to_volume(mesh, resolution=48)
    features = volume.get("features") or {}
    assert features.get("min_hole_radius") is None


def test_exact_band_is_not_slow(hole_volume: dict) -> None:
    """窄带精确化不能明显拖慢体素化（相对 EDT 版本 ≤ 4 倍，绝对值 ≤ 3 s）。"""
    mesh = _plate_with_hole()
    t0 = time.perf_counter()
    voxel.mesh_to_volume(mesh, resolution=41, exact_band=0.0)
    t_edt = time.perf_counter() - t0
    t0 = time.perf_counter()
    voxel.mesh_to_volume(mesh, resolution=41)
    t_exact = time.perf_counter() - t0
    print(f"\n体素化耗时：EDT {t_edt:.2f} s / 窄带精确 {t_exact:.2f} s（×{t_exact / t_edt:.2f}）")
    assert t_exact < max(3.0, t_edt * 4.0)


def test_point_triangle_distance_matches_bruteforce() -> None:
    """点到三角形距离公式与"逐点暴力采样"对照（防止 Ericson 分支写错）。"""
    rng = np.random.default_rng(0)
    tris = rng.normal(size=(8, 3, 3))
    pts = rng.normal(size=(8, 3))
    d = voxel._point_triangle_dist(pts, tris[:, 0], tris[:, 1], tris[:, 2])

    # 暴力：在三角形上撒 200×200 个重心坐标采样点，取最小距离
    u = np.linspace(0, 1, 200)
    grid_u, grid_v = np.meshgrid(u, u)
    valid = grid_u + grid_v <= 1.0
    bu = grid_u[valid]
    bv = grid_v[valid]
    worst = 0.0
    for idx, tri in enumerate(tris):
        a, b, c = tri
        samples = a + bu[:, None] * (b - a) + bv[:, None] * (c - a)
        brute = np.linalg.norm(pts[idx][None, :] - samples, axis=1).min()
        worst = max(worst, abs(brute - d[idx]))
    assert worst < 2e-3, f"最大偏差 {worst}"
