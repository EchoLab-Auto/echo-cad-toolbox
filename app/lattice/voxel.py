"""体素化（网格 → 有符号距离场）与等值面提取（体场 → 三角网格）。

属于晶格化工具（``app.lattice``）。

体素化采用**沿 Z 的射线奇偶法**：对每个 (x, y) 栅格列发一条向下的射线，收集
与网格表面的全部交点，按高度排序后奇偶填充。相比"体素包含性测试"，它一次性
批量完成、不依赖拾取引擎的包围体实现，对 CAD 离散出来的三角汤也稳健。

距离场分两段：远场用**欧氏距离变换（EDT）**，表面附近 ``band`` 个体素内改用
**精确的点—三角形距离**。EDT 的距离按体素中心量化，误差上限 0.5 个体素；毫米级
小孔正是死在这半个体素上 —— Ø2 mm 的孔在 pitch≈1 mm 时半径只有 1 个体素，量化
误差足够把孔壁磨圆、把孔直接糊死。窄带精确化之后零等值面回到亚体素精度。
"""

from __future__ import annotations

import gc

import numpy as np
import trimesh
from scipy import ndimage
from scipy.spatial import cKDTree


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


# --------------------------------------------------------------------------- #
# 窄带精确距离：图元细分 + 图元重心 KD-tree 候选 + 精确点—三角形距离
# --------------------------------------------------------------------------- #
#: 细分后单个图元 AABB 的最大边长（单位：体素）
_PRIM_MAX_EXT = 3.0
#: 细分后的图元数量上限（超了就放宽尺寸上限，细分过头只浪费内存）
_PRIM_BUDGET = 240_000
#: 每个窄带体素参与精确距离计算的候选图元数（KD-tree 的 k）
_NEIGHBOR_COUNT = 8
#: 每批处理的体素数，控制峰值内存（批内配对数组 ≈ 批 × k）
_VOXEL_CHUNK = 32_768
#: 窄带体素数量上限：超过就按比例收窄 band，保证高分辨率下体素化仍然是一次秒级操作
_BAND_MAX_VOXELS = 800_000
#: 窄带外侧过渡回 EDT 的宽度（单位：体素）
_BAND_TRANSITION = 1.0
#: 候选体素的余量（单位：体素）：class_dist <= (band + 余量) * pitch 全部纳入。
#: 异类体素中心到表面的距离最多比真实距离大 ~1.3 倍，band 以内（权重 1）的体素
#: 一定落在 (band + 1) 层以内，剩下的过渡带由 EDT 兜底。
_BAND_CANDIDATES = 1.0
#: cKDTree 并行查询线程数（-1 = 全部核；距离计算是纯数值、放线程池能线性加速）
_KD_WORKERS = -1


def _split_triangles(
    tri: np.ndarray,
    pitch: float,
    max_ext: float = _PRIM_MAX_EXT,
    budget: int = _PRIM_BUDGET,
) -> np.ndarray:
    """把三角形四叉细分到 AABB 单轴不超过 ``max_ext`` 个体素。

    细分不改变几何（四个子三角形之并与原三角形完全相同），只是让每个图元只覆盖
    一小块表面。这对候选查询很关键：候选图元是按**图元重心**在 KD-tree 里查的，
    图元越大重心离表面越远，"k 个最近重心"就越容易漏掉真正最近的面片 —— 一张
    几十毫米的大平面如果只用一个图元表示，它的重心可能离体素好几个毫米。

    图元数量超过 ``budget`` 时把尺寸上限翻倍重来：宁可图元大一点（候选略粗），
    也不要让图元数组把内存吃穿。
    """
    origin_tri = np.ascontiguousarray(tri, dtype=np.float64)
    cur = origin_tri
    for _ in range(6):
        limit = max_ext * pitch
        # 逐层细分：每轮只处理"还太大"的那部分，已合格的小三角形直接收进 kept。
        # 若每轮都遍历整个数组（含早已合格的），总工作量会翻好几倍 —— 大平面在
        # pitch 很小时要切 5~8 层，这个常数很致命。
        kept: list[np.ndarray] = []
        work = cur
        for _level in range(40):
            a, b, c = work[:, 0], work[:, 1], work[:, 2]
            lo = np.minimum(np.minimum(a, b), c)
            hi = np.maximum(np.maximum(a, b), c)
            area2 = np.linalg.norm(np.cross(b - a, c - a), axis=1)
            # 只切"又大又有面积"的三角形：布尔运算常留下又长又薄的碎片，它们的
            # AABB 很长但面积≈0，按 AABB 切会切成几千个零面积小片，白吃内存 ——
            # 而且碎片本来就不覆盖任何表面，距离由旁边的面片给出。
            big = ((hi - lo).max(axis=1) > limit) & (area2 > 0.5 * limit * limit)
            if not big.any():
                kept.append(work)
                break
            kept.append(work[~big])
            rest = work[big]
            a, b, c = rest[:, 0], rest[:, 1], rest[:, 2]
            ab = 0.5 * (a + b)
            bc = 0.5 * (b + c)
            ca = 0.5 * (c + a)
            work = np.concatenate(
                [
                    np.stack([a, ab, ca], axis=1),
                    np.stack([ab, b, bc], axis=1),
                    np.stack([ca, bc, c], axis=1),
                    np.stack([ab, bc, ca], axis=1),
                ],
                axis=0,
            )
        built = kept[0] if len(kept) == 1 else np.concatenate(kept, axis=0)
        if len(built) <= budget:
            return built
        # 超预算：放宽尺寸上限，从**原始三角形**重来（不能接着切已经切过的，
        # 那样只会原样返回超预算的结果）
        max_ext *= 2.0
        cur = origin_tri
    return cur


