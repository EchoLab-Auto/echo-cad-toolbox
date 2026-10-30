// =============================================================================
// 浏览器端晶格生成 —— WebGPU 计算着色器
//
// 一条管线三个 pass，全部在显卡上跑：
//
//   1. field_main  体素 SDF × 晶格隐式场 → 组合场 f（<=0 为材料）
//   2. count_main  统计等值面单元数（顶点）与等值面棱数（四边形）
//   3. vert_main   等值面单元 → 一个顶点（Surface Nets + Newton 投影）
//      index_main  等值面棱 → 一个四边形（两个三角形）
//
// 坐标约定与后端 app/lattice/voxel.py 完全一致：
//   * 体素 (i,j,k) 的采样点位于 origin + (i+0.5, j+0.5, k+0.5) * pitch；
//   * 展平索引 = (i * ny + j) * nz + k（numpy C 序）。
// 距离场以「毫米」为单位的近似有符号距离（<=0 为材料内部）。
// =============================================================================

const TAU: f32 = 6.283185307179586;

struct Params {
  dims:   vec4<u32>,   // nx, ny, nz, (未用)
  cell:   vec4<f32>,   // Lx, Ly, Lz, thickness
  origin: vec4<f32>,   // ox, oy, oz, pitch
  offset: vec4<f32>,   // offx, offy, offz, warp
  misc:   vec4<f32>,   // grad, grad_height, shell_thickness, skin_thickness
  flags:  vec4<u32>,   // kind, mode, wall_mode, keep_skin
  extra:  vec4<u32>,   // max_verts, max_indices, (未用), (未用)
};

@group(0) @binding(0) var<uniform> u: Params;
@group(0) @binding(1) var<storage, read> solid: array<f32>;      // 实体 SDF（服务端一次性体素化）
@group(0) @binding(2) var<storage, read_write> field: array<f32>; // 组合后的场
@group(0) @binding(3) var<storage, read_write> verts: array<f32>; // 6 float / 顶点：position.xyz, normal.xyz
@group(0) @binding(4) var<storage, read_write> slots: array<u32>; // 单元 → 顶点序号+1（0 = 空）
@group(0) @binding(5) var<storage, read_write> indices: array<u32>;
@group(0) @binding(6) var<storage, read_write> counters: array<atomic<u32>>; // 0=顶点数 1=索引数

// ---------------------------------------------------------------------------
// 晶格隐式场（与 app/lattice/implicit.py 一一对应）
// ---------------------------------------------------------------------------

fn foldf(x: f32, len: f32) -> f32 {
  let m = x - floor(x / len) * len;   // 等价于 numpy 的 x % len（结果非负）
  return min(m, len - m);
}

fn seg_dist(p: vec3<f32>, a: vec3<f32>, b: vec3<f32>) -> f32 {
  let ab = b - a;
  let ap = p - a;
  let l2 = dot(ab, ab);
  if (l2 < 1e-12) {
    return length(ap);
  }
  let t = clamp(dot(ap, ab) / l2, 0.0, 1.0);
  return length(ap - ab * t);
}

/// TPMS 的 (s, |∇s|)；s 无量纲，|∇s| 单位 1/mm
fn tpms_value(kind: u32, q: vec3<f32>, k: vec3<f32>) -> vec2<f32> {
  let A = cos(k.x * q.x);
  let a = sin(k.x * q.x);
  let B = cos(k.y * q.y);
  let b = sin(k.y * q.y);
  let C = cos(k.z * q.z);
  let c = sin(k.z * q.z);

  if (kind == 1u) { // schwarz_p
    let s = A + B + C;
    let g = vec3<f32>(-k.x * a, -k.y * b, -k.z * c);
    return vec2<f32>(s, length(g));
  }
  if (kind == 0u) { // gyroid
    let s = a * B + b * C + c * A;
    let g = vec3<f32>(
      k.x * (A * B - c * a),
      k.y * (B * C - a * b),
      k.z * (C * A - b * c)
    );
    return vec2<f32>(s, length(g));
  }

  // diamond / iwp：四正弦项组合
  var w = vec4<f32>(1.0, 1.0, 1.0, 1.0);
  if (kind == 3u) {
    w = vec4<f32>(2.0, 1.0, 1.0, 1.0);
  }
  let s = w.x * (a * b * c) + w.y * (a * B * C) + w.z * (A * b * C) + w.w * (A * B * c);
  let g = vec3<f32>(
    k.x * (w.x * (A * b * c) + w.y * (A * B * C) - w.z * (a * b * C) - w.w * (a * B * c)),
    k.y * (w.x * (a * B * c) - w.y * (a * b * C) + w.z * (A * B * C) - w.w * (A * b * c)),
    k.z * (w.x * (a * b * C) - w.y * (a * B * c) - w.z * (A * b * c) + w.w * (A * B * C))
  );
  return vec2<f32>(s, length(g));
}

