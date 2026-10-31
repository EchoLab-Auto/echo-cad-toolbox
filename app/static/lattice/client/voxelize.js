/**
 * 浏览器端体素化：三角网格 → 有符号距离场（SDF）。
 *
 * 这是「把算力交给客户机」的核心一环。此前体素化只在服务端做（trimesh + scipy），
 * 于是分辨率被容器内存钳死（1.2 GiB → 128），⌀167 的零件 pitch 就有 1.31 mm，
 * 1.2 mm 的薄壁连一个体素都不到 —— 画出来满身尖刺。搬到浏览器后，吃的是用户
 * 自己的内存（一般 8~32 GB），res 320 甚至 400 都跑得动。
 *
 * 算法与服务端 ``app/lattice/voxel.py`` 对齐，保证两边结果可比：
 *
 *   1. 沿 -Z 逐列投射线做奇偶填充 → 占据栅格（用 XY 均匀网格加速求交）；
 *   2. 对占据/空腔分别做**精确**欧氏距离变换（Felzenszwalb 1D 三轴扫描）；
 *   3. 合成 SDF：``occ ? -(inside - pitch/2) : outside - pitch/2``；
 *   4. 表面附近一个窄带内改用精确的「点—三角形距离」，消掉 EDT 的 ±0.5 体素量化
 *      误差 —— 毫米级小孔全靠这一步才不会被磨圆。
 *
 * 全部用 typed array + 预分配，不用 push 堆对象，避免几百万体素时 GC 抖动。
 */

/** 窄带内改用精确距离的默认宽度（单位：体素） */
const DEFAULT_EXACT_BAND = 3.0;
/** 各方向额外补的体素层数，保证等值面闭合 */
const DEFAULT_PAD = 3;
/** 窄带候选体素的余量（单位：体素） */
const BAND_CANDIDATES = 1.0;
/** 窄带外侧过渡回 EDT 的宽度（单位：体素） */
const BAND_TRANSITION = 1.0;
/** 窄带体素数量上限，超过就按比例收窄 band（控制内存与耗时） */
const BAND_MAX_VOXELS = 400000;
/** 精确距离计算的分批大小 */
const VOXEL_CHUNK = 8192;

/** 「无穷远」哨兵。别用 1e20：加上 q² 后尾数会丢光，(inf - inf) 算出来是 0。 */
const INF = 1e20;
/** 判定"这条线上没有种子"的阈值 */
const IS_INF = 1e19;

export class VoxelizeError extends Error {}

/**
 * 把三角网格体素化成 SDF 体场。
 *
 * @param {{positions: Float32Array|number[], indices: Uint32Array|number[]}} mesh
 * @param {object} [options]
 * @param {number} [options.resolution=128] 最长边方向的体素数
 * @param {number} [options.pad=3]          各方向补的体素层数
 * @param {number} [options.exactBand=3]    窄带精确距离的宽度（体素），0 = 关闭
 * @param {(fraction: number, stage: string) => void} [options.onProgress]
 * @returns {{sdf: Float32Array, pitch: number, origin: number[], bounds: number[][],
 *            shape: number[], resolution: number, fillRatio: number,
 *            features: object|null}}
 */