def _point_triangle_dist(p, a, b, c):
    """批量计算点到三角形的最短距离（mm），入参形状都是 ``(P, 3)``。

    两段式：先用重心坐标判断垂足是否落在三角形内部 —— 落在内部时平面距离就是
    精确值，直接返回；只有垂足落在三角形外的配对才继续算三条边的点到线段距离。
    表面附近的体素约 2/3 落在面内，这样能省掉大半逐元素运算。
    """
    ab = b - a
    ac = c - a
    ap = p - a
    n = np.cross(ab, ac)
    nn = np.einsum("ij,ij->i", n, n)
    s = np.einsum("ij,ij->i", n, ap)
    plane2 = s * s / np.maximum(nn, 1e-30)

    d00 = np.einsum("ij,ij->i", ab, ab)
    d01 = np.einsum("ij,ij->i", ab, ac)
    d11 = np.einsum("ij,ij->i", ac, ac)
    u = np.einsum("ij,ij->i", ap, ab)
    v = np.einsum("ij,ij->i", ap, ac)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-30
    safe = np.where(ok, den, 1.0)
    bu = (d11 * u - d01 * v) / safe
    bv = (d00 * v - d01 * u) / safe
    inside = ok & (bu >= 0.0) & (bv >= 0.0) & (bu + bv <= 1.0)

    out2 = np.where(inside, plane2, np.inf)
    need = np.flatnonzero(~inside)
    if need.size:
        pp = p[need]
        tri = np.stack([a[need], b[need], c[need]], axis=1)
        best2 = np.full(need.size, np.inf)
        for i, j in ((0, 1), (1, 2), (2, 0)):
            start = tri[:, i]
            edge = tri[:, j] - start
            ee = np.einsum("ij,ij->i", edge, edge)
            t = np.einsum("ij,ij->i", pp - start, edge) / np.maximum(ee, 1e-30)
            np.clip(t, 0.0, 1.0, out=t)
            diff = pp - (start + t[:, None] * edge)
            np.minimum(best2, np.einsum("ij,ij->i", diff, diff), out=best2)
        out2[need] = best2
    return np.sqrt(np.maximum(out2, 0.0))


