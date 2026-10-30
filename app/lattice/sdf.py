"""窄带精确有符号距离场 + 小特征诊断。

`voxel.mesh_to_volume` 原本的距离场来自「占据栅格的欧氏距离变换（EDT）」：
距离是按**体素中心量化**的，最大误差 ±0.5 个体素。pitch = 0.5 mm 时就是
±0.25 mm —— 对 Ø2 mm 的孔来说相当于半径误差 25%，孔壁会被明显磨圆，
孔小到 1~2 个体素时干脆糊死。

这里把表面附近 ``band`` 个体素内的距离换成**点到三角形的精确距离**，远场仍用
EDT（那里误差对等值面没有影响），两者在窄带边缘线性过渡。配合浏览器端
Surface Nets + Newton 投影，等值面位置误差从 O(h) 降到 O(h²)。

同时提供 :func:`feature_report`：在辅助低分辨率栅格上量出模型里**最窄的通道
半径**（封闭内腔 + 通孔/窄槽两种都算），据此给出「分辨率够不够 / 建议多少」的
判断 —— 这是毫米级小孔能不能保真的直接依据。
"""

from __future__ import annotations

import numpy as np

#: 精确窄带默认宽度（单位：体素）
DEFAULT_BAND = 3.0

#: 一次处理多少个 (体素, 三角形) 对，控制峰值内存
_PAIR_CHUNK = 400_000


# --------------------------------------------------------------------------- #
# 点到三角形距离（按"配对"向量化：p/a/b/c 等长）
# --------------------------------------------------------------------------- #
def _point_triangle_pairs(
    p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray
) -> np.ndarray:
    """逐对计算点到三角形的最短距离（Ericson《Real-Time Collision Detection》5.1.5）。

    所有入参形状均为 ``(P, 3)``，返回 ``(P,)``。
    """
    ab = b - a
    ac = c - a
    ap = p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)

    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)

    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)

    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2

    # 顶点区域（后写的覆盖先写的）
    dist = np.einsum("ij,ij->i", ap, ap)
    np.copyto(dist, np.einsum("ij,ij->i", bp, bp), where=(d3 >= 0) & (d4 <= d3))
    np.copyto(dist, np.einsum("ij,ij->i", cp, cp), where=(d6 >= 0) & (d5 <= d6))

    # 边 ab
    m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    if m.any():
        denom = d1[m] - d3[m]
        t = np.divide(d1[m], denom, out=np.zeros_like(denom), where=np.abs(denom) > 1e-30)
        q = a[m] + t[:, None] * ab[m]
        dist[m] = np.einsum("ij,ij->i", p[m] - q, p[m] - q)

    # 边 ac
    m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    if m.any():
        denom = d2[m] - d6[m]
        t = np.divide(d2[m], denom, out=np.zeros_like(denom), where=np.abs(denom) > 1e-30)
        q = a[m] + t[:, None] * ac[m]
        dist[m] = np.einsum("ij,ij->i", p[m] - q, p[m] - q)

    # 边 bc
    m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    if m.any():
        denom = (d4[m] - d3[m]) + (d5[m] - d6[m])
        t = np.divide(
            d4[m] - d3[m], denom, out=np.zeros_like(denom), where=np.abs(denom) > 1e-30
        )
        q = b[m] + t[:, None] * (c[m] - b[m])
        dist[m] = np.einsum("ij,ij->i", p[m] - q, p[m] - q)

    # 面内
    m = (va >= 0) & (vb >= 0) & (vc >= 0)
    if m.any():
        denom = va[m] + vb[m] + vc[m]
        safe = np.abs(denom) > 1e-30
        denom = np.where(safe, denom, 1.0)
        v = vb[m] / denom
        w = vc[m] / denom
        q = a[m] + v[:, None] * ab[m] + w[:, None] * ac[m]
        pit = np.einsum("ij,ij->i", p[m] - q, p[m] - q)
        dist[m] = np.where(safe, pit, dist[m])

    return np.sqrt(np.maximum(dist, 0.0))