export function voxelize(mesh, options = {}) {
  const {
    resolution = 128,
    pad = DEFAULT_PAD,
    exactBand = DEFAULT_EXACT_BAND,
    onProgress = null,
  } = options;

  const report = (f, s) => { if (onProgress) onProgress(f, s); };

  const positions = mesh.positions instanceof Float32Array
    ? mesh.positions : Float32Array.from(mesh.positions || []);
  const indices = mesh.indices instanceof Uint32Array
    ? mesh.indices : Uint32Array.from(mesh.indices || []);
  const nTri = (indices.length / 3) | 0;
  if (nTri === 0) throw new VoxelizeError('网格为空（没有三角形）');
  if (positions.length < 9) throw new VoxelizeError('网格顶点不足，无法体素化');

  // —— 包围盒 / 体素尺寸 ——
  const bounds = meshBounds(positions);
  const extents = [
    bounds[1][0] - bounds[0][0],
    bounds[1][1] - bounds[0][1],
    bounds[1][2] - bounds[0][2],
  ];
  const maxExtent = Math.max(extents[0], extents[1], extents[2]);
  if (!(maxExtent > 0) || !Number.isFinite(maxExtent)) {
    throw new VoxelizeError('网格尺寸无效（最长边为 0）');
  }
  const res = Math.max(16, Math.min(400, Math.round(resolution)));
  const pitch = maxExtent / res;

  // 各方向体素数（与服务端一致：ceil(extent/pitch) + 2*pad）
  const shape = extents.map((e) => Math.max(1, Math.ceil(e / pitch - 1e-9)) + pad * 2);
  const [nx, ny, nz] = shape;
  const origin = [
    bounds[0][0] - pad * pitch,
    bounds[0][1] - pad * pitch,
    bounds[0][2] - pad * pitch,
  ];

  // —— 1. 射线奇偶填充 ——
  report(0.02, '构建三角形索引');
  const index = buildXYGrid(positions, indices, nx, ny, origin, pitch);
  report(0.08, `射线填充（${nx}×${ny} 列）`);
  const occ = rayOccupancy(positions, indices, index, nx, ny, nz, origin, pitch,
    (f) => report(0.08 + 0.42 * f, '射线填充'));

  // —— 2. 内外欧氏距离 ——
  report(0.5, '距离变换');
  const insideDist = edtDistance(occ, shape, true, pitch,
    (f) => report(0.5 + 0.2 * f, '内部距离'));
  report(0.7, '空腔距离');
  const outsideDist = edtDistance(occ, shape, false, pitch,
    (f) => report(0.7 + 0.2 * f, '空腔距离'));

  // —— 3. 合成 SDF ——
  report(0.9, '合成距离场');
  const count = nx * ny * nz;
  const half = 0.5 * pitch;
  const sdf = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    sdf[i] = occ[i] ? -(insideDist[i] - half) : (outsideDist[i] - half);
  }

  // —— 4. 窄带精确化 ——
  if (exactBand > 0) {
    report(0.92, '窄带精确化');
    refineNarrowBand(positions, indices, sdf, shape, origin, pitch, exactBand,
      (f) => report(0.92 + 0.07 * f, '窄带精确化'));
  }

  let filled = 0;
  for (let i = 0; i < count; i++) if (occ[i]) filled++;

  report(1.0, '体素化完成');
  return {
    sdf,
    pitch,
    origin,
    bounds,
    shape,
    resolution: Math.max(nx, ny, nz),
    fillRatio: filled / count,
    features: null,
  };
}

/** 网格包围盒：`[[minx,miny,minz],[maxx,maxy,maxz]]` */
function meshBounds(positions) {
  let minX = Infinity; let minY = Infinity; let minZ = Infinity;
  let maxX = -Infinity; let maxY = -Infinity; let maxZ = -Infinity;
  for (let i = 0; i < positions.length; i += 3) {
    const x = positions[i]; const y = positions[i + 1]; const z = positions[i + 2];
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (z < minZ) minZ = z;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
    if (z > maxZ) maxZ = z;
  }
  return [[minX, minY, minZ], [maxX, maxY, maxZ]];
}

/* =========================================================================
 * 射线填充
 * ====================================================================== */

/**
 * 把三角形按 XY 包围盒塞进均匀网格，供逐列射线快速取候选。
 *
 * 每条射线沿 -Z 无限延伸，所以只需要三角形在 XY 平面的投影覆盖该列即可 —— 二维
 * 索引就够，不必上 BVH。⌀167 的盘在 res 320 下是 111k 列 × 16k 面，建索引后每列
 * 只有个位数候选，总求交次数从 1.8e9 降到 1e6 量级。
 */
