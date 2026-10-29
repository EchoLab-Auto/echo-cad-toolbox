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
    mesh = volume_to_mesh(
        combined,
        pitch,
        origin,
        level=0.0,
        smooth_iters=int(params["smooth"]),
        decimate_ratio=float(params["decimate"]),
    )

    report(0.9, "清理网格")
    mesh = _clean_mesh(mesh, pitch)

    boundary_edges, nonmanifold_edges = _edge_health(mesh)
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
