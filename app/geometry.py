"""摆线针轮（cycloidal / epicycloid pin-wheel）传动的几何核心。

本模块只依赖 numpy，包含传动参数、齿廓解析求解、运动学、接触分析
与几何合法性检查，是 Web API、DXF 导出与测试的共同基础。

坐标系约定
----------
世界系（world）
    与针轮（针齿壳）固连，原点位于针轮中心。针齿在此系中静止不动。
摆线轮系（disc）
    与摆线轮固连，原点位于摆线轮中心。

运动学（针轮固定，转臂输入）
----------------------------
转臂（偏心轴）转角 φ：

* 摆线轮中心   C(φ) = e·(cos φ, sin φ)
* 摆线轮自转角 θ_c(φ) = -φ / Zc
* 传动比       i = Zc  （摆线轮反向低速输出，即减速比 Zc:1）
* 输出盘（柱销盘）转角 θ_out = θ_c

齿廓（短幅外摆线的等距曲线）
----------------------------
    Zp = Zc + 1                       针齿数
    Rz' = Rz - ΔRz                    移距修形后的针齿中心圆半径
    rp' = rp + Δrp                    等距修形后的针齿半径

    P(τ) = ( Rz'·cos τ - e·cos(Zp·τ),  Rz'·sin τ - e·sin(Zp·τ) )      理论齿廓
    S(τ) = sqrt(Rz'² + (e·Zp)² - 2·Rz'·e·Zp·cos(Zc·τ))
    N(τ) = ( Rz'·cos τ - e·Zp·cos(Zp·τ),  Rz'·sin τ - e·Zp·sin(Zp·τ) ) / S(τ)
    Q(τ) = P(τ) - rp'·N(τ)            实际齿廓（内等距曲线）

``N`` 为单位外法线，``Q`` 即工程上使用的摆线轮齿廓。

接触律
------
转臂角为 φ 时，第 i 个针齿（ψ_i = 2π·i/Zp）与齿廓的接触点位于

    Q_i = Q(ψ_i + φ / Zc)

即理论齿廓 P 正是针齿中心在摆线轮系中划出的轨迹：``P(ψ_i + φ/Zc)``
等于第 i 个针齿中心在摆线轮系中的坐标（精确相等）。上述关系由数值
包络校验（见 ``tests/test_geometry.py``）确认。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

TAU = 2.0 * math.pi

#: 齿廓点默认采样密度（每齿点数）
DEFAULT_SAMPLES_PER_TOOTH = 64

#: 判定「接触 / 相切」的容差（mm）
CONTACT_TOL = 1e-6


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycloidParams:
    """摆线针轮副的几何参数（全部长度单位为 mm，角度输入单位为度）。

    默认值对应一个中等规格的 RV 型摆线轮（Zc=39 / Zp=40），可直接生成
    可用于加工的闭合齿廓。
    """

    # --- 基本参数 ---
    tooth_count: int = 39
    """摆线轮齿数 Zc。针齿数自动取 Zp = Zc + 1。

    取值范围 6 ~ 120（更大的值没有实际意义且数值上更易退化）。
    """

    eccentricity: float = 1.43
    """偏心距 e（转臂偏心量）。"""

    pin_circle_radius: float = 88.0
    """针齿中心圆半径 Rz（针齿分布圆半径）。"""

    pin_radius: float = 5.5
    """针齿（针齿套）半径 rp。"""

    # --- 齿廓修形（用于形成啮合侧隙，正值均为「加大间隙」方向）---
    equidistant_offset: float = 0.02
    """等距修形量 Δrp：虚拟针齿半径增大 Δrp，齿廓整体内缩。"""

    radial_offset: float = 0.0
    """移距修形量 ΔRz：针齿中心圆半径减小 ΔRz，齿廓外径随之减小。"""

    rotary_offset_deg: float = 0.0
    """转角修形量（度）：整个齿廓绕中心旋转，用于产生侧隙。"""

    # --- 针齿壳 ---
    ring_wall: float = 12.0
    """针齿壳壁厚（壳体外圆 = Rz + rp + ring_wall）。"""

    # --- 输出机构（柱销 / W 机构）---
    pin_hole_count: int = 0
    """柱销（= 柱销孔）数量。0 表示自动选取可行值。"""

    pin_hole_circle_radius: float = 0.0
    """柱销孔分布圆半径 Rw。0 表示自动。"""

    pin_sleeve_radius: float = 0.0
    """柱销套半径 rw（孔半径 = rw + e + clearance）。0 表示自动。"""

    clearance: float = 0.0
    """柱销孔额外单边间隙。"""

    center_bore_radius: float = 0.0
    """摆线轮中心孔半径。0 表示自动。"""

    # --- 采样 ---
    samples_per_tooth: int = DEFAULT_SAMPLES_PER_TOOTH
    """每个齿的齿廓采样点数（决定轮廓平滑度与 DXF 顶点数）。"""

    # -- 派生量 ---------------------------------------------------------------

    @property
    def pin_count(self) -> int:
        """针齿数 Zp = Zc + 1。"""
        return self.tooth_count + 1

    @property
    def effective_pin_circle_radius(self) -> float:
        """移距修形后的针齿中心圆半径 Rz'。"""
        return self.pin_circle_radius - self.radial_offset

    @property
    def effective_pin_radius(self) -> float:
        """等距修形后的（虚拟）针齿半径 rp'。"""
        return self.pin_radius + self.equidistant_offset

    @property
    def rotary_offset(self) -> float:
        """转角修形量（rad）。"""
        return math.radians(self.rotary_offset_deg)

    @property
    def short_amplitude_coefficient(self) -> float:
        """短幅系数 K1 = e·Zp / Rz。K1 < 1 才不根切，工程常用 0.5 ~ 0.75。"""
        return self.eccentricity * self.pin_count / self.pin_circle_radius

    @property
    def ratio(self) -> float:
        """针轮固定、转臂输入时的传动比 i = Zc（摆线轮反向输出）。"""
        return float(self.tooth_count)

    @property
    def tip_clearance_limit(self) -> float:
        """理论齿廓尖点（根切）临界值 Rz' - e·Zp，必须 > 0。"""
        return self.effective_pin_circle_radius - self.eccentricity * self.pin_count

    # -- 构造 / 序列化 --------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None, **overrides: Any) -> "CycloidParams":
        """从任意 mapping 构造（忽略未知字段），可叠加 overrides。"""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        clean: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key in known and value is not None:
                clean[key] = value
        clean.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（含派生量，便于前端展示）。"""
        data = asdict(self)
        data.update(
            pin_count=self.pin_count,
            effective_pin_circle_radius=self.effective_pin_circle_radius,
            effective_pin_radius=self.effective_pin_radius,
            short_amplitude_coefficient=self.short_amplitude_coefficient,
            ratio=self.ratio,
        )
        return data


# ---------------------------------------------------------------------------
# 齿廓曲线（解析式，矢量化）
# ---------------------------------------------------------------------------


def _sample_tau(params: CycloidParams, samples: int | None = None) -> np.ndarray:
    """齿廓参数 τ 的采样点（不含末端点，闭合曲线用）。"""
    if samples is None or samples <= 0:
        samples = params.tooth_count * params.samples_per_tooth
    samples = max(int(samples), 16)
    return np.linspace(0.0, TAU, samples, endpoint=False)


def theoretical_profile(params: CycloidParams, samples: int | None = None) -> np.ndarray:
    """理论齿廓 P(τ)：短幅外摆线（= 针齿中心在摆线轮系中的轨迹）。

    返回 ``(N, 2)`` 数组，按 τ 递增顺序排列，首尾不重复。
    """
    tau = _sample_tau(params, samples)
    rz = params.effective_pin_circle_radius
    ecc = params.eccentricity
    zp = params.pin_count
    x = rz * np.cos(tau) - ecc * np.cos(zp * tau)
    y = rz * np.sin(tau) - ecc * np.sin(zp * tau)
    return np.column_stack((x, y))


def profile_derivatives(
    params: CycloidParams, tau: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """理论齿廓的一阶/二阶导数与法线尺度因子。

    返回 ``(dx, dy, ddx, ddy, s)``，其中 ``s = |dP/dτ|``。
    """
    rz = params.effective_pin_circle_radius
    ecc = params.eccentricity
    zp = params.pin_count
    tau = np.asarray(tau, dtype=float)

    dx = -rz * np.sin(tau) + ecc * zp * np.sin(zp * tau)
    dy = rz * np.cos(tau) - ecc * zp * np.cos(zp * tau)
    ddx = -rz * np.cos(tau) + ecc * zp * zp * np.cos(zp * tau)
    ddy = -rz * np.sin(tau) + ecc * zp * zp * np.sin(zp * tau)

    s = np.sqrt(np.maximum(rz * rz + (ecc * zp) ** 2 - 2.0 * rz * ecc * zp * np.cos(params.tooth_count * tau), 0.0))
    return dx, dy, ddx, ddy, s


def profile_normals(params: CycloidParams, tau: np.ndarray | None = None) -> np.ndarray:
    """理论齿廓的单位外法线 N(τ)，形状 ``(N, 2)``。"""
    tau = _sample_tau(params) if tau is None else np.asarray(tau, dtype=float)
    dx, dy, _, _, s = profile_derivatives(params, tau)
    safe = np.where(s < 1e-12, 1.0, s)
    return np.column_stack((dy / safe, -dx / safe))


def curvature_radius(params: CycloidParams, tau: np.ndarray) -> np.ndarray:
    """理论齿廓的带符号曲率半径 ρ = 1/κ（mm）。"""
    dx, dy, ddx, ddy, s = profile_derivatives(params, tau)
    num = dx * ddy - dy * ddx
    den = np.power(np.maximum(s, 1e-12), 3)
    kappa = num / den
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = 1.0 / kappa
    return np.where(np.isfinite(rho), rho, np.sign(num) * np.inf)


def _eval_profile(params: CycloidParams, tau: np.ndarray | float) -> np.ndarray:
    """在实际齿廓上解析求点（含转角修形），``tau`` 支持标量或数组。"""
    tau = np.atleast_1d(np.asarray(tau, dtype=float))
    rz = params.effective_pin_circle_radius
    ecc = params.eccentricity
    zp = params.pin_count
    rp = params.effective_pin_radius

    dx = -rz * np.sin(tau) + ecc * zp * np.sin(zp * tau)
    dy = rz * np.cos(tau) - ecc * zp * np.cos(zp * tau)
    s = np.sqrt(np.maximum(
        rz * rz + (ecc * zp) ** 2 - 2.0 * rz * ecc * zp * np.cos(params.tooth_count * tau), 0.0))
    safe = np.where(s < 1e-12, np.nan, s)
    nx = dy / safe
    ny = -dx / safe

    x = rz * np.cos(tau) - ecc * np.cos(zp * tau) - rp * nx
    y = rz * np.sin(tau) - ecc * np.sin(zp * tau) - rp * ny

    rot = params.rotary_offset
    if abs(rot) > 1e-12:
        c, sn = math.cos(-rot), math.sin(-rot)
        x, y = x * c - y * sn, x * sn + y * c
    return np.column_stack((x, y))


def _resample_closed(pts: np.ndarray, count: int, density: int = 6) -> np.ndarray:
    """按弧长均匀重采样闭合折线（先加密再等距插值）。"""
    if pts.shape[0] < 3 or count <= 0:
        return pts
    dense = pts
    if density > 1:
        n = pts.shape[0]
        idx = np.arange(n)
        dense = np.column_stack((
            np.interp(np.linspace(0, n, n * density, endpoint=False), idx, pts[:, 0], period=n),
            np.interp(np.linspace(0, n, n * density, endpoint=False), idx, pts[:, 1], period=n),
        ))
    closed = np.vstack((dense, dense[:1]))
    seg = np.hypot(np.diff(closed[:, 0]), np.diff(closed[:, 1]))
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    total = cum[-1]
    if total <= 0:
        return pts
    target = np.linspace(0.0, total, count, endpoint=False)
    return np.column_stack((np.interp(target, cum, closed[:, 0]), np.interp(target, cum, closed[:, 1])))


def actual_profile(
    params: CycloidParams, samples: int | None = None, arc_uniform: bool = True
) -> np.ndarray:
    """实际齿廓 Q(τ)：理论齿廓沿外法线内偏 rp'，含转角修形。

    ``arc_uniform=True`` 时按弧长均匀重采样，使齿廓点分布均匀（更利于
    渲染与 DXF/CAM 使用）。返回 ``(N, 2)`` 闭合轮廓点，首尾不重复。
    """
    tau = _sample_tau(params, samples)
    pts = _eval_profile(params, tau)
    pts = pts[np.all(np.isfinite(pts), axis=1)]
    if arc_uniform and pts.shape[0] > 8:
        pts = _resample_closed(pts, tau.size)
    return pts


def contact_point(params: CycloidParams, pin_index: int, arm_angle: float) -> np.ndarray:
    """转臂角 ``arm_angle``（rad）时第 ``pin_index`` 个针齿的接触点（摆线轮系）。"""
    tau = TAU * pin_index / params.pin_count + arm_angle / params.tooth_count
    return _eval_profile(params, tau)[0]


def contact_points(params: CycloidParams, arm_angle: float) -> np.ndarray:
    """全部针齿在转臂角 ``arm_angle`` 时的接触点（摆线轮系），形状 ``(Zp, 2)``。"""
    idx = np.arange(params.pin_count)
    tau = TAU * idx / params.pin_count + arm_angle / params.tooth_count
    return _eval_profile(params, tau)


# ---------------------------------------------------------------------------
# 运动学
# ---------------------------------------------------------------------------


def arm_center(params: CycloidParams, arm_angle: float) -> tuple[float, float]:
    """摆线轮中心（世界系）C(φ)。"""
    return (
        params.eccentricity * math.cos(arm_angle),
        params.eccentricity * math.sin(arm_angle),
    )


def disc_angle(params: CycloidParams, arm_angle: float) -> float:
    """摆线轮（= 输出盘）自转角 θ_c(φ) = -φ/Zc。"""
    return -arm_angle / params.tooth_count


def disc_to_world(params: CycloidParams, pts: np.ndarray, arm_angle: float) -> np.ndarray:
    """把摆线轮系中的点变换到世界系（旋转 θ_c 后平移到 C）。"""
    theta = disc_angle(params, arm_angle)
    c, s = math.cos(theta), math.sin(theta)
    pts = np.asarray(pts, dtype=float)
    rot = np.column_stack((pts[:, 0] * c - pts[:, 1] * s, pts[:, 0] * s + pts[:, 1] * c))
    cx, cy = arm_center(params, arm_angle)
    return rot + np.array([cx, cy])


def world_to_disc(params: CycloidParams, pts: np.ndarray, arm_angle: float) -> np.ndarray:
    """世界系 → 摆线轮系（:func:`disc_to_world` 的逆变换）。"""
    theta = disc_angle(params, arm_angle)
    c, s = math.cos(-theta), math.sin(-theta)
    cx, cy = arm_center(params, arm_angle)
    shifted = np.asarray(pts, dtype=float) - np.array([cx, cy])
    return np.column_stack((shifted[:, 0] * c - shifted[:, 1] * s, shifted[:, 0] * s + shifted[:, 1] * c))


# ---------------------------------------------------------------------------
# 针轮 / 输出机构
# ---------------------------------------------------------------------------


def pin_centers(params: CycloidParams) -> np.ndarray:
    """全部针齿中心（世界系，静止），形状 ``(Zp, 2)``。"""
    psi = TAU * np.arange(params.pin_count) / params.pin_count
    return np.column_stack((params.pin_circle_radius * np.cos(psi),
                            params.pin_circle_radius * np.sin(psi)))


def output_layout(params: CycloidParams) -> dict[str, Any]:
    """输出机构（柱销 / 柱销孔）布局，自动补全未指定的参数。

    自动值按以下工程约束求解（均可在参数中显式覆盖）：

    * 柱销孔外壁到齿根至少保留 ``MIN_RIM`` = 2 mm
    * 相邻柱销孔之间至少保留 1.2 mm 壁厚
    * 柱销孔与中心孔之间至少保留 ``MIN_WEB`` = 2 mm
    * 柱销数默认 ≈ Zc/4（4 ~ 12 个），随齿数自动缩放

    返回 dict：

    * ``count``            柱销数 Zw
    * ``circle_radius``    柱销孔分布圆半径 Rw
    * ``sleeve_radius``    柱销套半径 rw
    * ``hole_radius``      柱销孔半径 = rw + e + clearance
    * ``hole_centers``     柱销孔中心（摆线轮系），形状 (Zw, 2)
    """
    MIN_RIM = 2.0
    MIN_WEB = 2.0
    MIN_GAP = 1.2

    zc = params.tooth_count
    prof = actual_profile(params, samples=max(720, zc * 24))
    radii = np.hypot(prof[:, 0], prof[:, 1])
    root_radius = float(radii.min())
    tip_radius = float(radii.max())

    # --- 柱销数 ---
    count = params.pin_hole_count
    if count <= 0:
        count = int(min(12, max(4, round(zc / 4))))
        count = min(count, zc)
    count = max(int(count), 3)

    # --- 分布圆半径：默认取齿根圆的 0.72，且必须给外壁留出空间 ---
    circle_radius = params.pin_hole_circle_radius
    if circle_radius <= 0:
        circle_radius = 0.72 * root_radius
    circle_radius = float(min(circle_radius, max(root_radius - MIN_RIM - 1.0, 0.5)))

    chord = 2.0 * circle_radius * math.sin(math.pi / count)
    hole_max_pitch = chord / 2.0 - MIN_GAP / 2.0
    hole_max_rim = root_radius - MIN_RIM - circle_radius
    hole_max = min(hole_max_pitch, hole_max_rim)

    # --- 柱销套半径 ---
    sleeve = params.pin_sleeve_radius
    if sleeve <= 0:
        sleeve = max(chord / 3.0 - params.eccentricity, 0.5)
    sleeve = float(sleeve)

    hole_radius = sleeve + params.eccentricity + params.clearance
    if hole_radius > hole_max:
        hole_radius = max(hole_max, 0.4)
        sleeve = max(hole_radius - params.eccentricity - params.clearance, 0.1)

    # --- 中心孔 ---
    bore_max = circle_radius - hole_radius - MIN_WEB
    bore = params.center_bore_radius
    if bore <= 0:
        # 自动中心孔：不超过齿根圆半径的 45%，否则零件过于"薄环"
        bore = max(min(bore_max, 0.45 * root_radius), 0.0)
    bore = float(bore)

    psi = TAU * np.arange(count) / count
    hole_centers = np.column_stack((circle_radius * np.cos(psi), circle_radius * np.sin(psi)))

    return {
        "count": int(count),
        "circle_radius": float(circle_radius),
        "sleeve_radius": float(sleeve),
        "hole_radius": float(hole_radius),
        "hole_centers": hole_centers,
        "center_bore_radius": float(bore),
        "root_radius": root_radius,
        "tip_radius": tip_radius,
        "hole_max": float(hole_max),
        "bore_max": float(bore_max),
    }


def ring_radii(params: CycloidParams) -> dict[str, float]:
    """针齿壳的关键半径。

    * ``pin_outer``  针齿外缘半径 Rz + rp
    * ``inner``      针齿壳内圆半径（应容纳全部针齿）Rz + rp + 1
    * ``outer``      针齿壳外圆半径
    """
    pin_outer = params.pin_circle_radius + params.pin_radius
    inner = pin_outer + 1.0
    outer = inner + params.ring_wall
    return {"pin_outer": pin_outer, "inner": inner, "outer": outer}


# ---------------------------------------------------------------------------
# 分析 / 校验
# ---------------------------------------------------------------------------


def analyze(params: CycloidParams, samples: int | None = None) -> dict[str, Any]:
    """完整几何分析：轮廓、派生量、接触状态与告警。"""
    n = samples or params.tooth_count * params.samples_per_tooth
    tau = _sample_tau(params, n)
    theo = theoretical_profile(params, n)
    act = actual_profile(params, n)
    rho = curvature_radius(params, tau)
    _, _, _, _, s = profile_derivatives(params, tau)

    radii = np.hypot(act[:, 0], act[:, 1])
    tip_radius = float(radii.max())
    root_radius = float(radii.min())

    theo_radii = np.hypot(theo[:, 0], theo[:, 1])
    theo_max = float(theo_radii.max())

    out = output_layout(params)
    ring = ring_radii(params)

    # 理论法向侧隙：等距修形直接形成法向间隙
    backlash = params.equidistant_offset

    min_curv = float(np.min(np.abs(rho))) if rho.size else math.inf
    s_min = float(np.min(s))

    # 星形性检测：摆线轮齿廓必须是关于中心的星形曲线（极角单调），
    # 否则说明等距偏移过量，齿廓自交 / 出现勾形。
    theta = np.unwrap(np.arctan2(act[:, 1], act[:, 0]))
    dtheta = np.diff(np.concatenate((theta, [theta[0] + TAU])))
    star_shaped = bool(np.all(dtheta > 0))
    fold_ratio = float(dtheta.min() / dtheta.mean()) if dtheta.size and dtheta.mean() > 0 else 0.0

    warnings = _collect_warnings(
        params, out, ring, min_curv, s_min, tip_radius, root_radius, star_shaped, fold_ratio
    )

    derived = {
        "pin_count": params.pin_count,
        "ratio": params.ratio,
        "short_amplitude_coefficient": params.short_amplitude_coefficient,
        "root_radius": root_radius,
        "tip_radius": tip_radius,
        "theoretical_tip_radius": theo_max,
        "tooth_height": tip_radius - root_radius,
        "tooth_pitch_angle_deg": 360.0 / params.tooth_count,
        "min_curvature_radius": min_curv,  # 理论齿廓（针齿中心轨迹）的最小曲率半径
        "normal_scale_min": s_min,
        "star_shaped": star_shaped,
        "fold_ratio": fold_ratio,
        "theoretical_backlash": backlash,
        "pin_ring": ring,
        "output": {
            "count": out["count"],
            "circle_radius": out["circle_radius"],
            "sleeve_radius": out["sleeve_radius"],
            "hole_radius": out["hole_radius"],
            "center_bore_radius": out["center_bore_radius"],
        },
        "profile_length": float(np.sum(np.hypot(*(np.roll(act, -1, axis=0) - act).T))),
        "undercut_limit": params.tip_clearance_limit,
    }

    return {
        "params": params.to_dict(),
        "derived": derived,
        "warnings": warnings,
        "profile": {
            "actual": act.tolist(),
            "theoretical": theo.tolist(),
            "tau": tau.tolist(),
        },
        "pin_ring": {
            "pin_centers": pin_centers(params).tolist(),
            "pin_radius": params.pin_radius,
            "pitch_radius": params.pin_circle_radius,
            "outer_radius": ring["outer"],
            "inner_radius": ring["inner"],
            "pin_outer_radius": ring["pin_outer"],
        },
        "output": {
            "hole_centers": out["hole_centers"].tolist(),
            "hole_radius": out["hole_radius"],
            "sleeve_radius": out["sleeve_radius"],
            "circle_radius": out["circle_radius"],
            "count": out["count"],
            "center_bore_radius": out["center_bore_radius"],
        },
    }


def _collect_warnings(
    params: CycloidParams,
    out: dict[str, Any],
    ring: dict[str, float],
    min_curv: float,
    s_min: float,
    tip_radius: float,
    root_radius: float,
    star_shaped: bool,
    fold_ratio: float,
) -> list[dict[str, str]]:
    """几何合法性检查，返回告警列表。"""
    warn: list[dict[str, str]] = []

    k1 = params.short_amplitude_coefficient
    if k1 >= 1.0:
        warn.append({
            "level": "error",
            "code": "undercut",
            "message": f"短幅系数 K1 = {k1:.3f} ≥ 1，理论齿廓出现尖点（根切），无法生成有效齿廓。请增大 Rz 或减小 e。",
        })
    elif k1 > 0.85:
        warn.append({
            "level": "warn",
            "code": "undercut_near",
            "message": f"短幅系数 K1 = {k1:.3f} 偏高（>0.85），齿廓接近根切，齿根应力较大。",
        })
    elif k1 < 0.3:
        warn.append({
            "level": "info",
            "code": "low_k1",
            "message": f"短幅系数 K1 = {k1:.3f} 偏低（<0.3），齿廓接近圆弧，承载能力下降。",
        })

    if s_min <= 1e-6:
        warn.append({
            "level": "error",
            "code": "cusp",
            "message": "法线尺度因子 S(τ) 出现零值，齿廓存在尖点，几何非法。",
        })

    if not star_shaped:
        warn.append({
            "level": "error",
            "code": "self_intersection",
            "message": (
                "实际齿廓不是星形曲线（极角出现回折），等距偏移量过大导致齿廓自交。"
                "请减小针齿半径 rp 或减小等距修形 Δrp。"
            ),
        })

    if params.effective_pin_radius > 0.25 * params.pin_circle_radius:
        warn.append({
            "level": "info",
            "code": "large_pin",
            "message": "针齿半径相对针齿中心圆半径偏大（>0.25），齿廓趋于平坦。",
        })

    if root_radius <= out["center_bore_radius"] + 2.0:
        warn.append({
            "level": "warn",
            "code": "thin_rim",
            "message": "齿根圆与中心孔之间壁厚不足 2 mm。",
        })

    rim = root_radius - (out["circle_radius"] + out["hole_radius"])
    if rim < 2.0:
        warn.append({
            "level": "warn",
            "code": "hole_overrun",
            "message": f"柱销孔外壁厚度仅 {rim:.2f} mm（<2 mm），实际会被齿根切穿。",
        })

    if out["circle_radius"] - out["hole_radius"] - out["center_bore_radius"] < 2.0:
        warn.append({
            "level": "warn",
            "code": "hole_bore_overlap",
            "message": "柱销孔与中心孔之间的壁厚不足 2 mm。",
        })

    if params.eccentricity <= 0:
        warn.append({"level": "error", "code": "e_zero", "message": "偏心距必须为正。"})
    if params.pin_radius <= 0:
        warn.append({"level": "error", "code": "rz_zero", "message": "针齿半径必须为正。"})

    return warn


# ---------------------------------------------------------------------------
# 接触表（供前端动画查表使用）
# ---------------------------------------------------------------------------


def contact_table(params: CycloidParams, steps: int = 72) -> dict[str, Any]:
    """预计算一个转臂周期内的啮合状态（供前端动画高亮）。

    对每个转臂角 φ_k = 2πk/steps：

    * ``pin_indices[k]``  承力侧（间隙最小的约一半）针齿索引
    * ``gaps[k]``        对应针齿的法向间隙（mm）
    * ``disc_angle``     摆线轮自转角（rad）
    * ``center``         摆线轮中心（世界系）
    """
    steps = max(int(steps), 8)
    rp = params.pin_radius
    pins = pin_centers(params)
    indices: list[list[int]] = []
    gaps_out: list[list[float]] = []
    discs: list[float] = []
    centers: list[list[float]] = []

    for k in range(steps):
        phi = TAU * k / steps
        world = disc_to_world(params, contact_points(params, phi), phi)
        gaps = np.hypot(*(world - pins).T) - rp
        order = np.argsort(gaps)
        half = max(1, params.pin_count // 2)
        sel = np.sort(order[:half])
        indices.append([int(i) for i in sel])
        gaps_out.append([round(float(gaps[i]), 6) for i in sel])
        discs.append(disc_angle(params, phi))
        centers.append(list(arm_center(params, phi)))

    return {
        "steps": steps,
        "pin_indices": indices,
        "gaps": gaps_out,
        "disc_angle": discs,
        "center": centers,
    }