function buildXYGrid(positions, indices, nx, ny, origin, pitch) {
  // 网格分辨率：让每格边长约 4 个体素，兼顾内存与候选数
  const cell = Math.max(4 * pitch, 1e-9);
  const gx = Math.max(1, Math.ceil((nx * pitch) / cell));
  const gy = Math.max(1, Math.ceil((ny * pitch) / cell));
  const nCell = gx * gy;

  const nTri = (indices.length / 3) | 0;
  const counts = new Uint32Array(nCell + 1);

  // 第一遍：数每格有多少三角形
  const triCellStart = new Uint32Array(nTri);
  const triCellCount = new Uint32Array(nTri);
  for (let t = 0; t < nTri; t++) {
    const i0 = indices[t * 3] * 3;
    const i1 = indices[t * 3 + 1] * 3;
    const i2 = indices[t * 3 + 2] * 3;
    const xs = [positions[i0], positions[i1], positions[i2]];
    const ys = [positions[i0 + 1], positions[i1 + 1], positions[i2 + 1]];
    const cx0 = clampInt(Math.floor((Math.min(...xs) - origin[0]) / cell), 0, gx - 1);
    const cx1 = clampInt(Math.floor((Math.max(...xs) - origin[0]) / cell), 0, gx - 1);
    const cy0 = clampInt(Math.floor((Math.min(...ys) - origin[1]) / cell), 0, gy - 1);
    const cy1 = clampInt(Math.floor((Math.max(...ys) - origin[1]) / cell), 0, gy - 1);
    triCellStart[t] = cy0 * gx + cx0;
    // 跨越的格子数（行优先，按行记录跨度）
    triCellCount[t] = ((cy1 - cy0) + 1) * 65536 + ((cx1 - cx0) + 1);
    for (let cy = cy0; cy <= cy1; cy++) {
      for (let cx = cx0; cx <= cx1; cx++) counts[cy * gx + cx + 1]++;
    }
  }
  // 前缀和
  for (let i = 0; i < nCell; i++) counts[i + 1] += counts[i];
  const total = counts[nCell];
  const items = new Uint32Array(total);
  const cursor = counts.slice(0, nCell);
  for (let t = 0; t < nTri; t++) {
    const c0 = triCellStart[t];
    const span = triCellCount[t];
    const cx0 = c0 % gx;
    const cy0 = (c0 - cx0) / gx;
    const w = (span % 65536) - 1;
    const h = Math.floor(span / 65536) - 1;
    for (let dy = 0; dy <= h; dy++) {
      for (let dx = 0; dx <= w; dx++) {
        const c = (cy0 + dy) * gx + (cx0 + dx);
        items[cursor[c]++] = t;
      }
    }
  }
  return { gx, gy, cell, starts: counts, items };
}