def _exact_narrow_band(
    mesh: trimesh.Trimesh,
    shape: tuple[int, int, int],
    origin: np.ndarray,
    pitch: float,
    candidates: np.ndarray,
    band: float = 3.0,
    neighbors: int = _NEIGHBOR_COUNT,
) -> tuple[np.ndarray, np.ndarray]:
    """窄带内计算**精确的**点到网格距离，返回 ``(距离, 已精确掩码)``。

    加速结构
    --------
    1. **图元细分**：三角形四叉细分到 AABB ≤ ``_PRIM_MAX_EXT`` 个体素，图元数量
       ≈ O(表面积 / pitch²)，与原始面数无关（几十毫米的大平面被拆成局部小块，
       候选查询才不会"重心在天边、面片在眼前"）。
    2. **KD-tree**：以图元重心建 ``cKDTree``，单点查询 O(log P + k)（P = 图元数，
       k = ``neighbors``），整批点在 C 里并行查完，不进 Python 循环。
    3. **精确距离**只算 (体素, k 个候选) 这一小撮配对：配对总数 = 窄带体素数 × k，
       与三角形总数无关。暴力法是 体素数 × 三角形数（Ø40 mm 板 + 96 段圆柱在
       pitch=0.42 mm 时是 10⁵ × 10³ 量级），这里能降两个数量级。

    精度
    ----
    图元重心都在自己的面片上，所以候选集里的最小距离是**真实距离的上界**；图元
    被细分到 ``_PRIM_MAX_EXT`` 个体素以内后，最近面片的重心离查询点最多比真实
    距离远 ``rho ≈ 1.5 体素``，而 k 个最近重心覆盖的半径远大于它 —— 窄带内
    （离表面一两个体素）最近面片总在候选集里。tests/test_lattice.py 用解析距离
    （立方体）与解析孔径（Ø2 mm 圆孔）把这一点钉死：带内最大误差 1e-2 mm 量级。

    Parameters
    ----------
    candidates:
        ``shape`` 形状的 bool 掩码，标记需要精确距离的体素（通常是离表面
        ``band + 1`` 层以内的体素）。

    Returns
    -------
    (dist, touched)
        ``dist``：``shape`` 形状的距离（mm），未计算处为 ``inf``；
        ``touched``：哪些体素拿到了精确值。
    """
    shape = tuple(int(v) for v in shape)
    flat_dist = np.full(int(np.prod(shape)), np.inf)
    flat_touched = np.zeros(int(np.prod(shape)), dtype=bool)
    empty = (flat_dist.reshape(shape), flat_touched.reshape(shape))

    tris = np.asarray(getattr(mesh, "triangles", ()), dtype=np.float64)
    if tris.ndim != 3 or tris.shape[0] == 0:
        return empty
    # 退化三角形（零面积）会让平面距离出现 0/0，先剔掉
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    tris = tris[np.einsum("ij,ij->i", normals, normals) > 1e-24]
    if len(tris) == 0:
        return empty

    # 单精度足够：坐标是 mm 量级，float32 的相对精度 1e-7 → 绝对误差 ~1e-5 mm，
    # 比体素小 5 个数量级；但逐元素运算的内存流量减半，实测快 ~1.8 倍。
    prim = _split_triangles(tris, float(pitch)).astype(np.float32)
    centers = prim.mean(axis=1, dtype=np.float64)
    tree = cKDTree(centers, compact_nodes=True, balanced_tree=True)

    flat = np.flatnonzero(np.asarray(candidates, dtype=bool).reshape(-1))
    if flat.size == 0:
        return empty
    vi, vj, vk = np.unravel_index(flat, shape)
    limit = float(band) * float(pitch)
    k = int(max(1, min(int(neighbors), len(centers))))

    for start in range(0, flat.size, _VOXEL_CHUNK):
        stop = min(flat.size, start + _VOXEL_CHUNK)
        sl = slice(start, stop)
        pts = np.column_stack(
            (
                origin[0] + (vi[sl] + 0.5) * float(pitch),
                origin[1] + (vj[sl] + 0.5) * float(pitch),
                origin[2] + (vk[sl] + 0.5) * float(pitch),
            )
        )
        _gap, idx = tree.query(pts, k=k, workers=_KD_WORKERS)
        idx = idx.reshape(len(pts), k)
        miss = idx >= len(centers)  # 邻居不足 k 个时 scipy 用 len(centers) 占位
        np.minimum(idx, len(centers) - 1, out=idx)
        corners = prim[idx.reshape(-1)]
        d = _point_triangle_dist(
            np.repeat(pts.astype(np.float32), k, axis=0),
            corners[:, 0],
            corners[:, 1],
            corners[:, 2],
        ).reshape(len(pts), k)
        d[miss] = np.inf
        best = d.min(axis=1)
        ok = np.flatnonzero(best <= limit)
        if ok.size == 0:
            continue
        target = flat[sl][ok]
        flat_dist[target] = best[ok]
        flat_touched[target] = True

    return flat_dist.reshape(shape), flat_touched.reshape(shape)