/// 世界坐标 p 处的晶格场值（mm，<=0 为晶格材料）
fn lattice_field(p0: vec3<f32>) -> f32 {
  var q = p0 - u.offset.xyz;
  let L = u.cell.xyz;

  // 域扭曲
  if (u.offset.w > 0.0) {
    let amp = u.offset.w;
    let wx = amp * L.x * sin(TAU * q.z / (L.z * 2.0) + 0.7);
    let wy = amp * L.y * sin(TAU * q.x / (L.x * 2.0) + 1.9);
    let wz = amp * L.z * sin(TAU * q.y / (L.y * 2.0) + 3.1);
    q = q + vec3<f32>(wx, wy, wz);
  }

  // Z 向渐变：单胞尺寸随高度连续变化
  let grad = u.misc.x;
  if (abs(grad) > 1e-9) {
    let h = max(u.misc.y, 1e-6);
    q.z = q.z + 0.5 * grad * q.z * q.z / h;
  }

  let half_t = 0.5 * u.cell.w;
  let kind = u.flags.x;

  if (kind <= 3u) {
    let k = vec3<f32>(TAU / L.x, TAU / L.y, TAU / L.z);
    let sg = tpms_value(kind, q, k);
    let dist = sg.x / max(sg.y, 1e-9);
    if (u.flags.z == 1u) {
      return dist - half_t;          // solid：单侧实体骨架
    }
    return abs(dist) - half_t;       // sheet：薄壁
  }

  // 杆型：折叠到单胞 1/8 象限
  let ux = foldf(q.x, L.x);
  let uy = foldf(q.y, L.y);
  let uz = foldf(q.z, L.z);
  var d: f32;

  if (kind == 4u || kind == 5u) { // sc / grid
    d = min(min(length(vec2<f32>(uy, uz)), length(vec2<f32>(uz, ux))), length(vec2<f32>(ux, uy)));
  } else {
    let p = vec3<f32>(ux, uy, uz);
    if (kind == 6u) {             // bcc
      d = seg_dist(p, vec3<f32>(0.0), 0.5 * L);
    } else {                      // fcc / octet
      d = seg_dist(p, vec3<f32>(0.0), vec3<f32>(0.5 * L.x, 0.5 * L.y, 0.0));
      d = min(d, seg_dist(p, vec3<f32>(0.0), vec3<f32>(0.5 * L.x, 0.0, 0.5 * L.z)));
      d = min(d, seg_dist(p, vec3<f32>(0.0), vec3<f32>(0.0, 0.5 * L.y, 0.5 * L.z)));
      if (kind == 8u) {
        d = min(d, seg_dist(p, vec3<f32>(0.0), 0.5 * L));
      }
    }
  }
  return d - half_t;
}

// ---------------------------------------------------------------------------
// Pass 1：体素 × 晶格 → 组合场
// ---------------------------------------------------------------------------