function clampInt(v, lo, hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

/**
 * 沿 -Z 逐列投射线的奇偶填充。
 *
 * 抖动（1e-3 体素）用来避开射线正好穿过三角形共边/顶点造成的奇偶退化 ——
 * 与服务端 ``_ray_occupancy`` 的处理一致。
 */
function rayOccupancy(positions, indices, index, nx, ny, nz, origin, pitch, onProgress) {
  const occ = new Uint8Array(nx * ny * nz);
  const { gx, gy, cell, starts, items } = index;
  const zStart = origin[2] + (nz + 2) * pitch;   // 体场上方，保证覆盖整个模型

  const hits = new Float64Array(256);            // 复用：单列的 Z 交点
  const jitterX = [0, pitch * 1e-3, -pitch * 1e-3];
  const jitterY = [0, pitch * 1e-3, -pitch * 1e-3];

  const nCol = nx * ny;
  const step = Math.max(1, Math.floor(nCol / 200));

  for (let col = 0; col < nCol; col++) {
    if (onProgress && col % step === 0) onProgress(col / nCol, '射线填充');

    const i = (col / ny) | 0;
    const j = col - i * ny;
    // 体素中心 + 轻微抖动
    const px = origin[0] + (i + 0.5) * pitch + jitterX[i % 3];
    const py = origin[1] + (j + 0.5) * pitch + jitterY[j % 3];

    const cx = clampInt(Math.floor((px - origin[0]) / cell), 0, gx - 1);
    const cy = clampInt(Math.floor((py - origin[1]) / cell), 0, gy - 1);
    const c = cy * gx + cx;
    const s = starts[c];
    const e = starts[c + 1];
    if (s === e) continue;

    let nHit = 0;
    for (let k = s; k < e; k++) {
      const t = items[k];
      const z = rayTriangleZ(positions, indices, t, px, py, zStart);
      if (z === null) continue;
      if (nHit === hits.length) {
        // 交点比预估多：扩容（极少发生，保持简单）
        const bigger = new Float64Array(hits.length * 2);
        bigger.set(hits);
        hits = bigger;
      }
      hits[nHit++] = z;
    }
    if (nHit === 0) continue;

    // 交点下方最近的体素索引 k 满足 z_k < z_hit（与服务端同一套取整）
    const z0 = origin[2];
    for (let h = 0; h < nHit; h++) {
      const k = Math.ceil((hits[h] - z0) / pitch - 1e-9) - 1;
      if (k < 0 || k >= nz) continue;
      // 奇偶：从顶部向下累积，穿越奇数次 = 内部。这里直接对 [k..0] 取反太贵，
      // 改为记录事件数、最后统一累加（见下）。
      occ[col * nz + k] ^= 1;   // 事件标记（偶数次抵消）
    }
  }

  // 从顶部向下累积奇偶
  for (let col = 0; col < nCol; col++) {
    const base = col * nz;
    let parity = 0;
    for (let k = nz - 1; k >= 0; k--) {
      parity ^= occ[base + k];
      occ[base + k] = parity;
    }
  }
  if (onProgress) onProgress(1, '射线填充');
  return occ;
}

/**
 * 射线 (px, py, zStart) 沿 -Z 与三角形求交，返回交点的 Z；不相交返回 null。
 * Möller–Trumbore，只要 z，所以省掉一部分运算。
 */
function rayTriangleZ(positions, indices, t, px, py, zStart) {
  const a = indices[t * 3] * 3;
  const b = indices[t * 3 + 1] * 3;
  const c = indices[t * 3 + 2] * 3;

  const ax = positions[a]; const ay = positions[a + 1]; const az = positions[a + 2];
  const bx = positions[b]; const by = positions[b + 1]; const bz = positions[b + 2];
  const cx = positions[c]; const cy = positions[c + 1]; const cz = positions[c + 2];

  // 边向量
  const e1x = bx - ax; const e1y = by - ay; const e1z = bz - az;
  const e2x = cx - ax; const e2y = cy - ay; const e2z = cz - az;

  // 射线方向 (0,0,-1) → 与 e2 叉积 = (e2y, -e2x, 0)
  const dx = e2y;
  const dy = -e2x;
  const dz = 0;

  const det = e1x * dx + e1y * dy + e1z * dz;
  if (det > -1e-12 && det < 1e-12) return null;   // 平行
  const inv = 1 / det;

  const tx = px - ax; const ty = py - ay; const tz = zStart - az;
  const u = (tx * dx + ty * dy + tz * dz) * inv;
  if (u < 0 || u > 1) return null;

  // v = (T × e1) · dir / det
  const qx = ty * e1z - tz * e1y;
  const qy = tz * e1x - tx * e1z;
  const qz = tx * e1y - ty * e1x;
  const v = (qx * 0 + qy * 0 + qz * -1) * inv;
  if (v < 0 || u + v > 1) return null;

  // t = (e2 · (T × e1)) / det
  const dist = (e2x * qx + e2y * qy + e2z * qz) * inv;
  if (dist <= 0) return null;                     // 在射线反方向
  return zStart - dist;
}

/* =========================================================================
 * 精确欧氏距离变换（Felzenszwalb & Huttenlocher）
 * ====================================================================== */

/**
 * 计算「到最近的异类体素」的欧氏距离（mm）。
 *
 * @param {Uint8Array} occ 占据栅格（1 = 实体）
 * @param {number[]} shape [nx, ny, nz]
 * @param {boolean} wantInside true = 算实体内部到表面的距离（实体体素有值）；
 *                             false = 算空腔到实体的距离（空腔体素有值）
 * @param {number} pitch 体素尺寸 mm
 */
function edtDistance(occ, shape, wantInside, pitch, onProgress) {
  const [nx, ny, nz] = shape;
  const count = nx * ny * nz;
  // f: 目标体素 = INF，其它 = 0 → 变换后得到「到最近的其它类」的平方距离
  const f = new Float64Array(count);
  const target = wantInside ? 1 : 0;
  for (let i = 0; i < count; i++) f[i] = (occ[i] === target) ? INF : 0;

  const tmp = new Float64Array(count);
  const lineLen = Math.max(nx, ny, nz);
  const bufF = new Float64Array(lineLen);
  const bufD = new Float64Array(lineLen);
  const v = new Int32Array(lineLen);
  const z = new Float64Array(lineLen + 1);

  const strides = [1, nx, nx * ny];
  const lens = [nx, ny, nz];

  for (let axis = 0; axis < 3; axis++) {
    const stride = strides[axis];
    const len = lens[axis];
    const other = [0, 1, 2].filter((a) => a !== axis);
    const n0 = lens[other[0]];
    const n1 = lens[other[1]];
    const s0 = strides[other[0]];
    const s1 = strides[other[1]];

    for (let a = 0; a < n0; a++) {
      for (let b = 0; b < n1; b++) {
        const base = a * s0 + b * s1;
        for (let k = 0; k < len; k++) bufF[k] = f[base + k * stride];
        edt1d(bufF, len, bufD, v, z);
        for (let k = 0; k < len; k++) tmp[base + k * stride] = bufD[k];
      }
    }
    f.set(tmp);
    if (onProgress) onProgress((axis + 1) / 3, '距离变换');
  }

  const out = new Float32Array(count);
  const p2 = pitch * pitch;
  for (let i = 0; i < count; i++) {
    const d2 = f[i];
    // 无种子（整条线都到不了异类体素）时给一个大而有限的值，供下游当"很远"用
    out[i] = d2 >= IS_INF ? 1e6 : Math.sqrt(d2 * p2);
  }
  return out;
}

/**
 * 一维平方距离变换：d[q] = min_p ( (q-p)^2 + f[p] )。
 *
 * 下包络抛物线算法，O(n)。关键点：**只把有限值的点当作抛物线起点**。整条线
 * 都无种子时（比如实体内部远离表面的那一整列）结果保持 INF，而不是被
 * `inf - inf` 算成 0 —— 那样会让深处距离全部塌成 0，SDF 形状虽然对、数值全错。
 */
function edt1d(f, n, d, v, z) {
  let k = -1;
  for (let q = 0; q < n; q++) {
    if (f[q] >= IS_INF) continue;         // 无种子的位置不参与
    if (k < 0) {
      k = 0;
      v[0] = q;
      z[0] = -INF;
      z[1] = INF;
      continue;
    }
    let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    while (s <= z[k]) {
      k--;
      if (k < 0) break;
      s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    }
    k++;
    v[k] = q;
    z[k] = s;
    z[k + 1] = INF;
  }

  if (k < 0) {                            // 整条线都没有种子
    for (let q = 0; q < n; q++) d[q] = INF;
    return;
  }

  k = 0;
  for (let q = 0; q < n; q++) {
    while (z[k + 1] < q) k++;
    const dq = q - v[k];
    d[q] = dq * dq + f[v[k]];
  }
}

/* =========================================================================
 * 窄带精确化
 * ====================================================================== */

/**
 * 表面附近的窄带里，把 EDT 的量化距离换成精确的点—三角形距离。
 *
 * EDT 的误差上限是 0.5 个体素（pitch=1 mm 时就是 ±0.5 mm），⌀2 mm 的孔会被磨圆
 * 甚至糊死。这里对 |sdf| ≤ band*pitch 的体素，用三角形的 XY 网格找最近的几个面片
 * 精确求距，再按 smoothstep 融回 EDT，接缝处 C1 连续。
 */
function refineNarrowBand(positions, indices, sdf, shape, origin, pitch, bandIn, onProgress) {
  const [nx, ny, nz] = shape;
  const count = nx * ny * nz;
  let band = bandIn;

  // 候选体素：|sdf| <= (band + 余量) * pitch
  const reach = (band + BAND_CANDIDATES) * pitch;
  let candidates = [];
  for (let i = 0; i < count; i++) {
    if (Math.abs(sdf[i]) <= reach) candidates.push(i);
  }
  // 体素太多时收窄 band，保证耗时可控（与服务端同一策略）
  while (candidates.length > BAND_MAX_VOXELS && band > 1.4) {
    band = Math.max(1.4, band * 0.8);
    const r2 = (band + BAND_CANDIDATES) * pitch;
    candidates = candidates.filter((i) => Math.abs(sdf[i]) <= r2);
  }
  if (candidates.length === 0) return;

  const grid = buildTriangleGrid(positions, indices, origin, pitch, band + 2);

  const nTri = (indices.length / 3) | 0;
  const bandMm = band * pitch;
  const transMm = BAND_TRANSITION * pitch;

  for (let start = 0; start < candidates.length; start += VOXEL_CHUNK) {
    const end = Math.min(candidates.length, start + VOXEL_CHUNK);
    for (let c = start; c < end; c++) {
      const idx = candidates[c];
      const k = (idx % (nx * ny)) | 0;
      const kz = (k % nz) | 0;
      const kxy = (k - kz) / nz;
      const ky = (kxy % ny) | 0;
      const kx = (kxy - ky) / ny;

      const px = origin[0] + (kx + 0.5) * pitch;
      const py = origin[1] + (ky + 0.5) * pitch;
      const pz = origin[2] + (kz + 0.5) * pitch;

      const exact = nearestTriangleDistance(
        positions, indices, nTri, grid, px, py, pz, (band + 2) * pitch
      );
      if (exact === null) continue;

      const sign = sdf[idx] < 0 ? -1 : 1;
      const near = sign * exact;
      // 权重：|d| <= band 完全用精确值，band ~ band+1 之间 smoothstep 融回 EDT
      let w = ((bandMm + transMm) - exact) / transMm;
      w = w < 0 ? 0 : (w > 1 ? 1 : w);
      w = w * w * (3 - 2 * w);
      sdf[idx] = w * near + (1 - w) * sdf[idx];
    }
    if (onProgress) onProgress(end / candidates.length, '窄带精确化');
  }
}

/** 三角形空间网格（3D），供窄带精确距离查候选 */
function buildTriangleGrid(positions, indices, origin, pitch, cellVoxels) {
  const nTri = (indices.length / 3) | 0;
  const cell = Math.max(cellVoxels * pitch, 1e-9);
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < positions.length; i += 3) {
    for (let a = 0; a < 3; a++) {
      const v = positions[i + a];
      if (v < lo[a]) lo[a] = v;
      if (v > hi[a]) hi[a] = v;
    }
  }
  const dims = [0, 1, 2].map((a) =>
    Math.max(1, Math.ceil((hi[a] - lo[a]) / cell) + 1));
  const [gx, gy, gz] = dims;
  const nCell = gx * gy * gz;

  const counts = new Uint32Array(nCell + 1);
  const boxes = new Int32Array(nTri * 6);
  for (let t = 0; t < nTri; t++) {
    const b = triangleBox(positions, indices, t);
    const c0 = cellOf(b, 0, lo, cell, gx);
    const c1 = cellOf(b, 1, lo, cell, gy);
    const c2 = cellOf(b, 2, lo, cell, gz);
    const d0 = cellOf(b, 3, lo, cell, gx);
    const d1 = cellOf(b, 4, lo, cell, gy);
    const d2 = cellOf(b, 5, lo, cell, gz);
    boxes[t * 6] = c0; boxes[t * 6 + 1] = c1; boxes[t * 6 + 2] = c2;
    boxes[t * 6 + 3] = d0; boxes[t * 6 + 4] = d1; boxes[t * 6 + 5] = d2;
    for (let z = c2; z <= d2; z++) {
      for (let y = c1; y <= d1; y++) {
        for (let x = c0; x <= d0; x++) counts[(z * gy + y) * gx + x + 1]++;
      }
    }
  }
  for (let i = 0; i < nCell; i++) counts[i + 1] += counts[i];
  const items = new Uint32Array(counts[nCell]);
  const cursor = counts.slice(0, nCell);
  for (let t = 0; t < nTri; t++) {
    const c0 = boxes[t * 6]; const c1 = boxes[t * 6 + 1]; const c2 = boxes[t * 6 + 2];
    const d0 = boxes[t * 6 + 3]; const d1 = boxes[t * 6 + 4]; const d2 = boxes[t * 6 + 5];
    for (let z = c2; z <= d2; z++) {
      for (let y = c1; y <= d1; y++) {
        for (let x = c0; x <= d0; x++) {
          items[cursor[(z * gy + y) * gx + x]++] = t;
        }
      }
    }
  }
  return { gx, gy, gz, lo, cell, starts: counts, items };
}