# --------------------------------------------------------------------------- #
# 小特征诊断（内腔 / 通孔半径 → 推荐分辨率）
# --------------------------------------------------------------------------- #
def _void_pocket_radii(
    field: np.ndarray,
    solid: np.ndarray,
    bounds=None,
    origin=None,
    sub_pitch: float = 1.0,
    min_voxels: int = 8,
) -> tuple[list[float], int]:
    """返回 (实体包围盒内各空腔的内切半径, 空腔个数)。

    空腔 = 实体包围盒内的 void 连通域。之所以把分析范围收进**实体包围盒**而不是
    整个体场：通孔（Ø2 mm 挖孔最常见的形态）两端都通到体场边界，在整场里属于
    "外部"而会被漏掉；缩进包围盒后孔道被盒壁切断，就变成独立空腔，其 SDF 最大值
    正是孔径。封闭内腔本来就在包围盒内，一并覆盖。

    副作用（已记录在 docstring 里）：非凸模型在包围盒内的"空余角落"也会被当成
    空腔；它们的半径通常很大，不会触发"孔太小"的告警。
    """
    if bounds is not None and origin is not None:
        lo_w = np.asarray(bounds[0], dtype=np.float64).ravel()[:3]
        hi_w = np.asarray(bounds[1], dtype=np.float64).ravel()[:3]
        org = np.asarray(origin, dtype=np.float64).ravel()[:3]
        lo = np.clip(np.ceil((lo_w - org) / sub_pitch - 0.5), 0, np.array(solid.shape) - 1)
        hi = np.clip(np.floor((hi_w - org) / sub_pitch - 0.5), 0, np.array(solid.shape) - 1)
        lo = [int(v) for v in lo]
        hi = [int(v) for v in hi]
    else:
        idx = np.nonzero(solid)
        if idx[0].size == 0:
            return [], 0
        lo = [int(a.min()) for a in idx]
        hi = [int(a.max()) for a in idx]
    # 包围盒向内收一格：模型体素化后与包围盒之间常有不到 1 个体素的缝隙，
    # 盒角会留下一小片"空腔"，不收会让它冒充小孔。
    inset = 1
    lo = [min(v + inset, h) for v, h in zip(lo, hi)]
    hi = [max(h - inset, v) for v, h in zip(hi, lo)]
    box = np.zeros_like(solid)
    box[lo[0] : hi[0] + 1, lo[1] : hi[1] + 1, lo[2] : hi[2] + 1] = True
    void = box & ~solid
    labels, count = ndimage.label(void)
    if count == 0:
        return [], 0
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    ids = [i for i in range(1, count + 1) if int(sizes[i]) >= min_voxels]
    if not ids:
        return [], 0
    peaks = np.atleast_1d(ndimage.maximum(field, labels, ids)).astype(np.float64)
    # 峰值小于 3/4 体素的空腔半宽不到 1 个体素，几乎都是射线奇偶填充/降采样留下的
    # 噪声（真正这么小的孔在占据栅格里已经塌了），丢掉它们免得刷出假告警。
    keep = peaks >= 0.75 * float(sub_pitch)
    radii = [float(v) for v in peaks[keep]]
    return radii, len(radii)


