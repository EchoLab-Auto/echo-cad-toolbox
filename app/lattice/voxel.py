"""体素化（网格 → 有符号距离场）与等值面提取（体场 → 三角网格）。

属于晶格化工具（``app.lattice``）。

体素化采用**沿 Z 的射线奇偶法**：对每个 (x, y) 栅格列发一条向下的射线，收集
与网格表面的全部交点，按高度排序后奇偶填充。相比"体素包含性测试"，它一次性
批量完成、不依赖拾取引擎的包围体实现，对 CAD 离散出来的三角汤也稳健。

随后用欧氏距离变换（EDT）把占据栅格转成 SDF，并带 ``0.5 * pitch`` 的体素中心
修正，使零等值面落在真实表面附近，marching cubes 出来不会被体素"阶梯化"。
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage


class VolumeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 网格 → SDF 体场
# --------------------------------------------------------------------------- #
def _ray_occupancy(
    mesh: trimesh.Trimesh,
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    z_start: float,
    chunk: int = 16384,
) -> np.ndarray:
    """沿 -Z 方向射线奇偶填充，返回 shape (nx, ny, nz) 的 bool 占据栅格。"""
    nx, ny, nz = len(xs), len(ys), len(zs)
    ncol = nx * ny

    z0 = float(zs[0])
    pitch = float(zs[1] - zs[0]) if nz > 1 else 1.0

    # 轻微抖动，避开与三角形共面/共边造成的奇偶退化（幅度远小于体素）
    jx = xs + pitch * 1e-3 * (np.arange(nx) % 3 - 1)
    jy = ys + pitch * 1e-3 * (np.arange(ny) % 3 - 1)

    grid = np.zeros(ncol * nz, dtype=np.int32)
    for start in range(0, ncol, chunk):
        idx = np.arange(start, min(ncol, start + chunk))
        cols = np.column_stack([jx[idx // ny], jy[idx % ny], np.full(idx.size, z_start)])
        dirs = np.tile([0.0, 0.0, -1.0], (idx.size, 1))

        try:
            hits, idx_ray, _idx_tri = mesh.ray.intersects_location(
                cols, dirs, multiple_hits=True
            )
        except Exception as exc:
            raise VolumeError(f"射线求交失败：{exc}") from exc

        if len(hits) == 0:
            continue

        zvals = np.asarray(hits, dtype=np.float64)[:, 2]
        # 交点下方最近的体素索引 k（满足 z_k < z_hit）
        k = np.ceil((zvals - z0) / pitch - 1e-9).astype(np.int64) - 1
        valid = (k >= 0) & (k < nz)
        if not valid.any():
            continue
        flat = (idx_ray[valid] + start) * nz + k[valid]
        grid += np.bincount(flat, minlength=grid.size).astype(np.int32)

    events = grid.reshape(ncol, nz)
    # 从顶部向下累积，奇数次穿越 = 内部
    inside = (np.cumsum(events[:, ::-1], axis=1)[:, ::-1] % 2).astype(bool)
    return inside.reshape(nx, ny, nz)


def mesh_to_volume(mesh: trimesh.Trimesh, resolution: int = 160, pad: int = 3) -> dict:
    """把三角网格体素化成 SDF 体场。

    Parameters
    ----------
    mesh:
        三角网格（单位 mm）。
    resolution:
        最长边方向的体素数量（决定精度与耗时，128~256 是常用范围）。
    pad:
        各方向额外补的体素层数，保证等值面闭合。

    Returns
    -------
    dict: ``sdf`` (float32, shape=(nx,ny,nz))、``pitch``、``origin``、``bounds``
    """
    if len(mesh.faces) == 0:
        raise VolumeError("网格为空")
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    extents = bounds[1] - bounds[0]
    if not np.all(np.isfinite(extents)) or extents.max() <= 0:
        raise VolumeError("网格尺寸无效")

    resolution = int(max(16, min(400, resolution)))
    pitch = float(extents.max() / resolution)
    if not np.isfinite(pitch) or pitch <= 0:
        raise VolumeError("体素尺寸无效")

    origin = bounds[0] - pad * pitch
    shape = tuple(int(np.ceil(e / pitch)) + 2 * pad for e in extents)
    nx, ny, nz = shape

    xs = origin[0] + (np.arange(nx) + 0.5) * pitch
    ys = origin[1] + (np.arange(ny) + 0.5) * pitch
    zs = origin[2] + (np.arange(nz) + 0.5) * pitch
    z_start = float(bounds[1][2] + pad * pitch + pitch)

    occ = _ray_occupancy(mesh, xs, ys, zs, z_start)
    if not occ.any():
        raise VolumeError("体素化结果为空：模型可能不是闭合实体，或尺寸异常")
    if occ.mean() > 0.999:
        raise VolumeError("体素化结果几乎全是实体：模型法向可能反向")

    inside_dist = ndimage.distance_transform_edt(occ, sampling=pitch)
    outside_dist = ndimage.distance_transform_edt(~occ, sampling=pitch)
    half = 0.5 * pitch
    sdf = np.where(occ, -(inside_dist - half), outside_dist - half)

    # 边界保护（实体自身不该贴到体场边界，这里兜底）
    sdf = boundary_guard(sdf, pitch)

    sdf = np.ascontiguousarray(sdf.astype(np.float32))
    return {
        "sdf": sdf,
        "pitch": float(pitch),
        "origin": origin.astype(np.float64),
        "bounds": bounds,
        "resolution": int(max(shape)),
        "shape": tuple(int(v) for v in shape),
        "fill_ratio": float(occ.mean()),
    }


def boundary_guard(field: np.ndarray, pitch: float, voxels: float = 1.6) -> np.ndarray:
    """把体场最外 ``voxels`` 层强制为"外部"，保证等值面在体场内部闭合。

    任何让材料顶到体场边界的组合（典型是 ``union`` 里晶格填满包围盒）都必须
    经过这一步，否则 marching cubes 会在边界处留下开口。
    """
    nx, ny, nz = field.shape
    ax = np.arange(nx, dtype=np.float32)
    ay = np.arange(ny, dtype=np.float32)
    az = np.arange(nz, dtype=np.float32)
    edge = ax[:, None, None]
    edge = np.minimum(edge, (nx - 1 - ax)[:, None, None])
    edge = np.minimum(edge, ay[None, :, None])
    edge = np.minimum(edge, (ny - 1 - ay)[None, :, None])
    edge = np.minimum(edge, az[None, None, :])
    edge = np.minimum(edge, (nz - 1 - az)[None, None, :])
    edge = (edge * np.float32(pitch)).astype(np.float32)
    return np.maximum(field, np.float32(voxels * pitch) - edge)


def save_volume(volume: dict, path) -> None:
    np.savez_compressed(
        str(path),
        sdf=volume["sdf"],
        pitch=volume["pitch"],
        origin=volume["origin"],
        bounds=volume["bounds"],
        resolution=volume["resolution"],
    )


def load_volume(path) -> dict:
    with np.load(str(path)) as data:
        sdf = data["sdf"]
        return {
            "sdf": sdf,
            "pitch": float(data["pitch"]),
            "origin": np.asarray(data["origin"], dtype=np.float64),
            "bounds": np.asarray(data["bounds"], dtype=np.float64),
            "resolution": int(data["resolution"]),
            "shape": tuple(int(v) for v in sdf.shape),
        }


# --------------------------------------------------------------------------- #
# 体场 → 网格
# --------------------------------------------------------------------------- #
def volume_to_mesh(
    field: np.ndarray,
    pitch: float,
    origin,
    level: float = 0.0,
    smooth_iters: int = 0,
    decimate_ratio: float = 0.0,
    decimate_target_faces: int | None = None,
) -> trimesh.Trimesh:
    """marching cubes 提取等值面 → 可选平滑 / 抽稀。"""
    from skimage import measure

    field = np.ascontiguousarray(field, dtype=np.float32)
    if not np.isfinite(field).all():
        field = np.nan_to_num(field, nan=1.0, posinf=1.0, neginf=-1.0)
    if field.min() > level or field.max() < level:
        raise VolumeError("体场中没有可提取的等值面（参数过于极端或壁厚小于体素）")

    verts, faces, _normals, _values = measure.marching_cubes(
        field, level=float(level), spacing=(float(pitch),) * 3
    )
    verts = (
        np.asarray(verts, dtype=np.float64)
        + np.asarray(origin, dtype=np.float64)[None, :]
        + 0.5 * pitch
    )
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.remove_unreferenced_vertices()

    if smooth_iters and smooth_iters > 0:
        try:
            trimesh.smoothing.filter_taubin(mesh, lamb=0.55, nu=-0.6, iterations=int(smooth_iters))
        except Exception:  # noqa: BLE001
            pass

    if decimate_target_faces or decimate_ratio:
        try:
            if decimate_target_faces:
                target = int(decimate_target_faces)
            else:
                target = max(1000, int(len(mesh.faces) * (1.0 - float(decimate_ratio))))
            if target < len(mesh.faces):
                mesh = mesh.simplify_quadric_decimation(face_count=target)
        except Exception:  # noqa: BLE001
            pass

    mesh.process(validate=True)
    return mesh


def occupancy_stats(field: np.ndarray, pitch: float) -> dict:
    """从体场估算体积/密度。"""
    solid = field <= 0.0
    voxel_vol = float(pitch) ** 3
    return {
        "voxels": int(solid.sum()),
        "volume": float(solid.sum()) * voxel_vol,
        "bbox_volume": float(solid.size) * voxel_vol,
    }