function cellOf(box, i, lo, cell, n) {
  const v = Math.floor((box[i] - lo[i % 3]) / cell);
  return v < 0 ? 0 : (v >= n ? n - 1 : v);
}

function triangleBox(positions, indices, t) {
  const a = indices[t * 3] * 3;
  const b = indices[t * 3 + 1] * 3;
  const c = indices[t * 3 + 2] * 3;
  return [
    Math.min(positions[a], positions[b], positions[c]),
    Math.min(positions[a + 1], positions[b + 1], positions[c + 1]),
    Math.min(positions[a + 2], positions[b + 2], positions[c + 2]),
    Math.max(positions[a], positions[b], positions[c]),
    Math.max(positions[a + 1], positions[b + 1], positions[c + 1]),
    Math.max(positions[a + 2], positions[b + 2], positions[c + 2]),
  ];
}

/**
 * 点 p 到网格的最近三角形距离。
 * 先查所在格子；若该格为空或最近距离大于格边长，按环向外扩（最多 3 圈）。
 */
function nearestTriangleDistance(positions, indices, nTri, grid, px, py, pz, reach) {
  const { gx, gy, gz, lo, cell, starts, items } = grid;
  const cx = clampInt(Math.floor((px - lo[0]) / cell), 0, gx - 1);
  const cy = clampInt(Math.floor((py - lo[1]) / cell), 0, gy - 1);
  const cz = clampInt(Math.floor((pz - lo[2]) / cell), 0, gz - 1);

  let best = Infinity;
  for (let ring = 0; ring <= 3; ring++) {
    for (let z = cz - ring; z <= cz + ring; z++) {
      if (z < 0 || z >= gz) continue;
      for (let y = cy - ring; y <= cy + ring; y++) {
        if (y < 0 || y >= gy) continue;
        for (let x = cx - ring; x <= cx + ring; x++) {
          if (x < 0 || x >= gx) continue;
          // 只处理当前壳层，避免重复
          if (ring > 0
            && Math.abs(x - cx) !== ring
            && Math.abs(y - cy) !== ring
            && Math.abs(z - cz) !== ring) continue;
          const c = (z * gy + y) * gx + x;
          for (let k = starts[c]; k < starts[c + 1]; k++) {
            const t = items[k];
            const d = pointTriangleDistance(positions, indices, t, px, py, pz);
            if (d < best) best = d;
          }
        }
      }
    }
    // 已经找到解，且下一条壳层不可能更近，就可以停
    if (best < Infinity && best <= ring * cell) break;
  }
  if (best === Infinity || best > reach * 2) return best === Infinity ? null : best;
  return best;
}