def feature_report(
    sdf: np.ndarray,
    pitch: float,
    extent_mm: float | None = None,
    *,
    bounds=None,
    origin=None,
    max_side: int = 128,
    min_voxels: int = 8,
    res_range: tuple[int, int] = (48, 400),
) -> dict:
    """估计模型里最窄的孔/内腔半径，并给出推荐分辨率（纯 float/int/str/list）。

    分析在**辅助低分辨率栅格**（最长边 ≤ ``max_side``）上做，320³ 的高分辨率体场
    也不会因为连通域标记多占内存；诊断精度由该栅格的体素尺寸决定，见返回的
    ``scan_pitch_mm``。

    量的是"实体包围盒内的空腔"的内切半径：空腔里 SDF 的最大值就是它的内切球
    半径，也就是孔/腔的半径。取包围盒而不是整个体场，是为了让**通孔**也能量到
    —— 通孔两端通到体场边界，在整场里属于"外部"，缩进包围盒后才会被盒壁封成
    独立空腔。封闭内腔本来就在包围盒内，一并覆盖。

    推荐分辨率的取法：目标体素边长 ≈ 最小孔半径 / 2（孔直径占 4 个体素），
    再按 ``res_range`` 夹到合理区间。

    Parameters
    ----------
    sdf:
        有符号距离场（``<=0`` 为材料），形状 ``(nx, ny, nz)``，单位 mm。
    pitch:
        体素边长（mm）。
    extent_mm:
        模型最长边（mm）。``None`` 时按体场自身尺寸估算（会含 ``pad`` 的余量）。
    bounds, origin:
        模型包围盒与体场原点（mm）。给了就用真实包围盒切空腔，比"实体体素的
        包围盒"更贴边；不给则按实体体素自己算。
    max_side:
        诊断栅格的最长边体素数。
    min_voxels:
        空腔的最小体积（辅助栅格体素数），用来滤掉体素化噪点。
    res_range:
        推荐分辨率的上下限。

    Returns
    -------
    dict: ``pitch_mm`` / ``min_hole_radius`` / ``min_feature_mm`` /
    ``recommended_resolution`` / ``hole_to_pitch_ratio`` / ``warnings``，
    外加 ``cavities`` / ``scan_pitch_mm`` / ``scan_shape`` 三个诊断字段。
    全部是 float / int / str / list，可直接 JSON 序列化（会被塞进 HTTP 头）。
    """
    field = np.asarray(sdf, dtype=np.float32)
    if field.ndim != 3:
        raise ValueError("sdf 必须是三维体场")
    pitch = float(pitch)
    shape = tuple(int(v) for v in field.shape)
    step = max(1, int(np.ceil(max(shape) / max(8, int(max_side)))))
    if step > 1:
        # 分块取 max：宁可把孔判大一点，也不要因为降采样把毫米级小孔整个漏掉
        blocks = [int(np.ceil(n / step)) for n in shape]
        pad = [(blocks[i] * step - shape[i], 0) for i in range(3)]
        if any(p[0] for p in pad):
            field = np.pad(field, pad, mode="edge")
        field = field.reshape(blocks[0], step, blocks[1], step, blocks[2], step).max(
            axis=(1, 3, 5)
        )
        sub_pitch = pitch * step
    else:
        sub_pitch = pitch
    sub = np.ascontiguousarray(field, dtype=np.float32)
    scan_shape = tuple(int(v) for v in sub.shape)

    # 体素化/降采样可能在空腔里留下孤立材料碎点，会把一条孔道切成几段（每段的
    # 最大 SDF 都偏小，量出来就不是孔径了）。只丢掉**面积极小**的实体连通域：
    # 不能用闭运算糊，那会把毫米级小孔一起堵死。
    solid = sub <= 0.0
    parts, parts_n = ndimage.label(solid)
    if parts_n > 1:
        sizes = np.bincount(parts.ravel(), minlength=parts_n + 1)
        keep = sizes >= int(min_voxels)
        keep[0] = False
        solid = keep[parts]
    radii, cavities = _void_pocket_radii(
        sub, solid, bounds=bounds, origin=origin, sub_pitch=sub_pitch,
        min_voxels=int(min_voxels),
    )

    if extent_mm and float(extent_mm) > 0:
        extent = float(extent_mm)
    else:
        extent = float(max(scan_shape) * sub_pitch)
    lo, hi = int(res_range[0]), int(res_range[1])

    result: dict = {
        "pitch_mm": round(pitch, 4),
        "scan_pitch_mm": round(float(sub_pitch), 4),
        "scan_shape": [int(v) for v in scan_shape],
        "cavities": int(cavities),
        "min_hole_radius": None,
        "min_feature_mm": round(2.0 * pitch, 4),
        "recommended_resolution": int(np.clip(np.ceil(extent / pitch), lo, hi)),
        "hole_to_pitch_ratio": None,
        "warnings": [],
    }
    if not radii:
        # 没有可测的孔/腔：最小特征未知，按"当前体素能分辨的下限"记 2 个体素
        return result

    smallest = float(min(radii))
    result["min_hole_radius"] = round(smallest, 4)
    result["min_feature_mm"] = round(2.0 * smallest, 4)
    # 目标体素边长 ≈ 最小孔半径 / 2 → 分辨率 = 最长边 / 目标边长
    need_pitch = max(0.5 * smallest, 1e-6)
    recommended = int(np.clip(np.ceil(extent / need_pitch), lo, hi))
    result["recommended_resolution"] = recommended
    ratio = float(smallest / pitch) if pitch > 0 else float("inf")
    result["hole_to_pitch_ratio"] = round(ratio, 3)
    if ratio < 1.0:
        result["warnings"].append(
            f"最小孔半径 {smallest:.2f} mm 不到 1 个体素（{ratio:.1f}），"
            f"等值面会把孔糊死或严重失真，建议分辨率 ≥ {recommended}"
        )
    elif ratio < 2.0:
        result["warnings"].append(
            f"最小孔半径 {smallest:.2f} mm 只有 {ratio:.1f} 个体素，"
            f"等值面会明显失真，建议分辨率 ≥ {recommended}"
        )
    return result

