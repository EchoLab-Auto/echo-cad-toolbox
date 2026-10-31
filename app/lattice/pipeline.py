"""晶格生成管线：参数校验 → 体场组合 → marching cubes → 网格后处理。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field

import numpy as np
import trimesh

from .implicit import KIND_INFO, LatticeField, estimate_relative_density
from .voxel import boundary_guard, occupancy_stats, volume_to_mesh

ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
DEFAULT_PARAMS: dict = {
    "lattice": "gyroid",          # 晶格类型
    "cell_size": 10.0,            # 单胞尺寸 mm
    "cell_size_z": None,          # 可选的 Z 向单胞尺寸（None = 同 cell_size）
    "thickness": 1.2,             # 壁厚 / 杆直径 mm
    "wall_mode": "sheet",         # sheet | solid（仅 TPMS）
    "mode": "intersect",          # intersect | shell | subtract | union
    "shell_thickness": 2.5,       # mode=shell 时的壳层厚度 mm
    "keep_skin": False,           # 保留原始外表面一层壳
    "skin_thickness": 1.2,        # keep_skin 时的表皮厚度 mm
    "resolution": 160,            # 体素分辨率（最长边体素数）
    "warp": 0.0,                  # 域扭曲幅度
    "grad": 0.0,                  # 沿 Z 的渐变率
    "smooth": 1,                  # 平滑迭代
    "decimate": 0.3,              # 抽稀比例 0~0.9
    "align": "min",               # min | center  单胞对齐
    "thin_wall": "auto",          # auto = 画不出的薄壁自动加厚到能画（结果里说明）；off = 按原值硬画
}

#: 前端表单用的参数区间（也用于后端校验）
PARAM_LIMITS: dict = {
    "cell_size": (1.0, 200.0),
    "cell_size_z": (1.0, 200.0),
    "thickness": (0.05, 50.0),
    "shell_thickness": (0.1, 100.0),
    "skin_thickness": (0.1, 50.0),
    "resolution": (48, 320),
    "warp": (0.0, 0.6),
    "grad": (-0.8, 0.8),
    "smooth": (0, 8),
    "decimate": (0.0, 0.95),
}

#: 壁厚 / 杆径"画得好看"要多少个体素 —— 用于给用户建议分辨率，不是硬门槛
#: （硬门槛是下面的 MIN_RENDER_WALL_VOXELS）。
#: 实测（摆线轮盘 ⌀167、gyroid、cell 10）固定 pitch=1.31 mm 只改壁厚：
#:
#: =========  ==========  ============  ==============
#: 壁厚       体素/壁厚   比表面积      结果
#: =========  ==========  ============  ==============
#: 1.2 mm    0.92        2.79 1/mm     碎成毛刺（真值 ≈0.87）
#: 2.0 mm    1.53        1.36 1/mm     仍明显虚高
#: 2.6 mm    1.98        1.12 1/mm     接近
#: 3.9 mm    2.98        0.87 1/mm     与解析值一致
#: =========  ==========  ============  ==============
#:
#: 低于 ~3 个体素时，采样点会漏掉薄壁（两端都在壁外），等值面只能靠"碰巧
#: 落在壁内的体素"拼出来 —— 于是变成一串毛刺 / 断续的瘤，表面积虚高好几倍。
WALL_VOXELS_GOOD = 3.0
#: 单胞至少要有多少个体素，否则晶格形状本身失真（gyroid 的曲面特征被抹平）
CELL_VOXELS_MIN = 5.0

#: 低于这个体素数就**画不出**这层壁（不是"画得糙"，是根本不连续）—— 与其交给
#: 用户一张满身尖刺的网格，不如自动加到刚好能画的厚度，并在结果里说清楚。
#: 实测（摆线轮盘 ⌀167 / gyroid / cell 10，以 res320 细体素为参照的比表面积偏差）：
#:
#: ===========  ============  ==================
#: 壁厚/体素     比表偏差       结论
#: ===========  ============  ==================
#: 0.92         +34.6%        尖刺（改前的默认参数）
#: 1.22         +1.2%         可用
#: 1.53         -1.1%         可用
#: 1.80         -2.2%         可用
#: ===========  ============  ==================
#:
#: 临界点在 1.0~1.2 之间，取 1.5 留余量（杆状晶格、渐变壁厚更吃体素）。
MIN_RENDER_WALL_VOXELS = 1.5

MODES = {
    "intersect": "实体 ∩ 晶格（内部填充晶格）",
    "shell": "仅表层壳内填充晶格",
    "subtract": "从实体中挖掉晶格（做减重腔）",
    "union": "实体 ∪ 晶格（外扩）",
}


def normalize_params(raw: dict | None, bounds=None) -> dict:
    """合并默认参数、做类型转换与范围裁剪。"""
    params = dict(DEFAULT_PARAMS)
    if raw:
        params.update({k: v for k, v in raw.items() if k in DEFAULT_PARAMS})

    params["lattice"] = str(params["lattice"]) if params["lattice"] in KIND_INFO else "gyroid"
    params["wall_mode"] = "solid" if str(params.get("wall_mode")) == "solid" else "sheet"
    if params["mode"] not in MODES:
        params["mode"] = "intersect"
    if str(params.get("align")) not in ("min", "center"):
        params["align"] = "min"
    # auto（默认）：画不出的薄壁自动加厚到能画；off：按用户填的原值硬画（会出尖刺）
    params["thin_wall"] = (
        "off" if str(params.get("thin_wall")).lower() in ("off", "keep", "none", "false", "0") else "auto"
    )

    for key, (lo, hi) in PARAM_LIMITS.items():
        if key == "cell_size_z" and params.get(key) in (None, "", "null"):
            params[key] = None
            continue
        try:
            val = float(params[key])
        except (TypeError, ValueError):
            val = float(DEFAULT_PARAMS[key] if DEFAULT_PARAMS[key] is not None else 10.0)
        params[key] = type(DEFAULT_PARAMS[key])(max(lo, min(hi, val))) if isinstance(
            DEFAULT_PARAMS[key], int
        ) else max(lo, min(hi, val))

    params["keep_skin"] = bool(params.get("keep_skin"))

    # 壁厚不能超过半个单胞，否则晶格会连成实心
    cell = float(params["cell_size"])
    if cell > 0:
        params["thickness"] = float(min(params["thickness"], 0.8 * cell))

    return params


# --------------------------------------------------------------------------- #
# 体场计算
# --------------------------------------------------------------------------- #
def _compute_lattice_field(
    lattice: LatticeField, shape, pitch: float, origin, chunk: int = 16
) -> np.ndarray:
    nx, ny, nz = (int(v) for v in shape)
    out = np.empty((nx, ny, nz), dtype=np.float32)
    xs = origin[0] + (np.arange(nx) + 0.5) * pitch
    ys = origin[1] + (np.arange(ny) + 0.5) * pitch
    zs = origin[2] + (np.arange(nz) + 0.5) * pitch
    X = xs[:, None, None]
    Y = ys[None, :, None]
    for k0 in range(0, nz, chunk):
        k1 = min(nz, k0 + chunk)
        depth = k1 - k0
        Z = zs[k0:k1].reshape(1, 1, depth)
        bx = np.broadcast_to(X, (nx, ny, depth))
        by = np.broadcast_to(Y, (nx, ny, depth))
        bz = np.broadcast_to(Z, (nx, ny, depth))
        out[:, :, k0:k1] = lattice.evaluate(bx, by, bz).astype(np.float32)
    return out


def combine_fields(solid: np.ndarray, lat: np.ndarray, params: dict) -> np.ndarray:
    """按模式组合实体场与晶格场（均为 <=0 表示材料）。"""
    mode = params["mode"]
    shell_t = float(params["shell_thickness"])
    skin_t = float(params["skin_thickness"])

    if mode == "intersect":
        combined = np.maximum(solid, lat)
    elif mode == "shell":
        shell = np.maximum(solid, -solid - shell_t)   # 表面向内 shell_t 的一层
        combined = np.maximum(shell, lat)
    elif mode == "subtract":
        combined = np.maximum(solid, -lat)            # 实体减去晶格
    elif mode == "union":
        combined = np.minimum(solid, lat)
    else:  # pragma: no cover - normalize_params 已兜底
        combined = np.maximum(solid, lat)

    if params.get("keep_skin") and mode in ("intersect", "shell"):
        skin = np.maximum(solid, -solid - skin_t)     # 原始外表面一层壳
        combined = np.minimum(combined, skin)         # 壳 ∪ 晶格填充

    return combined


def _fit_thickness_to_voxels(params: dict, pitch: float) -> tuple[float, str | None]:
    """薄壁兜底：把画不出来的壁厚抬到这套体素能画出来的最小值。

    体素比壁还粗时等值面不可能连续（采样点整块落在壁外），只会得到一张满身
    尖刺、表面积虚高 30%+ 的网格。与其把它交给用户，不如按"能画出来的最薄壁厚"
    渲染，并把这件事写进结果里 —— 用户至少拿到一个能切片、能打印的实体，也
    知道自己的 1.20 mm 到底变成了多少。

    返回 ``(实际使用的壁厚, 给用户的说明或 None)``。
    """
    requested = float(params["thickness"])
    if pitch <= 0 or params.get("thin_wall") == "off":
        return requested, None

    # 加厚也不能越过"壁厚 ≤ 0.8 单胞"这条线，否则晶格会连成实心
    floor = min(MIN_RENDER_WALL_VOXELS * pitch, 0.8 * float(params["cell_size"]))
    # 2% 容差：刚好卡在 1.5 个体素上时不要来回抖
    if requested >= floor * 0.98:
        return requested, None

    thicker = (floor / requested - 1.0) * 100.0 if requested > 0 else 0.0
    note = (
        f"ℹ 已按壁厚 {floor:.2f} mm 渲染（你填的是 {requested:.2f} mm）：体素 {pitch:.3f} mm，"
        f"{requested:.2f} mm 只占 {requested / pitch:.2f} 个体素，等值面画不出连续的壁，"
        f"硬画只会得到满身尖刺（实测表面积虚高 35%）。{floor:.2f} mm（{floor / pitch:.1f} 个"
        f"体素）是这套体素下能画出的最薄壁厚，比你填的厚 {thicker:.0f}%。"
        f"若要严格 {requested:.2f} mm：提高分辨率、把零件缩小，或切到浏览器 GPU 引擎；"
        f"也可以把参数 thin_wall 设为 off 按原值硬画。"
    )
    return floor, note


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
@dataclass
class BuildResult:
    mesh: trimesh.Trimesh
    stats: dict = dc_field(default_factory=dict)
    params: dict = dc_field(default_factory=dict)


def build(
    volume: dict, params: dict, progress: ProgressFn | None = None
) -> BuildResult:
    """根据体场与参数生成晶格网格。"""
    t_start = time.time()

    def report(frac: float, stage: str) -> None:
        if progress:
            progress(float(frac), stage)

    params = normalize_params(params)
    solid = volume["sdf"]
    pitch = float(volume["pitch"])
    origin = np.asarray(volume["origin"], dtype=np.float64)
    shape = solid.shape

    # 薄壁兜底：体素比壁还粗时硬画出来就是一张刺球，先把它抬到能画的厚度
    thickness_requested = float(params["thickness"])
    thickness_used, thin_note = _fit_thickness_to_voxels(params, pitch)
    params["thickness"] = thickness_used

    # 单胞对齐
    bounds = volume.get("bounds")
    cell_z = params["cell_size_z"] or params["cell_size"]
    if params.get("align") == "center" and bounds is not None:
        center = (np.asarray(bounds[0]) + np.asarray(bounds[1])) / 2.0
        offset = center - 0.5 * np.array([params["cell_size"], params["cell_size"], cell_z])
    else:
        offset = np.asarray(bounds[0], dtype=np.float64) if bounds is not None else np.zeros(3)

    grad_height = None
    if bounds is not None:
        grad_height = float(np.asarray(bounds[1])[2] - np.asarray(bounds[0])[2])

    lattice = LatticeField(
        kind=params["lattice"],
        cell=(params["cell_size"], params["cell_size"], cell_z),
        thickness=params["thickness"],
        wall_mode=params["wall_mode"],
        warp=params["warp"],
        grad=params["grad"],
        grad_height=grad_height,
        offset=tuple(float(v) for v in offset),
    )

    report(0.05, "计算晶格场")
    lat = _compute_lattice_field(lattice, shape, pitch, origin)

    report(0.55, "组合实体与晶格")
    combined = combine_fields(solid, lat, params)
    # 组合后可能顶到体场边界（union），必须再兜一次，否则等值面开口
    combined = boundary_guard(combined, pitch)

    # 统计（基于体素）
    orig = occupancy_stats(solid, pitch)
    new = occupancy_stats(combined, pitch)
    rel_density = (new["volume"] / orig["volume"]) if orig["volume"] > 0 else 0.0

    report(0.65, "提取等值面")
    mesh_notes: list[str] = []
    mesh = volume_to_mesh(
        combined,
        pitch,
        origin,
        level=0.0,
        smooth_iters=int(params["smooth"]),
        decimate_ratio=float(params["decimate"]),
        notes=mesh_notes,
    )

    report(0.9, "清理网格")
    mesh = _clean_mesh(mesh, pitch)

    boundary_edges, nonmanifold_edges = _edge_health(mesh)
    warnings = ([thin_note] if thin_note else []) + _resolution_warnings(
        params, pitch, mesh,
        extent=float(np.max(np.asarray(bounds[1]) - np.asarray(bounds[0]))) if bounds is not None else 0.0,
        wall_already_fixed=thin_note is not None,
    ) + list(mesh_notes)
    stats = {
        "faces": len(mesh.faces),
        "boundary_edges": boundary_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "vertices": len(mesh.vertices),
        "volume_mm3": float(abs(mesh.volume)),
        "area_mm2": float(mesh.area),
        "watertight": bool(mesh.is_watertight),
        "material_volume_mm3": float(new["volume"]),
        "original_volume_mm3": float(orig["volume"]),
        "relative_density": float(rel_density),
        "analytical_density": float(
            estimate_relative_density(
                params["lattice"],
                (params["cell_size"], params["cell_size"], cell_z),
                params["thickness"],
            )
        ),
        "voxel_pitch": float(pitch),
        "voxel_shape": [int(v) for v in shape],
        "wall_voxels": float(params["thickness"] / pitch) if pitch > 0 else 0.0,
        "thickness_requested": float(thickness_requested),
        "thickness_used": float(thickness_used),
        "thin_wall_fixed": bool(thin_note),
        "min_wall_voxels": float(MIN_RENDER_WALL_VOXELS),
        "cell_voxels": float(min(params["cell_size"], cell_z) / pitch) if pitch > 0 else 0.0,
        "warnings": warnings,
        "seconds": round(time.time() - t_start, 2),
        "extents": [float(v) for v in mesh.extents],
    }
    report(1.0, "完成")
    return BuildResult(mesh=mesh, stats=stats, params=params)


def _edge_health(mesh: trimesh.Trimesh) -> tuple[int, int]:
    """返回 (开口边数, 非流形边数)。开口边 > 0 说明网格没闭合。"""
    try:
        edges = mesh.edges_sorted
        _uniq, counts = np.unique(edges, axis=0, return_counts=True)
        return int((counts == 1).sum()), int((counts > 2).sum())
    except Exception:  # noqa: BLE001
        return -1, -1


def _resolution_warnings(
    params: dict,
    pitch: float,
    mesh: trimesh.Trimesh,
    extent: float = 0.0,
    wall_already_fixed: bool = False,
) -> list[str]:
    """把「这个分辨率根本画不出这层壁」翻译成用户能动手的一句话。

    体素化之后 pitch 就定死了，用户能改的只有壁厚 / 分辨率 / 模型尺寸，所以每条
    建议都给**具体数值**，而不是一句「请提高分辨率」。
    """
    out: list[str] = []
    if pitch <= 0:
        return out

    wall = float(params["thickness"])
    wall_vox = wall / pitch
    min_thick = MIN_RENDER_WALL_VOXELS * pitch
    # 阈值就是"画得出连续的壁"的下限（实测 1.2 个体素起比表面积偏差已在 ±2% 内）；
    # thin_wall=off 时才会落到这里 —— auto 模式下已经兜底加厚过，不该再念一遍。
    if wall_vox < MIN_RENDER_WALL_VOXELS and not wall_already_fixed:
        if wall_vox < 1.0:
            msg = (
                f"壁厚 {wall:.2f} mm 只有 {wall_vox:.2f} 个体素（体素 {pitch:.3f} mm），"
                f"比一个体素还薄 —— 采样点会整块漏掉这层壁，等值面只能把零星落在壁内的"
                f"体素连起来，于是表面碎成一片尖刺/毛边（实测表面积虚高 35%）。"
            )
        else:
            msg = (
                f"壁厚 {wall:.2f} mm 只有 {wall_vox:.2f} 个体素（体素 {pitch:.3f} mm，"
                f"至少要 {MIN_RENDER_WALL_VOXELS:.1f} 个才能画出连续的壁）—— 表面会出现"
                f"毛边和断续的凸起，尺寸也不准。"
            )
        fixes = [f"把壁厚加到 ≥ {min_thick:.1f} mm"]
        if extent > 0:
            need = int(np.ceil(extent * MIN_RENDER_WALL_VOXELS / max(wall, 1e-9)))
            fixes.append(f"或把分辨率提到 ≥ {need}（受本机内存上限约束）")
        fixes.append("或把模型等比缩小")
        out.append("⚠ " + msg + "可选：" + "；".join(fixes) + "。")

    cell = min(
        float(params["cell_size"]),
        float(params.get("cell_size_z") or params["cell_size"]),
    )
    if cell / pitch < CELL_VOXELS_MIN:
        out.append(
            f"⚠ 单胞 {cell:.1f} mm 只有 {cell / pitch:.1f} 个体素"
            f"（建议 ≥ {CELL_VOXELS_MIN:.0f}）：晶格本身的形状会被抹平，"
            f"先加大单胞或提高分辨率。"
        )

    if not mesh.is_watertight:
        try:
            counts = np.unique(np.asarray(mesh.edges_sorted), axis=0, return_counts=True)[1]
            nonmanifold = int((counts > 2).sum())
        except Exception:  # noqa: BLE001
            nonmanifold = 0
        if nonmanifold > max(4, len(mesh.faces) // 5000):
            out.append(
                f"⚠ 网格有 {nonmanifold} 条非流形边（不是闭合实体），切片前要修："
                f"多半是抽稀把薄壁两层缝到了一起，把「抽稀」调小或调 0。"
            )
    return out


def _clean_mesh(mesh: trimesh.Trimesh, pitch: float) -> trimesh.Trimesh:
    """去掉脱离主体的小碎片（体素噪声），并尽量把开口补上。"""
    try:
        parts = mesh.split(only_watertight=False)
    except Exception:  # noqa: BLE001
        return _seal_mesh(mesh)

    if len(parts) > 1:
        vols = np.array([
            abs(p.volume) if p.is_watertight else float(len(p.faces)) * pitch * pitch * 0.1
            for p in parts
        ])
        max_vol = float(vols.max()) if len(vols) else 0.0
        min_vol = max((4.0 * pitch) ** 3, 1e-3 * max_vol)
        keep = [p for p, v in zip(parts, vols) if v >= min_vol]
        if not keep:
            keep = [parts[int(np.argmax(vols))]]
        if len(keep) != len(parts):
            mesh = trimesh.util.concatenate(keep)
            mesh.process(validate=True)

    return _seal_mesh(mesh)


def _seal_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """对少量开口做补洞（大规模开口说明模型本身有问题，补了反而慢）。"""
    try:
        if mesh.is_watertight:
            return mesh
        edges = mesh.edges_sorted
        _uniq, counts = np.unique(edges, axis=0, return_counts=True)
        n_boundary = int((counts == 1).sum())
        if 0 < n_boundary <= 20000:
            trimesh.repair.fill_holes(mesh)
            if not mesh.is_watertight:
                trimesh.repair.fix_normals(mesh, multibody=True)
    except Exception:  # noqa: BLE001
        pass
    return mesh