@compute @workgroup_size(4, 4, 4)
fn field_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let dims = u.dims.xyz;
  if (any(gid >= dims)) {
    return;
  }
  let idx = (gid.x * dims.y + gid.y) * dims.z + gid.z;
  let p = u.origin.xyz + (vec3<f32>(gid) + vec3<f32>(0.5)) * u.origin.w;

  let s = solid[idx];
  let lat = lattice_field(p);

  let mode = u.flags.y;
  var f: f32;
  if (mode == 0u) {          // 内部填充
    f = max(s, lat);
  } else if (mode == 1u) {   // 表层壳内填充
    f = max(max(s, -s - u.misc.z), lat);
  } else if (mode == 2u) {   // 挖减重腔
    f = max(s, -lat);
  } else {                   // 外扩融合
    f = min(s, lat);
  }

  // 保留原始外表皮
  if (u.flags.w == 1u && (mode == 0u || mode == 1u)) {
    f = min(f, max(s, -s - u.misc.w));
  }

  // 边界保护：最外 1.6 个体素强制为“外部”，保证等值面在体场内部闭合
  let ex = min(f32(gid.x), f32(dims.x - 1u) - f32(gid.x));
  let ey = min(f32(gid.y), f32(dims.y - 1u) - f32(gid.y));
  let ez = min(f32(gid.z), f32(dims.z - 1u) - f32(gid.z));
  let edge = min(min(ex, ey), ez) * u.origin.w;
  f = max(f, 1.6 * u.origin.w - edge);

  field[idx] = f;
}

// ---------------------------------------------------------------------------
// 场采样（三线性）
// ---------------------------------------------------------------------------

fn field_at(i: u32, j: u32, k: u32) -> f32 {
  let dims = u.dims.xyz;
  return field[(i * dims.y + j) * dims.z + k];
}

fn sample_field(p: vec3<f32>) -> f32 {
  let dims = u.dims.xyz;
  let g = (p - u.origin.xyz) / u.origin.w - vec3<f32>(0.5);
  let gi = floor(g);
  let t = g - gi;

  let hi = vec3<i32>(dims) - vec3<i32>(2);
  var i0 = clamp(vec3<i32>(gi), vec3<i32>(0), hi);
  let bi = vec3<u32>(i0);

  let c000 = field_at(bi.x, bi.y, bi.z);
  let c100 = field_at(bi.x + 1u, bi.y, bi.z);
  let c010 = field_at(bi.x, bi.y + 1u, bi.z);
  let c110 = field_at(bi.x + 1u, bi.y + 1u, bi.z);
  let c001 = field_at(bi.x, bi.y, bi.z + 1u);
  let c101 = field_at(bi.x + 1u, bi.y, bi.z + 1u);
  let c011 = field_at(bi.x, bi.y + 1u, bi.z + 1u);
  let c111 = field_at(bi.x + 1u, bi.y + 1u, bi.z + 1u);

  let c00 = mix(c000, c100, t.x);
  let c10 = mix(c010, c110, t.x);
  let c01 = mix(c001, c101, t.x);
  let c11 = mix(c011, c111, t.x);
  let c0 = mix(c00, c10, t.y);
  let c1 = mix(c01, c11, t.y);
  return mix(c0, c1, t.z);
}

fn gradient(p: vec3<f32>) -> vec3<f32> {
  let h = 0.5 * u.origin.w;
  let dx = vec3<f32>(h, 0.0, 0.0);
  let dy = vec3<f32>(0.0, h, 0.0);
  let dz = vec3<f32>(0.0, 0.0, h);
  return vec3<f32>(
    sample_field(p + dx) - sample_field(p - dx),
    sample_field(p + dy) - sample_field(p - dy),
    sample_field(p + dz) - sample_field(p - dz)
  );
}

// ---------------------------------------------------------------------------
// 单元 → 顶点（Surface Nets + Newton 投影）
// ---------------------------------------------------------------------------

/// 单元 8 个角点的场值，按 c = i*4 + j*2 + k 排列
fn cell_corners(ci: vec3<u32>) -> array<f32, 8> {
  var v: array<f32, 8>;
  v[0] = field_at(ci.x, ci.y, ci.z);
  v[1] = field_at(ci.x, ci.y, ci.z + 1u);
  v[2] = field_at(ci.x, ci.y + 1u, ci.z);
  v[3] = field_at(ci.x, ci.y + 1u, ci.z + 1u);
  v[4] = field_at(ci.x + 1u, ci.y, ci.z);
  v[5] = field_at(ci.x + 1u, ci.y, ci.z + 1u);
  v[6] = field_at(ci.x + 1u, ci.y + 1u, ci.z);
  v[7] = field_at(ci.x + 1u, ci.y + 1u, ci.z + 1u);
  return v;
}

