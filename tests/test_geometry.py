"""几何核心的数值验证。

重点验证「齿廓与针齿的真实啮合关系」——这是摆线轮设计中最容易搞错的地方：

1. 把摆线轮齿廓按运动学变换到世界系后，齿廓上**任意**一点到最近针齿
   中心的距离必须 ≥ 针齿半径（不穿透），且在接触点处**恰好**等于针齿半径。
2. 接触律 ``τ_i = ψ_i + φ/Zc`` 给出的点必须是真正的接触点。
3. 齿廓沿一圈的径向极值个数必须等于齿数 Zc。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.geometry import (
    TAU,
    CycloidParams,
    actual_profile,
    analyze,
    contact_points,
    disc_to_world,
    pin_centers,
    theoretical_profile,
    world_to_disc,
)

#: 无修形参数：用于精确的几何/啮合验证（修形会引入设计侧隙，另测）
PARAMS = CycloidParams(equidistant_offset=0.0, radial_offset=0.0)
#: 默认参数（含 0.02mm 等距修形）
MOD = CycloidParams()


@pytest.fixture(scope="module")
def profile() -> np.ndarray:
    return actual_profile(PARAMS, samples=4000)


def test_pin_count_relation() -> None:
    assert PARAMS.pin_count == PARAMS.tooth_count + 1 == 40
    assert 0.0 < PARAMS.short_amplitude_coefficient < 1.0


def test_profile_is_closed_and_finite(profile: np.ndarray) -> None:
    assert profile.shape == (4000, 2)
    assert np.all(np.isfinite(profile))
    # 首尾点的间隔应与普通采样步长同量级（闭合曲线）
    step = np.hypot(*(profile[-1] - profile[0]))
    typical = np.hypot(*(profile[500] - profile[501]))
    assert step < 3.0 * typical + 1e-9


def test_tooth_count_matches_radial_extrema(profile: np.ndarray) -> None:
    """齿廓一圈的径向极大值个数 == 齿数 Zc。"""
    radii = np.hypot(profile[:, 0], profile[:, 1])
    n = radii.size
    peaks = 0
    for i in range(n):
        if radii[i] > radii[i - 1] and radii[i] >= radii[(i + 1) % n]:
            peaks += 1
    assert peaks == PARAMS.tooth_count, f"expected {PARAMS.tooth_count} teeth, got {peaks}"


def test_envelope_contact_no_penetration() -> None:
    """核心验证：齿廓始终贴合针齿外侧（距离 ≥ rp，接触处 == rp）。

    用高密度参数采样避免离散化误差；精确接触性另见
    :func:`test_contact_law_matches_pin_positions`。
    """
    dense = actual_profile(PARAMS, samples=12000, arc_uniform=False)
    pins = pin_centers(PARAMS)
    rp = PARAMS.pin_radius
    min_gap = math.inf
    contact_hits = 0
    tested = 0

    for k in range(8):
        phi = TAU * k / 8.0
        world = disc_to_world(PARAMS, dense, phi)
        nearest = np.linalg.norm(world[:, None, :] - pins[None, :, :], axis=2).min(axis=1)
        min_gap = min(min_gap, float(nearest.min()))
        contact_hits += int(np.sum(np.abs(nearest - rp) < 1e-3))
        tested += nearest.size

    assert min_gap >= rp - 1e-9, f"穿透针齿：最小距离 {min_gap} < rp {rp}"
    assert min_gap - rp < 2e-3, f"应存在精确接触点，实测最小距离 {min_gap}"
    assert contact_hits > 0.005 * tested, "接触点比例过低，接触律可能有误"


def test_contact_law_matches_pin_positions() -> None:
    """接触律：接触点在摆线轮系中与针齿中心的距离恒为 rp。"""
    rp = PARAMS.pin_radius
    for phi in (0.0, 0.37, 1.91, 5.0):
        contacts = contact_points(PARAMS, phi)
        world_pin = disc_to_world(PARAMS, contacts, phi)
        d = np.linalg.norm(world_pin - pin_centers(PARAMS), axis=1)
        # 每个针齿的接触点都应该落在该针齿的圆周上（等距曲线的定义）
        assert np.allclose(d, rp, atol=1e-6), f"phi={phi}: max err {np.abs(d - rp).max()}"


def test_theoretical_profile_hits_pin_centers() -> None:
    """理论齿廓 P 即针齿中心的轨迹：P(ψ_i + φ/Zc) 变换后与针齿中心重合。"""
    phi = 0.83
    pins = pin_centers(PARAMS)
    for i in (0, 7, 23, 39):
        tau = TAU * i / PARAMS.pin_count + phi / PARAMS.tooth_count
        theo = theoretical_profile(PARAMS, samples=None)
        # 直接按解析式计算 P(tau)
        rz = PARAMS.effective_pin_circle_radius
        e = PARAMS.eccentricity
        zp = PARAMS.pin_count
        p = np.array([[rz * math.cos(tau) - e * math.cos(zp * tau),
                       rz * math.sin(tau) - e * math.sin(zp * tau)]])
        world = disc_to_world(PARAMS, p, phi)
        assert np.linalg.norm(world[0] - pins[i]) < 1e-9
    assert theo.shape[1] == 2


def test_world_disc_roundtrip() -> None:
    phi = 2.2
    pts = np.random.default_rng(7).uniform(-50, 50, size=(32, 2))
    back = world_to_disc(PARAMS, disc_to_world(PARAMS, pts, phi), phi)
    assert np.allclose(back, pts, atol=1e-9)


def test_modification_creates_uniform_backlash() -> None:
    """修形使齿廓整体内缩，与针齿之间形成大小恒定的法向侧隙。"""
    contacts = contact_points(MOD, 0.41)
    world = disc_to_world(MOD, contacts, 0.41)
    d = np.linalg.norm(world - pin_centers(MOD), axis=1)
    assert np.allclose(d, MOD.pin_radius + MOD.equidistant_offset, atol=1e-9)
    assert d.min() > MOD.pin_radius  # 不接触、不穿透


def test_output_geometry_sane() -> None:
    res = analyze(CycloidParams(samples_per_tooth=32))
    out = res["output"]
    assert out["count"] >= 6
    assert out["hole_radius"] > 0
    assert out["circle_radius"] > out["hole_radius"]
    d = res["derived"]
    assert d["root_radius"] < d["tip_radius"]
    assert 2.5 < d["tooth_height"] < 3.5  # ≈ 2e


def test_undercut_detected() -> None:
    bad = CycloidParams(eccentricity=3.0)  # K1 = 3*40/88 = 1.36 > 1
    res = analyze(bad, samples=720)
    codes = {w["code"] for w in res["warnings"]}
    assert "undercut" in codes


def test_no_warnings_for_default() -> None:
    res = analyze(CycloidParams())
    errors = [w for w in res["warnings"] if w["level"] == "error"]
    assert errors == []


def test_profile_point_count() -> None:
    p = CycloidParams(tooth_count=20, samples_per_tooth=24)
    assert actual_profile(p).shape == (20 * 24, 2)