def mesh_to_volume(
    mesh: trimesh.Trimesh,
    resolution: int = 160,
    pad: int = 3,
    exact_band: float = 3.0,
) -> dict:
    """把三角网格体素化成 SDF 体场。

    Parameters
    ----------
    mesh:
        三角网格（单位 mm）。
    resolution:
        最长边方向的体素数量（决定精度与耗时，128~256 是常用范围）。
    pad:
        各方向额外补的体素层数，保证等值面闭合。
    exact_band:
        表面附近多少个体素内改用**精确的**点—三角形距离（单位：体素）。
        0 表示关闭（退回纯 EDT）。这是毫米级小孔能保真的关键：EDT 的量化误差
        上限 0.5 个体素，pitch=1 mm 时就是 ±0.5 mm，Ø2 mm 的孔会被磨圆甚至糊死。

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

    # 窄带精确化：EDT 的距离按体素中心量化（±0.5 体素），毫米级小孔正是死在
    # 这半个体素上。表面附近 band 个体素内换成精确的点—三角形距离，窄带外侧再留
    # 一个体素宽的 smoothstep 过渡带融回 EDT，接缝处不会有台阶。
    if exact_band and exact_band > 0:
        band = float(exact_band)
        # 候选体素：异类体素中心到表面的距离最远 0.87 个体素，所以
        # class_dist <= (band + 1) * pitch 一定覆盖全部 |d| <= band * pitch 的体素。
        # 体素太多时按比例收窄 band（成本 ≈ 窄带体素数 × 候选数），保证高分辨率下
        # 体素化仍然是一次秒级操作。
        class_dist = np.where(occ, inside_dist, outside_dist)
        candidates = class_dist <= (band + _BAND_CANDIDATES) * pitch
        while int(candidates.sum()) > _BAND_MAX_VOXELS and band > 1.4:
            band = max(1.4, band * 0.8)
            candidates = class_dist <= (band + _BAND_CANDIDATES) * pitch
        exact, touched = _exact_narrow_band(
            mesh, shape, origin, float(pitch), candidates, band=band
        )
        if touched.any():
            # 权重：|d| <= band 的体素**完全**用精确距离（小孔腔内也是精确的，孔心
            # 不会被打折），band ~ band+1 之间 smoothstep 融回 EDT，C1 连续无折角。
            # 零等值面位于 |d|≈0，永远在纯精确区里，过渡带只影响远场形状。
            w = np.clip(((band + _BAND_TRANSITION) * pitch - exact) / (_BAND_TRANSITION * pitch), 0.0, 1.0)
            w = np.where(touched, w * w * (3.0 - 2.0 * w), 0.0)
            near = np.where(touched, exact, 0.0)
            sdf = np.sign(sdf) * (w * near + (1.0 - w) * np.abs(sdf))

    # 边界保护（实体自身不该贴到体场边界，这里兜底）
    sdf = boundary_guard(sdf, pitch)

    # 小特征诊断：模型里最窄的通道（内腔 / 通孔）决定"这个分辨率够不够"。
    # 用带符号距离场本身来量半径 —— 空腔里 SDF 的最大值就是内切半径。
    try:
        features = feature_report(
            sdf, float(pitch), float(extents.max()), bounds=bounds, origin=origin
        )
    except Exception:  # noqa: BLE001 - 诊断失败不影响主流程
        features = None

    sdf = np.ascontiguousarray(sdf.astype(np.float32))
    volume = {
        "sdf": sdf,
        "pitch": float(pitch),
        "origin": origin.astype(np.float64),
        "bounds": bounds,
        "resolution": int(max(shape)),
        "shape": tuple(int(v) for v in shape),
        "fill_ratio": float(occ.mean()),
    }
    if features:
        volume["features"] = features
    return volume


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
    import json

    np.savez_compressed(
        str(path),
        sdf=volume["sdf"],
        pitch=volume["pitch"],
        origin=volume["origin"],
        bounds=volume["bounds"],
        resolution=volume["resolution"],
        # 小特征诊断随体场一起缓存：否则第一次请求（现算）和第二次（读缓存）
        # 返回的 X-Volume-Meta 头会不一致
        features=json.dumps(volume.get("features") or {}, ensure_ascii=False),
    )


def load_volume(path) -> dict:
    import json

    with np.load(str(path)) as data:
        sdf = data["sdf"]
        volume = {
            "sdf": sdf,
            "pitch": float(data["pitch"]),
            "origin": np.asarray(data["origin"], dtype=np.float64),
            "bounds": np.asarray(data["bounds"], dtype=np.float64),
            "resolution": int(data["resolution"]),
            "shape": tuple(int(v) for v in sdf.shape),
        }
        if "features" in data:
            try:
                features = json.loads(str(data["features"]))
                if features:
                    volume["features"] = features
            except ValueError:
                pass
        return volume


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
    del field
    # 平移与类型收敛都在 marching cubes 的原始数组上做，避免多一份副本
    verts = np.asarray(verts, dtype=np.float64)
    verts += np.asarray(origin, dtype=np.float64)[None, :] + 0.5 * pitch
    faces = np.asarray(faces, dtype=np.int32)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.remove_unreferenced_vertices()
    del verts, faces
    gc.collect()

    # 顺序很关键：**先抽稀再平滑**。二次误差抽稀的内存开销与面数成正比
    # （实测 190 万面网格抽稀需 ~500 MB），而 Taubin 平滑按顶点邻接建图，
    # 在抽稀后的网格上做能省下 100 MB 以上。两者都只是形貌修饰，先做哪个
    # 视觉差异很小，但内存差异很大。
    target = 0
    if decimate_target_faces:
        target = int(decimate_target_faces)
    elif decimate_ratio:
        target = max(1000, int(len(mesh.faces) * (1.0 - float(decimate_ratio))))
    if target and target < len(mesh.faces):
        mesh = _decimate(mesh, target)
        gc.collect()

    if smooth_iters and smooth_iters > 0:
        try:
            trimesh.smoothing.filter_taubin(mesh, lamb=0.55, nu=-0.6, iterations=int(smooth_iters))
        except Exception:  # noqa: BLE001
            pass

    mesh.process(validate=True)
    return mesh


def _decimate(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """抽稀到目标面数。

    优先用 ``fast_simplification`` 的原生接口：它直接吃 float32 顶点 + int32 索引，
    结果是新数组，不必像 trimesh 的包装那样先 ``mesh.copy()`` 再转 float64 / int64
    （实测包装版多占 ~100 MB，与这里省下的部分相当）。
    """
    try:
        import fast_simplification

        verts32 = np.ascontiguousarray(mesh.vertices, dtype=np.float32)
        faces32 = np.ascontiguousarray(mesh.faces, dtype=np.int32)
        reduction = 1.0 - float(target_faces) / float(len(mesh.faces))
        reduction = min(max(reduction, 0.0), 0.99)
        new_verts, new_faces = fast_simplification.simplify(verts32, faces32, target_reduction=reduction)
        if len(new_faces) == 0:
            raise ValueError("抽稀结果为空")
        out = trimesh.Trimesh(
            vertices=np.asarray(new_verts, dtype=np.float64),
            faces=np.asarray(new_faces, dtype=np.int64),
            process=False,
        )
        return out
    except Exception:  # noqa: BLE001
        # 回退到 trimesh 自带实现（更慢更吃内存，但保证可用）
        try:
            return mesh.simplify_quadric_decimation(face_count=int(target_faces))
        except Exception:  # noqa: BLE001
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