/// 单元内 12 条棱上与零等值面的交点均值（单元局部坐标 0~1）
fn crossing_centroid(ci: vec3<u32>) -> vec4<f32> {
  let v = cell_corners(ci);
  var sum = vec3<f32>(0.0, 0.0, 0.0);
  var count = 0.0;
  var inside = 0u;

  for (var c = 0u; c < 8u; c = c + 1u) {
    let ii = (c >> 2u) & 1u;
    let jj = (c >> 1u) & 1u;
    let kk = c & 1u;
    if (v[c] <= 0.0) {
      inside = inside + 1u;
    }
    let base = vec3<f32>(f32(ii), f32(jj), f32(kk));

    // +x 方向
    if (ii == 0u) {
      let other = v[c + 4u];
      if ((v[c] < 0.0) != (other < 0.0)) {
        let t = v[c] / (v[c] - other);
        sum = sum + base + vec3<f32>(t, 0.0, 0.0);
        count = count + 1.0;
      }
    }
    // +y 方向
    if (jj == 0u) {
      let other = v[c + 2u];
      if ((v[c] < 0.0) != (other < 0.0)) {
        let t = v[c] / (v[c] - other);
        sum = sum + base + vec3<f32>(0.0, t, 0.0);
        count = count + 1.0;
      }
    }
    // +z 方向
    if (kk == 0u) {
      let other = v[c + 1u];
      if ((v[c] < 0.0) != (other < 0.0)) {
        let t = v[c] / (v[c] - other);
        sum = sum + base + vec3<f32>(0.0, 0.0, t);
        count = count + 1.0;
      }
    }
  }
  if (count < 1.0) {
    return vec4<f32>(0.0, 0.0, 0.0, -1.0);
  }
  return vec4<f32>(sum / count, count);
}

// ---------------------------------------------------------------------------
// Pass 2：统计（顶点数 / 四边形数），用来精确分配缓冲区
// ---------------------------------------------------------------------------

@compute @workgroup_size(4, 4, 4)
fn count_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let dims = u.dims.xyz;
  if (gid.x + 1u >= dims.x || gid.y + 1u >= dims.y || gid.z + 1u >= dims.z) {
    return;
  }
  let v = cell_corners(gid);
  var inside = 0u;
  for (var c = 0u; c < 8u; c = c + 1u) {
    if (v[c] <= 0.0) {
      inside = inside + 1u;
    }
  }
  if (inside == 0u || inside == 8u) {
    return;
  }
  _ = atomicAdd(&counters[0], 1u);

  // 该单元的 +x/+y/+z 三条棱是否穿过等值面 → 对应上行邻居的四边形
  if ((v[0] < 0.0) != (v[4] < 0.0)) {
    if (gid.y > 0u && gid.z > 0u) {
      _ = atomicAdd(&counters[1], 6u);
    }
  }
  if ((v[0] < 0.0) != (v[2] < 0.0)) {
    if (gid.x > 0u && gid.z > 0u) {
      _ = atomicAdd(&counters[1], 6u);
    }
  }
  if ((v[0] < 0.0) != (v[1] < 0.0)) {
    if (gid.x > 0u && gid.y > 0u) {
      _ = atomicAdd(&counters[1], 6u);
    }
  }
}

// ---------------------------------------------------------------------------
// Pass 3：写顶点
// ---------------------------------------------------------------------------

@compute @workgroup_size(4, 4, 4)
fn vert_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let dims = u.dims.xyz;
  if (gid.x + 1u >= dims.x || gid.y + 1u >= dims.y || gid.z + 1u >= dims.z) {
    return;
  }
  let centroid = crossing_centroid(gid);
  if (centroid.w < 1.0) {
    return;
  }

  // 单元局部坐标 → 世界坐标
  let base = u.origin.xyz + (vec3<f32>(gid) + vec3<f32>(0.5)) * u.origin.w;
  var p = base + centroid.xyz * u.origin.w;

  // Newton 投影：把顶点推到 f = 0 的真实等值面上（距离场下 1~2 步即收敛到
  // 亚体素精度，这是毫米级小孔/薄壁不被“体素阶梯化”的关键）
  for (var it: u32 = 0u; it < 2u; it = it + 1u) {
    let f = sample_field(p);
    let g = gradient(p);
    let gg = dot(g, g);
    if (gg < 1e-9) {
      break;
    }
    p = p - g * (f / gg);
  }
  // 限制在单元附近，避免病态场把顶点甩出去
  let lo = base - 0.35 * u.origin.w;
  let hi = base + 1.35 * u.origin.w;
  p = clamp(p, lo, hi);

  var n = gradient(p);
  let nl = length(n);
  if (nl < 1e-9) {
    n = vec3<f32>(0.0, 0.0, 1.0);
  } else {
    n = n / nl;
  }

  let cell = (gid.x * (dims.y - 1u) + gid.y) * (dims.z - 1u) + gid.z;
  let slot = atomicAdd(&counters[0], 1u);
  if (slot >= u.extra.x) {
    slots[cell] = 0u;
    return;
  }
  slots[cell] = slot + 1u;

  let o = slot * 6u;
  verts[o + 0u] = p.x;
  verts[o + 1u] = p.y;
  verts[o + 2u] = p.z;
  verts[o + 3u] = n.x;
  verts[o + 4u] = n.y;
  verts[o + 5u] = n.z;
}