# --------------------------------------------------------------------------- #
# 窄带精确距离
# --------------------------------------------------------------------------- #
def exact_narrow_band(
    mesh,
    shape: tuple[int, int, int],
    origin: np.ndarray,
    pitch: float,
    band: float = DEFAULT_BAND,
    candidates: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """在窄带内计算**精确的**点到网格距离。

    Parameters
    ----------
    candidates:
        允许被精确化的体素掩码（``shape`` 形状的 bool）。传入用 EDT 结果粗筛出来的
        窄带能把工作量从「全体素」降到「表面附近一层」。``None`` 表示全部体素。

    Returns
    -------
    (dist, touched)
        ``dist``：``shape`` 形状的精确距离，未计算处为 ``np.inf``；
        ``touched``：哪些体素拿到了精确值（供调用方做 EDT → 精确的过渡）。
    """
    nx, ny, nz = (int(v) for v in shape)
    origin = np.asarray(origin, dtype=np.float64).ravel()[:3]
    empty = (np.full(shape, np.inf, dtype=np.float64), np.zeros(shape, dtype=bool))

    tris = np.asarray(mesh.triangles, dtype=np.float64)
    if tris.size == 0:
        return empty

    # 退化三角形会让距离公式出现 0/0
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    tris = tris[np.linalg.norm(normals, axis=1) > 1e-12]
    if tris.size == 0:
        return empty

    tri_lo = tris.min(axis=1)
    tri_hi = tris.max(axis=1)
    tri_size = float(np.median(np.max(tri_hi - tri_lo, axis=1)))

    # 空间分桶的格子边长：
    #   * 下界 3 个体素（太小则桶数爆炸、内存吃紧）；
    #   * 三角形细分很密（tri_size 小）时放大到 2×三角面尺寸，减少"每个体素都要
    #     和一大票小三角形比距离"的开销；
    #   * 上界 6 个体素 —— 大平面（几十毫米一个三角形）绝不能让格子跟着变大，
    #     否则一个桶里塞下所有三角形，配对数会变成 体素数 × 三角形数。
    cell = min(max(3.0 * pitch, 2.0 * tri_size), 6.0 * pitch)
    pad = band * pitch * 1.05

    def buckets(cell_size: float):
        lo = np.floor((tri_lo - pad) / cell_size).astype(np.int64)
        hi = np.floor((tri_hi + pad) / cell_size).astype(np.int64)
        span = hi - lo + 1
        return lo, span, span[:, 0] * span[:, 1] * span[:, 2]

    lo, span, per_tri = buckets(cell)
    # 巨三角形会把分桶展开量撑爆：逐步放大格子直到可接受
    for _ in range(8):
        if per_tri.sum() <= 6_000_000:
            break
        cell *= 1.8
        lo, span, per_tri = buckets(cell)

    tri_ids = np.repeat(np.arange(len(tris)), per_tri)
    offsets = np.repeat(np.cumsum(per_tri) - per_tri, per_tri)
    lin = np.arange(int(per_tri.sum()), dtype=np.int64) - offsets
    sx = span[tri_ids, 0]
    sy = span[tri_ids, 1]
    ix = lo[tri_ids, 0] + lin % sx
    iy = lo[tri_ids, 1] + (lin // sx) % sy
    iz = lo[tri_ids, 2] + lin // (sx * sy)

    # ---- 候选体素（窄带）----
    if candidates is None:
        vid = np.arange(nx * ny * nz, dtype=np.int64)
    else:
        vid = np.flatnonzero(np.asarray(candidates, dtype=bool).reshape(-1))
        if vid.size == 0:
            return empty
    vi, vj, vk = np.unravel_index(vid, shape)

    # 两边都用**世界坐标**分桶（同一个原点，否则桶编号会整体错位）
    vx = np.floor((origin[0] + (vi + 0.5) * pitch) / cell).astype(np.int64)
    vy = np.floor((origin[1] + (vj + 0.5) * pitch) / cell).astype(np.int64)
    vz = np.floor((origin[2] + (vk + 0.5) * pitch) / cell).astype(np.int64)

    kx_min = min(int(ix.min()), int(vx.min()))
    ky_min = min(int(iy.min()), int(vy.min()))
    kz_min = min(int(iz.min()), int(vz.min()))
    kx_span = max(int(ix.max()), int(vx.max())) - kx_min + 1
    ky_span = max(int(iy.max()), int(vy.max())) - ky_min + 1
    kz_span = max(int(iz.max()), int(vz.max())) - kz_min + 1

    def key_of(ax, ay, az):
        return ((ax - kx_min) * ky_span + (ay - ky_min)) * kz_span + (az - kz_min)

    tkey = key_of(ix, iy, iz)
    vkey = key_of(vx, vy, vz)

    t_order = np.argsort(tkey, kind="stable")
    tkey = tkey[t_order]
    tri_ids = tri_ids[t_order]

    v_order = np.argsort(vkey, kind="stable")
    vkey = vkey[v_order]

    pair_a, pair_b = _match_sorted_keys(vkey, tkey)
    if pair_a.size == 0:
        return empty

    vox_sel = v_order[pair_a]
    vox_idx = vid[vox_sel]
    tri_idx = tri_ids[pair_b]

    pts = np.column_stack(
        [
            origin[0] + (vi[vox_sel] + 0.5) * pitch,
            origin[1] + (vj[vox_sel] + 0.5) * pitch,
            origin[2] + (vk[vox_sel] + 0.5) * pitch,
        ]
    )

    # ---- 逐块算距离，只保留窄带内的结果（更远的交给 EDT）----
    # 先做一次「点到三角形 AABB」的下界筛选：分桶是按格子做的，一个格子里可能
    # 塞着几百个三角形（典型是圆柱孔那一片加密的小面），不做这步会有十倍以上的
    # 无效距离计算。
    limit = band * pitch * 1.02
    idx_parts: list[np.ndarray] = []
    val_parts: list[np.ndarray] = []
    for start in range(0, vox_idx.size, _PAIR_CHUNK):
        sl = slice(start, start + _PAIR_CHUNK)
        gt_idx = tri_idx[sl]
        pp = pts[sl]
        gap = np.maximum(np.maximum(tri_lo[gt_idx] - pp, pp - tri_hi[gt_idx]), 0.0)
        lower = np.sqrt(np.einsum("ij,ij->i", gap, gap))
        near = lower <= limit
        if not near.any():
            continue
        pp = pp[near]
        gt = tris[gt_idx[near]]
        d = _point_triangle_pairs(pp, gt[:, 0], gt[:, 1], gt[:, 2])
        keep = d <= limit
        if keep.any():
            idx_parts.append(vox_idx[sl][near][keep])
            val_parts.append(d[keep])

    if not idx_parts:
        return empty

    flat_idx = np.concatenate(idx_parts)
    flat_val = np.concatenate(val_parts)

    order = np.argsort(flat_idx, kind="stable")
    flat_idx = flat_idx[order]
    flat_val = flat_val[order]
    starts = np.flatnonzero(np.r_[True, flat_idx[1:] != flat_idx[:-1]])

    best = np.full(nx * ny * nz, np.inf)
    best[flat_idx[starts]] = np.minimum.reduceat(flat_val, starts)

    touched = np.isfinite(best)
    return best.reshape(shape), touched.reshape(shape)


def _match_sorted_keys(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """两个**升序** key 数组求交集配对（同一 key 内做笛卡尔积），返回下标对。"""
    if a.size == 0 or b.size == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    ua, a_start, a_count = np.unique(a, return_index=True, return_counts=True)
    b_start = np.searchsorted(b, ua, side="left")
    b_end = np.searchsorted(b, ua, side="right")
    b_count = b_end - b_start
    hit = b_count > 0
    if not hit.any():
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    a_start = a_start[hit]
    a_count = a_count[hit]
    b_start = b_start[hit]
    b_count = b_count[hit]

    rep = a_count * b_count
    total = int(rep.sum())
    if total == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    group = np.repeat(np.arange(rep.size), rep)
    local = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(rep) - rep, rep)
    pa = a_start[group] + local // b_count[group]
    pb = b_start[group] + local % b_count[group]
    return pa, pb


# --------------------------------------------------------------------------- #
# 小特征诊断
# --------------------------------------------------------------------------- #
def feature_report(
    field: np.ndarray,
    pitch: float,
    extent_max: float,
    bounds: np.ndarray | None = None,
    origin: np.ndarray | None = None,
    scan_resolution: int = 160,
) -> dict:
    """量出模型里**最窄的通道半径**，并给出分辨率建议。

    输入用**有符号距离场**而不是占据栅格：空腔里 SDF 的最大值就是该空腔的内切
    半径（精确到亚体素，因为窄带内的距离是真的点到三角面距离），比在占据栅格上
    做 EDT 准得多 —— 后者在 pitch≈1 mm 时误差能到 ±50%。

    两种通道都算：

    * **封闭内腔**（不与体场边界连通）—— 连通域内 SDF 最大值；
    * **通孔 / 窄槽**（与外部连通）—— 把"包围盒之外"当作材料再取空腔，孔道两端
      被盒壁封住，于是成为独立连通域，其 SDF 最大值就是孔径。

    Parameters
    ----------
    field:
        有符号距离场（<=0 为材料），形状 ``(nx, ny, nz)``。
    pitch:
        体素边长（mm）。
    extent_max:
        模型最长边（mm），用于把"需要的体素尺寸"换算成分辨率。
    bounds, origin:
        模型包围盒与体场原点（mm）；给了才能算通孔。
    scan_resolution:
        诊断用栅格的最高分辨率（只影响诊断精度，不影响生成结果）。
    """
    from scipy import ndimage

    field = np.asarray(field, dtype=np.float32)
    nx, ny, nz = field.shape

    step = max(1, int(np.ceil(max(nx, ny, nz) / max(32, int(scan_resolution)))))
    sub = field[::step, ::step, ::step] if step > 1 else field
    sub_pitch = pitch * step

    inside = sub <= 0.0
    # 体素化可能在空腔里留下孤立的"材料"碎点，把孔道切成几段（每段的最大 SDF
    # 都偏小，量出来就不是孔径了）。先做一次 1 体素的闭运算把碎点吃掉。
    inside = ndimage.binary_closing(inside, iterations=1)
    void = ~inside
    labels, count = ndimage.label(void)
    result: dict = {
        "scan_pitch": round(float(sub_pitch), 4),
        "cavities": 0,
        "min_hole_radius": None,
        "min_feature_mm": None,
        "recommended_resolution": None,
        "hole_to_pitch_ratio": None,
        "warnings": [],
    }
    if count == 0:
        return result

    exterior = set(np.unique(labels[0]).tolist())
    exterior.update(np.unique(labels[-1]).tolist())
    exterior.update(np.unique(labels[:, 0]).tolist())
    exterior.update(np.unique(labels[:, -1]).tolist())
    exterior.update(np.unique(labels[:, :, 0]).tolist())
    exterior.update(np.unique(labels[:, :, -1]).tolist())
    exterior.discard(0)

    radii: list[float] = []

    def add_from(lab: np.ndarray, label_id: int, min_size: int) -> None:
        mask = lab == label_id
        if int(mask.sum()) < min_size:
            return
        # 空腔里 SDF 的最大值 ≈ 内切半径，但栅格化的空腔边界比真实孔壁"靠里"
        # 半个体素（占据是按体素中心判的），这里补回来 —— 补偿后实测 Ø2 mm 孔在
        # pitch = 0.2~0.5 mm 下都稳定给出 0.9 mm 左右（真值 1.0）。
        peak = float(sub[mask].max())
        radius = min(peak + 0.5 * sub_pitch, peak * 1.35)
        if radius > 0:
            radii.append(radius)

    # (a) 封闭内腔
    for label_id in range(1, count + 1):
        if label_id in exterior:
            continue
        before = len(radii)
        add_from(labels, label_id, 4)
        if len(radii) > before:
            result["cavities"] += 1

    # (b) 通孔 / 窄槽：把"包围盒之外"当成材料再取空腔
    if bounds is not None and origin is not None:
        lo = np.asarray(bounds[0], dtype=np.float64).ravel()[:3]
        hi = np.asarray(bounds[1], dtype=np.float64).ravel()[:3]
        org = np.asarray(origin, dtype=np.float64).ravel()[:3]
        margin = 0.5 * sub_pitch
        idx_lo = np.ceil((lo + margin - org) / sub_pitch - 0.5).astype(np.int64)
        idx_hi = np.floor((hi - margin - org) / sub_pitch - 0.5).astype(np.int64)
        idx_lo = np.clip(idx_lo, 0, np.array(sub.shape) - 1)
        idx_hi = np.clip(idx_hi, 0, np.array(sub.shape) - 1)
        box = np.zeros(sub.shape, dtype=bool)
        box[
            int(idx_lo[0]) : int(idx_hi[0]) + 1,
            int(idx_lo[1]) : int(idx_hi[1]) + 1,
            int(idx_lo[2]) : int(idx_hi[2]) + 1,
        ] = True

        labels2, count2 = ndimage.label(~(inside | ~box))
        for label_id in range(1, count2 + 1):
            add_from(labels2, label_id, 4)

    if not radii:
        return result

    smallest = min(radii)
    result["min_hole_radius"] = round(smallest, 4)
    result["min_feature_mm"] = round(smallest * 2.0, 4)
    # 目标：孔径至少占 4 个体素 → 需要的体素边长 = 孔径 / 4
    need_pitch = max(smallest * 2.0 / 4.0, 1e-6)
    recommended = int(np.clip(np.ceil(extent_max / need_pitch), 48, 400))
    result["recommended_resolution"] = recommended
    if pitch > 0:
        ratio = float(smallest * 2.0 / pitch)
        result["hole_to_pitch_ratio"] = round(ratio, 3)
        if ratio < 4.0:
            result["warnings"].append(
                f"最小孔 ⌀{smallest * 2:.2f} mm 只有 {ratio:.1f} 个体素宽，"
                f"等值面会明显失真，建议分辨率 ≥ {recommended}"
            )
    return result