/** 点到三角形的最短距离（Ericson, Real-Time Collision Detection） */
function pointTriangleDistance(positions, indices, t, px, py, pz) {
  const a = indices[t * 3] * 3;
  const b = indices[t * 3 + 1] * 3;
  const c = indices[t * 3 + 2] * 3;
  const ax = positions[a]; const ay = positions[a + 1]; const az = positions[a + 2];
  const bx = positions[b]; const by = positions[b + 1]; const bz = positions[b + 2];
  const cx = positions[c]; const cy = positions[c + 1]; const cz = positions[c + 2];

  const abx = bx - ax; const aby = by - ay; const abz = bz - az;
  const acx = cx - ax; const acy = cy - ay; const acz = cz - az;
  const apx = px - ax; const apy = py - ay; const apz = pz - az;

  const d1 = abx * apx + aby * apy + abz * apz;
  const d2 = acx * apx + acy * apy + acz * apz;
  if (d1 <= 0 && d2 <= 0) return dist3(px, py, pz, ax, ay, az);

  const bpx = px - bx; const bpy = py - by; const bpz = pz - bz;
  const d3 = abx * bpx + aby * bpy + abz * bpz;
  const d4 = acx * bpx + acy * bpy + acz * bpz;
  if (d3 >= 0 && d4 <= d3) return dist3(px, py, pz, bx, by, bz);

  const vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0) {
    const v = d1 / (d1 - d3);
    return dist3(px, py, pz, ax + v * abx, ay + v * aby, az + v * abz);
  }

  const cpx = px - cx; const cpy = py - cy; const cpz = pz - cz;
  const d5 = abx * cpx + aby * cpy + abz * cpz;
  const d6 = acx * cpx + acy * cpy + acz * cpz;
  if (d6 >= 0 && d5 <= d6) return dist3(px, py, pz, cx, cy, cz);

  const vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0) {
    const w = d2 / (d2 - d6);
    return dist3(px, py, pz, ax + w * acx, ay + w * acy, az + w * acz);
  }

  const va = d3 * d6 - d5 * d4;
  if (va <= 0 && (d4 - d3) >= 0 && (d5 - d6) >= 0) {
    const w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
    return dist3(px, py, pz,
      bx + w * (cx - bx), by + w * (cy - by), bz + w * (cz - bz));
  }

  const denom = 1 / (va + vb + vc);
  const v = vb * denom;
  const w = vc * denom;
  return dist3(px, py, pz,
    ax + abx * v + acx * w, ay + aby * v + acy * w, az + abz * v + acz * w);
}

function dist3(ax, ay, az, bx, by, bz) {
  const dx = ax - bx; const dy = ay - by; const dz = az - bz;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}