// ---------------------------------------------------------------------------
// Pass 4：写四边形索引
// ---------------------------------------------------------------------------

fn slot_of(ci: vec3<u32>) -> u32 {
  let dims = u.dims.xyz;
  let cell = (ci.x * (dims.y - 1u) + ci.y) * (dims.z - 1u) + ci.z;
  return slots[cell];
}

fn emit_quad(a: u32, b: u32, c: u32, d: u32, flip: bool) {
  let base = atomicAdd(&counters[1], 6u);
  if (base + 6u > u.extra.y) {
    return;
  }
  let i0 = a - 1u;
  let i1 = b - 1u;
  let i2 = c - 1u;
  let i3 = d - 1u;
  if (flip) {
    indices[base + 0u] = i0;
    indices[base + 1u] = i3;
    indices[base + 2u] = i2;
    indices[base + 3u] = i0;
    indices[base + 4u] = i2;
    indices[base + 5u] = i1;
  } else {
    indices[base + 0u] = i0;
    indices[base + 1u] = i1;
    indices[base + 2u] = i2;
    indices[base + 3u] = i0;
    indices[base + 4u] = i2;
    indices[base + 5u] = i3;
  }
}

@compute @workgroup_size(4, 4, 4)
fn index_main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let dims = u.dims.xyz;
  if (gid.x + 1u >= dims.x || gid.y + 1u >= dims.y || gid.z + 1u >= dims.z) {
    return;
  }
  let v = cell_corners(gid);
  let inside0 = v[0] < 0.0;

  // 四条共享该棱的单元（Surface Nets 的对偶四边形）
  // +x 棱
  if (inside0 != (v[4] < 0.0) && gid.y > 0u && gid.z > 0u) {
    let s0 = slot_of(gid);
    let s1 = slot_of(gid - vec3<u32>(0u, 1u, 0u));
    let s2 = slot_of(gid - vec3<u32>(0u, 1u, 1u));
    let s3 = slot_of(gid - vec3<u32>(0u, 0u, 1u));
    if (s0 > 0u && s1 > 0u && s2 > 0u && s3 > 0u) {
      emit_quad(s0, s1, s2, s3, !inside0);
    }
  }
  // +y 棱
  if (inside0 != (v[2] < 0.0) && gid.x > 0u && gid.z > 0u) {
    let s0 = slot_of(gid);
    let s1 = slot_of(gid - vec3<u32>(1u, 0u, 0u));
    let s2 = slot_of(gid - vec3<u32>(1u, 0u, 1u));
    let s3 = slot_of(gid - vec3<u32>(0u, 0u, 1u));
    if (s0 > 0u && s1 > 0u && s2 > 0u && s3 > 0u) {
      emit_quad(s0, s3, s2, s1, !inside0);
    }
  }
  // +z 棱
  if (inside0 != (v[1] < 0.0) && gid.x > 0u && gid.y > 0u) {
    let s0 = slot_of(gid);
    let s1 = slot_of(gid - vec3<u32>(1u, 0u, 0u));
    let s2 = slot_of(gid - vec3<u32>(1u, 1u, 0u));
    let s3 = slot_of(gid - vec3<u32>(0u, 1u, 0u));
    if (s0 > 0u && s1 > 0u && s2 > 0u && s3 > 0u) {
      emit_quad(s0, s1, s2, s3, !inside0);
    }
  }
}
