/* =============================================================================
 * 摆线针轮几何分析 —— 浏览器端纯函数实现（原生 ES module，无构建）
 *
 * 目标：给前端提供与 `POST /api/analyze` **完全同构**的返回值，从而把参数滑块的
 * 交互式分析从服务器搬到浏览器（去掉网络往返）。字段名、数值语义、数组结构、
 * 告警条目与级别、甚至 `_round_points` 的 4 位小数压缩都按后端 app/api.py
 * 与 app/geometry.py 逐行对齐。
 *
 * 公式来源（与 app/geometry.py 顶部注释一致）
 * -------------------------------------------
 *   Zp    = Zc + 1                       针齿数
 *   Rz'   = Rz - ΔRz                     移距修形后的针齿中心圆半径
 *   rp'   = rp + Δrp                     等距修形后的（虚拟）针齿半径
 *   P(τ)  = ( Rz'·cosτ - e·cos(Zp·τ),  Rz'·sinτ - e·sin(Zp·τ) )      理论齿廓
 *   S(τ)  = sqrt( Rz'² + (e·Zp)² - 2·Rz'·e·Zp·cos(Zc·τ) )            法线尺度因子
 *   N(τ)  = ( Rz'·cosτ - e·Zp·cos(Zp·τ),  Rz'·sinτ - e·Zp·sin(Zp·τ) ) / S(τ)
 *   Q(τ)  = P(τ) - rp'·N(τ)              实际齿廓（内等距曲线）
 * 转角修形：整个实际齿廓绕原点旋转 -Δθ（Δθ 为弧度）。
 *
 * 数值方法与容差（后端用 numpy 的地方都换成等价的标量算法）
 * ----------------------------------------------------------
 * 1. τ 采样：`np.linspace(0, TAU, N, endpoint=False)` → τ_i = i·(TAU/N)，逐点同式。
 * 2. 弧长均匀重采样（`_resample_closed`，density = 6）：先用线性插值把原折线
 *    加密 6 倍（端点环绕），再按累计弦长做一次线性插值等分。全过程都是同式
 *    线性插值，与后端逐位对齐（仅浮点求和顺序不同，~1e-15 相对误差）。
 * 3. 曲率 ρ = (dx·ddy - dy·ddx) / max(S, 1e-12)³，`min_curvature_radius`
 *    取 min|ρ|；S == 0 处 ρ 为 ±Infinity，与 numpy 的 `np.where(isfinite...)`
 *    分支一致（num == 0 时得到 NaN，numpy 同样如此，JSON 里都是 null）。
 * 4. 星形性（自交检测）：对实际齿廓点取极角 θ，按 `np.unwrap` 的算法（跳变
 *    阈值 discont = π，修正量取 2π 的整数倍）解缠，然后检查相邻极角差是否
 *    全部 > 0；`fold_ratio` = min(Δθ) / mean(Δθ)。判定本身没有容差，因此极端
 *    自交参数下若极角差恰好落在 ±1ulp 内，结论可能与后端差 1 个采样点；
 *    实际参数域内结论稳定（见 tools/analysis_parity.py 的用例）。
 * 5. 周长 `profile_length`：实际齿廓折线（含末点到首点）的弦长和，顺序累加；
 *    numpy 用成对求和，差异 ~1e-15 相对量级。
 * 6. 接触表：对每个转臂角 φ_k = 2πk/steps 解析求出全部接触点，变换到世界系后
 *    与针齿中心求距，取间隙最小的 floor(Zp/2) 个针齿；间隙按 `round(x, 6)`
 *    输出（JS 侧用 `toFixed(6)`，它与 Python `round` 只在十进制中点处不同，
 *    而此类中点无法用二进制浮点精确表示，故结果一致）。
 * 7. 4 位小数压缩（后端 `_round_points`）：实际/理论齿廓、针齿中心、柱销孔中心、
 *    接触表 center 都用 `Number(v.toFixed(4))`，与 Python `round(v, 4)` 等价
 *    （理由同上）。
 * 8. Python 的 `round()` 是「四舍六入五取偶」，JS 的 `Math.round()` 是「五入」，
 *    自动布局里 `round(Zc/4)` 会碰到 Zc ≡ 2 (mod 4) 的中点，因此实现了
 *    `pyRound()` 显式模拟银行家舍入。
 *
 * 导出
 * ----
 *   analyze(params, opts)       → 与 /api/analyze 响应同构的对象
 *   contactTable(params, steps) → 只算接触表（供动画复用）
 *   normalizeParams(params)     → 按后端 ParamsIn 的默认值补齐/取整
 *   PARAM_DEFAULTS              → 默认参数（与后端 ParamsIn 一致）
 *
 * opts（可选，均为浏览器端便利，不影响与后端的同构性）
 *   contactSteps / contact_steps / steps : 接触表步数，0（默认）= 不返回 contacts，
 *                                  与 AnalyzeRequest.contact_steps 语义一致，上限 1440
 *   samples                      : 覆盖齿廓采样总数（对应 geometry.analyze(samples=)）
 *   round                        : 坐标小数位，默认 4（对齐 _round_points）；
 *                                  false / 0 关闭取整，拿到全精度
 * ========================================================================== */

'use strict';

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

/** 与 app/geometry.py:DEFAULT_SAMPLES_PER_TOOTH 一致 */
const DEFAULT_SAMPLES_PER_TOOTH = 64;

/** 法线尺度因子视为 0 的阈值（对应 geometry._eval_profile 里的 1e-12） */
const NORMAL_EPS = 1e-12;

/** 与 pydantic ParamsIn 的默认值一致：缺省字段按此补齐 */
export const PARAM_DEFAULTS = Object.freeze({
  tooth_count: 39,
  eccentricity: 1.43,
  pin_circle_radius: 88.0,
  pin_radius: 5.5,
  equidistant_offset: 0.02,
  radial_offset: 0.0,
  rotary_offset_deg: 0.0,
  ring_wall: 12.0,
  pin_hole_count: 0,
  pin_hole_circle_radius: 0.0,
  pin_sleeve_radius: 0.0,
  clearance: 0.0,
  center_bore_radius: 0.0,
  samples_per_tooth: DEFAULT_SAMPLES_PER_TOOTH,
});

/* ---------------------------------------------------------------------------
 * 小工具
 * ------------------------------------------------------------------------ */

/** 转数值，null/undefined/空串/非数一律回落默认值（后端 ParamsIn 拒绝 null，这里宽容） */
function num(value, fallback) {
  if (value === null || value === undefined || value === '') return fallback;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** Python 的 round()：四舍六入五取偶（仅用于自动布局里的 round(Zc/4)） */
function pyRound(x) {
  const f = Math.floor(x);
  const diff = x - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** Python 的 round(v, digits)（digits > 0）：定点十进制取整 */
function roundTo(value, digits) {
  if (!Number.isFinite(value)) return value;
  if (Math.abs(value) >= 1e21) return value; // toFixed 对大数会转指数形式
  return Number(value.toFixed(digits));
}

/**
 * 正确舍入的 hypot(x, y)，用于对齐后端 numpy 的 ``np.hypot``。
 *
 * 为什么不用 ``Math.hypot``：V8 的实现有 ~1 ulp 误差，而后端 numpy 的 hypot
 * 在本机实测有 99.4% 的输入等于「正确舍入」值（与 glibc 的 hypot 一致）。
 * 这 1 ulp 的差会在「柱销孔外壁厚度恰好 2 mm」这种边界判定上让告警抖动，
 * 所以用 Dekker/Veltkamp 的 double-double 技巧先把 x²+y² 算成 (hi, lo)，
 * 再对 sqrt 做一次牛顿修正。同一算法在 30 万组随机点上与 C 库 hypot 逐位一致
 * （见 tools/analysis_parity.py 的派生量对拍：root/tip 半径多数情况逐位相同）。
 *
 * 极端量级（溢出/下溢）走 ``Math.hypot`` 兜底——摆线轮的实际尺寸用不到。
 */
const VELTKAMP = 134217729.0; // 2^27 + 1

function hypot2(x, y) {
  const ax = Math.abs(x);
  const ay = Math.abs(y);
  if (ax === 0 && ay === 0) return 0;
  const guess = ax * ax + ay * ay;
  if (!(guess > 0) || !Number.isFinite(guess) || ax > 1e150) return Math.hypot(ax, ay);

  // two_prod(ax, ax)：拆分后求平方的误差项
  const px = ax * ax;
  let c = VELTKAMP * ax;
  const ah = c - (c - ax);
  const al = ax - ah;
  const ex = (ah * ah - px + 2 * ah * al) + al * al;

  const py = ay * ay;
  c = VELTKAMP * ay;
  const bh = c - (c - ay);
  const bl = ay - bh;
  const ey = (bh * bh - py + 2 * bh * bl) + bl * bl;

  // two_sum(px, py)：和与误差
  const hi = px + py;
  const bb = hi - px;
  const es = px - (hi - bb) + (py - bb);
  const lo = es + (ex + ey);

  // r = sqrt(hi)，再用残差修正 1 次
  const r = Math.sqrt(hi);
  const p2 = r * r;
  c = VELTKAMP * r;
  const rh = c - (c - r);
  const rl = r - rh;
  const e2 = (rh * rh - p2 + 2 * rh * rl) + rl * rl;
  const resid = (hi - p2 - e2) + lo;
  return r + resid / (2 * r);
}

/* ---------------------------------------------------------------------------
 * 参数归一化
 * ------------------------------------------------------------------------ */

/**
 * 按后端 ParamsIn 的默认值与类型补齐参数（int 字段取整，其余转 float）。
 * 未知字段忽略 —— 与 geometry.CycloidParams.from_mapping 的行为一致。
 */
export function normalizeParams(params = {}) {
  const src = params || {};
  const d = PARAM_DEFAULTS;
  const intOf = (v, fb) => Math.trunc(num(v, fb));
  return {
    tooth_count: intOf(src.tooth_count, d.tooth_count),
    eccentricity: num(src.eccentricity, d.eccentricity),
    pin_circle_radius: num(src.pin_circle_radius, d.pin_circle_radius),
    pin_radius: num(src.pin_radius, d.pin_radius),
    equidistant_offset: num(src.equidistant_offset, d.equidistant_offset),
    radial_offset: num(src.radial_offset, d.radial_offset),
    rotary_offset_deg: num(src.rotary_offset_deg, d.rotary_offset_deg),
    ring_wall: num(src.ring_wall, d.ring_wall),
    pin_hole_count: intOf(src.pin_hole_count, d.pin_hole_count),
    pin_hole_circle_radius: num(src.pin_hole_circle_radius, d.pin_hole_circle_radius),
    pin_sleeve_radius: num(src.pin_sleeve_radius, d.pin_sleeve_radius),
    clearance: num(src.clearance, d.clearance),
    center_bore_radius: num(src.center_bore_radius, d.center_bore_radius),
    samples_per_tooth: intOf(src.samples_per_tooth, d.samples_per_tooth),
  };
}

/* ---------------------------------------------------------------------------
 * 几何核心（逐个函数对应 app/geometry.py）
 * ------------------------------------------------------------------------ */

/** CycloidParams 的派生量，预计算避免重复求值 */
function makeGeom(p) {
  const Zc = Math.max(1, Math.trunc(p.tooth_count));
  const Zp = Zc + 1;
  return {
    Zc,
    Zp,
    e: p.eccentricity,
    Rzp: p.pin_circle_radius - p.radial_offset, // Rz'
    rpp: p.pin_radius + p.equidistant_offset, // rp'
    eZp: p.eccentricity * Zp,
    rot: p.rotary_offset_deg * DEG,
  };
}

/** geometry._sample_tau：τ_i = i·TAU/N，N 至少 16 */
function sampleTau(count) {
  const n = Math.max(Math.trunc(count) || 0, 16);
  const step = TAU / n;
  const tau = new Float64Array(n);
  for (let i = 0; i < n; i += 1) tau[i] = i * step;
  return tau;
}

/** geometry.theoretical_profile：理论齿廓 P(τ)，返回 [[x, y], ...] */
function theoreticalProfile(g, tau) {
  const { Rzp, e, Zp } = g;
  const pts = new Array(tau.length);
  for (let i = 0; i < tau.length; i += 1) {
    const t = tau[i];
    pts[i] = [Rzp * Math.cos(t) - e * Math.cos(Zp * t), Rzp * Math.sin(t) - e * Math.sin(Zp * t)];
  }
  return pts;
}

/**
 * geometry._eval_profile：实际齿廓 Q(τ)（含转角修形）。
 * 返回 { x, y, finite }，finite[i] === 0 表示该行在 numpy 里是 NaN 会被丢弃。
 */
function evalProfile(g, tau) {
  const { Rzp, e, Zp, Zc, rpp, rot } = g;
  const eZp = g.eZp;
  const n = tau.length;
  const x = new Float64Array(n);
  const y = new Float64Array(n);
  const finite = new Uint8Array(n);
  const doRot = Math.abs(rot) > 1e-12;
  const cr = doRot ? Math.cos(-rot) : 1;
  const sr = doRot ? Math.sin(-rot) : 0;

  for (let i = 0; i < n; i += 1) {
    const t = tau[i];
    const c = Math.cos(t);
    const s = Math.sin(t);
    const cz = Math.cos(Zp * t);
    const sz = Math.sin(Zp * t);
    const dx = -Rzp * s + e * Zp * sz;
    const dy = Rzp * c - e * Zp * cz;
    // S(τ) 与 profile_derivatives 同式；浮点消去导致负值时按 0 处理
    const sLen = Math.sqrt(Math.max(Rzp * Rzp + eZp * eZp - 2 * Rzp * e * Zp * Math.cos(Zc * t), 0));
    if (!(sLen >= NORMAL_EPS)) {
      // 对应 numpy: safe = where(S < 1e-12, nan, S) → 该行坐标全为 NaN
      x[i] = NaN;
      y[i] = NaN;
      finite[i] = 0;
    } else {
      const nx = dy / sLen;
      const ny = -dx / sLen;
      let px = Rzp * c - e * cz - rpp * nx;
      let py = Rzp * s - e * sz - rpp * ny;
      if (doRot) {
        const rx = px * cr - py * sr;
        const ry = px * sr + py * cr;
        px = rx;
        py = ry;
      }
      x[i] = px;
      y[i] = py;
      finite[i] = 1;
    }
  }
  return { x, y, finite };
}

/** geometry.profile_derivatives：一阶/二阶导数与法线尺度因子 */
function profileDerivatives(g, tau) {
  const { Rzp, e, Zp, Zc } = g;
  const eZp = g.eZp;
  const n = tau.length;
  const dx = new Float64Array(n);
  const dy = new Float64Array(n);
  const ddx = new Float64Array(n);
  const ddy = new Float64Array(n);
  const s = new Float64Array(n);
  for (let i = 0; i < n; i += 1) {
    const t = tau[i];
    const c = Math.cos(t);
    const sn = Math.sin(t);
    const cz = Math.cos(Zp * t);
    const sz = Math.sin(Zp * t);
    dx[i] = -Rzp * sn + e * Zp * sz;
    dy[i] = Rzp * c - e * Zp * cz;
    ddx[i] = -Rzp * c + e * Zp * Zp * cz;
    ddy[i] = -Rzp * sn + e * Zp * Zp * sz;
    s[i] = Math.sqrt(Math.max(Rzp * Rzp + eZp * eZp - 2 * Rzp * e * Zp * Math.cos(Zc * t), 0));
  }
  return { dx, dy, ddx, ddy, s };
}

/**
 * geometry._resample_closed：按弧长均匀重采样闭合折线。
 * 先线性插值加密 density 倍（环绕），再按累计弦长等距线性插值。
 */
function resampleClosed(pts, count, density = 6) {
  const n = pts.length;
  if (n < 3 || count <= 0) return pts;

  // --- 1) 加密：np.linspace(0, n, n*density, endpoint=False) 上做周期线性插值
  const total = n * density;
  const step = n / (n * density); // 与 numpy linspace 的步长同值（= 1/density）
  const denseX = new Float64Array(total);
  const denseY = new Float64Array(total);
  for (let j = 0; j < total; j += 1) {
    const t = j * step;
    const k = Math.floor(t);
    const frac = t - k;
    const a = pts[k % n];
    const b = pts[(k + 1) % n];
    denseX[j] = (b[0] - a[0]) * frac + a[0];
    denseY[j] = (b[1] - a[1]) * frac + a[1];
  }

  // --- 2) 累计弦长（闭链：末尾补回首点）
  const closedX = new Float64Array(total + 1);
  const closedY = new Float64Array(total + 1);
  const cum = new Float64Array(total + 1);
  closedX[0] = denseX[0];
  closedY[0] = denseY[0];
  cum[0] = 0;
  for (let i = 1; i <= total; i += 1) {
    const px = i === total ? denseX[0] : denseX[i];
    const py = i === total ? denseY[0] : denseY[i];
    closedX[i] = px;
    closedY[i] = py;
    cum[i] = cum[i - 1] + hypot2(px - closedX[i - 1], py - closedY[i - 1]);
  }
  const length = cum[total];
  if (!(length > 0)) return pts;

  // --- 3) 等弧长插值：目标点单调递增，游标推进等价于 np.interp 的二分查找
  const targetStep = length / count;
  const out = new Array(count);
  let j = 0;
  for (let i = 0; i < count; i += 1) {
    const target = i * targetStep;
    // 取「cum[j] <= target 的最大 j」，与 np.interp 的二分查找同解
    while (j + 1 < total && cum[j + 1] <= target) j += 1;
    if (cum[j] === target) {
      out[i] = [closedX[j], closedY[j]];
    } else {
      const span = cum[j + 1] - cum[j];
      const slopeX = (closedX[j + 1] - closedX[j]) / span;
      const slopeY = (closedY[j + 1] - closedY[j]) / span;
      out[i] = [slopeX * (target - cum[j]) + closedX[j], slopeY * (target - cum[j]) + closedY[j]];
    }
  }
  return out;
}

/** geometry.actual_profile：实际齿廓（NaN 行过滤 + 弧长均匀重采样） */
function actualProfile(g, tau) {
  const ev = evalProfile(g, tau);
  const pts = [];
  for (let i = 0; i < tau.length; i += 1) {
    if (ev.finite[i]) pts.push([ev.x[i], ev.y[i]]);
  }
  if (pts.length > 8) return resampleClosed(pts, tau.length);
  return pts;
}

/** geometry.pin_centers：针齿中心（世界系，静止） */
function pinCenters(p) {
  const Zp = Math.max(1, Math.trunc(p.tooth_count)) + 1;
  const out = new Array(Zp);
  for (let i = 0; i < Zp; i += 1) {
    const psi = (TAU * i) / Zp;
    out[i] = [p.pin_circle_radius * Math.cos(psi), p.pin_circle_radius * Math.sin(psi)];
  }
  return out;
}

/** geometry.ring_radii：针齿壳的关键半径 */
function ringRadii(p) {
  const pinOuter = p.pin_circle_radius + p.pin_radius;
  const inner = pinOuter + 1.0;
  const outer = inner + p.ring_wall;
  return { pin_outer: pinOuter, inner, outer };
}

/**
 * geometry.output_layout：输出机构（柱销 / 柱销孔 / 中心孔）布局，自动补全未指定项。
 * 与后端逐行一致，包括 root_radius 用 max(720, Zc*24) 采样的细节。
 */
function outputLayout(p, g) {
  const MIN_RIM = 2.0;
  const MIN_WEB = 2.0;
  const MIN_GAP = 1.2;

  const zc = g.Zc;
  const tauLayout = sampleTau(Math.max(720, zc * 24));
  const prof = actualProfile(g, tauLayout);
  let rootRadius = Infinity;
  let tipRadius = -Infinity;
  for (let i = 0; i < prof.length; i += 1) {
    const r = hypot2(prof[i][0], prof[i][1]);
    if (r < rootRadius) rootRadius = r;
    if (r > tipRadius) tipRadius = r;
  }

  // --- 柱销数：默认 ≈ Zc/4（4 ~ 12 个），注意 Python 的 round 是银行家舍入
  let count = Math.trunc(p.pin_hole_count);
  if (count <= 0) {
    count = Math.min(12, Math.max(4, pyRound(zc / 4)));
    count = Math.min(count, zc);
  }
  count = Math.max(Math.trunc(count), 3);

  // --- 分布圆半径：默认取齿根圆的 0.72，且必须给外壁留出空间
  let circleRadius = p.pin_hole_circle_radius;
  if (circleRadius <= 0) circleRadius = 0.72 * rootRadius;
  circleRadius = Math.min(circleRadius, Math.max(rootRadius - MIN_RIM - 1.0, 0.5));

  const chord = 2.0 * circleRadius * Math.sin(Math.PI / count);
  const holeMaxPitch = chord / 2.0 - MIN_GAP / 2.0;
  const holeMaxRim = rootRadius - MIN_RIM - circleRadius;
  const holeMax = Math.min(holeMaxPitch, holeMaxRim);

  // --- 柱销套半径
  let sleeve = p.pin_sleeve_radius;
  if (sleeve <= 0) sleeve = Math.max(chord / 3.0 - p.eccentricity, 0.5);
  let holeRadius = sleeve + p.eccentricity + p.clearance;
  if (holeRadius > holeMax) {
    holeRadius = Math.max(holeMax, 0.4);
    sleeve = Math.max(holeRadius - p.eccentricity - p.clearance, 0.1);
  }

  // --- 中心孔
  const boreMax = circleRadius - holeRadius - MIN_WEB;
  let bore = p.center_bore_radius;
  if (bore <= 0) bore = Math.max(Math.min(boreMax, 0.45 * rootRadius), 0.0);

  const holeCenters = new Array(count);
  for (let i = 0; i < count; i += 1) {
    const psi = (TAU * i) / count;
    holeCenters[i] = [circleRadius * Math.cos(psi), circleRadius * Math.sin(psi)];
  }

  return {
    count,
    circle_radius: circleRadius,
    sleeve_radius: sleeve,
    hole_radius: holeRadius,
    hole_centers: holeCenters,
    center_bore_radius: bore,
    // 以下几项只用于对齐 geometry.output_layout 的返回结构，不进 JSON
    root_radius: rootRadius,
    tip_radius: tipRadius,
    hole_max: holeMax,
    bore_max: boreMax,
  };
}

/* ---------------------------------------------------------------------------
 * 告警（geometry._collect_warnings）
 * ------------------------------------------------------------------------ */

function collectWarnings(derivedInfo, out, sMin, rootRadius, starShaped) {
  const warn = [];

  const k1 = derivedInfo.short_amplitude_coefficient;
  if (k1 >= 1.0) {
    warn.push({
      level: 'error',
      code: 'undercut',
      message: `短幅系数 K1 = ${k1.toFixed(3)} ≥ 1，理论齿廓出现尖点（根切），无法生成有效齿廓。请增大 Rz 或减小 e。`,
    });
  } else if (k1 > 0.85) {
    warn.push({
      level: 'warn',
      code: 'undercut_near',
      message: `短幅系数 K1 = ${k1.toFixed(3)} 偏高（>0.85），齿廓接近根切，齿根应力较大。`,
    });
  } else if (k1 < 0.3) {
    warn.push({
      level: 'info',
      code: 'low_k1',
      message: `短幅系数 K1 = ${k1.toFixed(3)} 偏低（<0.3），齿廓接近圆弧，承载能力下降。`,
    });
  }

  if (sMin <= 1e-6) {
    warn.push({
      level: 'error',
      code: 'cusp',
      message: '法线尺度因子 S(τ) 出现零值，齿廓存在尖点，几何非法。',
    });
  }

  if (!starShaped) {
    warn.push({
      level: 'error',
      code: 'self_intersection',
      message:
        '实际齿廓不是星形曲线（极角出现回折），等距偏移量过大导致齿廓自交。' +
        '请减小针齿半径 rp 或减小等距修形 Δrp。',
    });
  }

  if (derivedInfo.effective_pin_radius > 0.25 * derivedInfo.pin_circle_radius) {
    warn.push({
      level: 'info',
      code: 'large_pin',
      message: '针齿半径相对针齿中心圆半径偏大（>0.25），齿廓趋于平坦。',
    });
  }

  if (rootRadius <= out.center_bore_radius + 2.0) {
    warn.push({
      level: 'warn',
      code: 'thin_rim',
      message: '齿根圆与中心孔之间壁厚不足 2 mm。',
    });
  }

  const rim = rootRadius - (out.circle_radius + out.hole_radius);
  if (rim < 2.0) {
    warn.push({
      level: 'warn',
      code: 'hole_overrun',
      message: `柱销孔外壁厚度仅 ${rim.toFixed(2)} mm（<2 mm），实际会被齿根切穿。`,
    });
  }

  if (out.circle_radius - out.hole_radius - out.center_bore_radius < 2.0) {
    warn.push({
      level: 'warn',
      code: 'hole_bore_overlap',
      message: '柱销孔与中心孔之间的壁厚不足 2 mm。',
    });
  }

  if (derivedInfo.eccentricity <= 0) {
    warn.push({ level: 'error', code: 'e_zero', message: '偏心距必须为正。' });
  }
  if (derivedInfo.pin_radius <= 0) {
    warn.push({ level: 'error', code: 'rz_zero', message: '针齿半径必须为正。' });
  }

  return warn;
}

/* ---------------------------------------------------------------------------
 * 星形性（自交）检测：np.unwrap + 极角单调性
 * ------------------------------------------------------------------------ */

function starMetrics(pts) {
  const m = pts.length;
  if (m === 0) return { star_shaped: false, fold_ratio: 0.0 };

  const theta = new Float64Array(m);
  let prevRaw = Math.atan2(pts[0][1], pts[0][0]);
  theta[0] = prevRaw;
  let correction = 0;
  for (let i = 1; i < m; i += 1) {
    const raw = Math.atan2(pts[i][1], pts[i][0]);
    // np.unwrap：把 |Δ| ≥ π 的跳变折算到 (-π, π] 后作为修正量累加
    const dd = raw - prevRaw;
    if (Math.abs(dd) >= Math.PI) {
      let wrapped = (((dd + Math.PI) % TAU) + TAU) % TAU - Math.PI;
      if (wrapped === -Math.PI && dd > 0) wrapped = Math.PI;
      correction += wrapped - dd;
    }
    prevRaw = raw;
    theta[i] = raw + correction;
  }

  // dtheta = diff(concat(theta, [theta[0] + TAU]))
  let starShaped = true;
  let dMin = Infinity;
  let sum = 0;
  for (let i = 0; i < m; i += 1) {
    const next = i + 1 < m ? theta[i + 1] : theta[0] + TAU;
    const dth = next - theta[i];
    if (!(dth > 0)) starShaped = false;
    if (dth < dMin) dMin = dth;
    sum += dth;
  }
  const mean = sum / m;
  const foldRatio = mean > 0 ? dMin / mean : 0.0;
  return { star_shaped: starShaped, fold_ratio: foldRatio };
}

/* ---------------------------------------------------------------------------
 * 接触表（geometry.contact_table）
 * ------------------------------------------------------------------------ */

/** 转臂角 phi 时全部针齿的接触点，已变换到世界系 */
function contactWorldPoints(g, phi) {
  const { Zp, Zc, e } = g;
  const tau = new Float64Array(Zp);
  const shift = phi / Zc;
  for (let i = 0; i < Zp; i += 1) tau[i] = (TAU * i) / Zp + shift;
  const ev = evalProfile(g, tau);

  // 摆线轮系 → 世界系：旋转 θc = -φ/Zc 后平移到 C(φ) = e·(cosφ, sinφ)
  const theta = -phi / Zc;
  const c = Math.cos(theta);
  const s = Math.sin(theta);
  const cx = e * Math.cos(phi);
  const cy = e * Math.sin(phi);
  const out = new Array(Zp);
  for (let i = 0; i < Zp; i += 1) {
    const px = ev.x[i];
    const py = ev.y[i];
    out[i] = [px * c - py * s + cx, px * s + py * c + cy];
  }
  return out;
}

/**
 * geometry.contact_table：一个转臂周期内的啮合状态（供前端动画查表）。
 * 返回 { steps, pin_indices, gaps, disc_angle, center }。
 *
 * ``digits`` 与后端 API 层 `_round_points` 对齐：center 保留 4 位小数；
 * gaps 固定 6 位（后端 contact_table 内 round(x, 6)）。
 */
export function contactTable(params, steps = 72, digits = 4) {
  const p = normalizeParams(params);
  const g = makeGeom(p);
  const n = Math.max(Math.trunc(num(steps, 72)), 8); // 后端 steps = max(int(steps), 8)
  const pins = pinCenters(p);
  const rp = p.pin_radius; // 注意：后端用未修形的 pin_radius
  const half = Math.max(1, Math.floor(g.Zp / 2));

  const indices = [];
  const gapsOut = [];
  const discAngle = [];
  const center = [];

  for (let k = 0; k < n; k += 1) {
    const phi = (TAU * k) / n;
    const world = contactWorldPoints(g, phi);

    const gaps = new Float64Array(g.Zp);
    for (let i = 0; i < g.Zp; i += 1) {
      gaps[i] = hypot2(world[i][0] - pins[i][0], world[i][1] - pins[i][1]) - rp;
    }

    // np.argsort(gaps)：NaN 排最后；并列时按索引升序（确定性更好）
    const order = new Array(g.Zp);
    for (let i = 0; i < g.Zp; i += 1) order[i] = i;
    order.sort((a, b) => {
      const ga = gaps[a];
      const gb = gaps[b];
      const na = Number.isNaN(ga);
      const nb = Number.isNaN(gb);
      if (na || nb) return na === nb ? a - b : na ? 1 : -1;
      return ga === gb ? a - b : ga - gb;
    });

    const sel = order.slice(0, half).sort((a, b) => a - b);
    indices.push(sel);
    gapsOut.push(sel.map((i) => roundTo(gaps[i], 6)));
    discAngle.push(-phi / g.Zc);
    const c0 = p.eccentricity * Math.cos(phi);
    const c1 = p.eccentricity * Math.sin(phi);
    center.push(digits > 0 ? [roundTo(c0, digits), roundTo(c1, digits)] : [c0, c1]);
  }

  return { steps: n, pin_indices: indices, gaps: gapsOut, disc_angle: discAngle, center };
}

/* ---------------------------------------------------------------------------
 * 主入口：与 POST /api/analyze 同构
 * ------------------------------------------------------------------------ */

/**
 * 完整几何分析（轮廓 + 派生量 + 告警 + 可选接触表）。
 *
 * @param {object} params 与后端 ParamsIn 同名的参数字段（缺省项按默认值补齐）
 * @param {object} [opts] { contactSteps | contact_steps | steps, samples, round }
 * @returns {object} 与 /api/analyze 响应同构的对象：
 *   { params, derived, warnings, profile: {actual, theoretical},
 *     pin_ring, output, contacts? }
 */
export function analyze(params, opts = {}) {
  const options = opts || {};
  const p = normalizeParams(params);
  const g = makeGeom(p);

  const digits = options.round === false || options.round === 0 ? 0 : Math.trunc(num(options.round, 4));
  const roundPt = (pt) => (digits > 0 ? [roundTo(pt[0], digits), roundTo(pt[1], digits)] : [pt[0], pt[1]]);

  const sampleOverride = num(options.samples, 0);
  const count = sampleOverride > 0 ? sampleOverride : p.tooth_count * p.samples_per_tooth;
  const tau = sampleTau(count);

  const theo = theoreticalProfile(g, tau);
  const act = actualProfile(g, tau);
  const deriv = profileDerivatives(g, tau);

  // --- 半径极值（实际齿廓 / 理论齿廓）
  let tipRadius = -Infinity;
  let rootRadius = Infinity;
  for (let i = 0; i < act.length; i += 1) {
    const r = hypot2(act[i][0], act[i][1]);
    if (r > tipRadius) tipRadius = r;
    if (r < rootRadius) rootRadius = r;
  }
  let theoreticalTipRadius = -Infinity;
  for (let i = 0; i < theo.length; i += 1) {
    const r = hypot2(theo[i][0], theo[i][1]);
    if (r > theoreticalTipRadius) theoreticalTipRadius = r;
  }

  const out = outputLayout(p, g);
  const ring = ringRadii(p);

  // --- 曲率半径 |ρ| 的最小值与法线尺度因子最小值
  let minCurv = Infinity;
  let curvNaN = false;
  for (let i = 0; i < tau.length; i += 1) {
    const numerator = deriv.dx[i] * deriv.ddy[i] - deriv.dy[i] * deriv.ddx[i];
    const den = Math.pow(Math.max(deriv.s[i], 1e-12), 3);
    const kappa = numerator / den;
    let rho = kappa === 0 ? Infinity : 1 / kappa; // 1/0 → ±Infinity
    if (!Number.isFinite(rho)) {
      // numpy: where(isfinite(rho), rho, sign(num) * inf)，num == 0 时得到 NaN
      rho = Math.sign(numerator) * Infinity;
    }
    const abs = Math.abs(rho);
    if (Number.isNaN(abs)) curvNaN = true;
    else if (abs < minCurv) minCurv = abs;
  }
  if (curvNaN) minCurv = NaN;

  let sMin = Infinity;
  for (let i = 0; i < tau.length; i += 1) {
    if (deriv.s[i] < sMin) sMin = deriv.s[i];
  }

  // --- 星形性 / 自交检测
  const star = starMetrics(act);

  // --- 齿廓周长（折线闭合，含末点到首点）
  let profileLength = 0;
  for (let i = 0; i < act.length; i += 1) {
    const nxt = act[(i + 1) % act.length];
    profileLength += hypot2(nxt[0] - act[i][0], nxt[1] - act[i][1]);
  }

  const k1 = (p.eccentricity * g.Zp) / p.pin_circle_radius;
  const tipClearanceLimit = g.Rzp - g.eZp;

  const warnings = collectWarnings(
    {
      short_amplitude_coefficient: k1,
      effective_pin_radius: g.rpp,
      pin_circle_radius: p.pin_circle_radius,
      eccentricity: p.eccentricity,
      pin_radius: p.pin_radius,
    },
    out,
    sMin,
    rootRadius,
    star.star_shaped,
  );

  // --- 与 geometry.CycloidParams.to_dict() 同序同值（含派生量）
  const paramsOut = {
    tooth_count: p.tooth_count,
    eccentricity: p.eccentricity,
    pin_circle_radius: p.pin_circle_radius,
    pin_radius: p.pin_radius,
    equidistant_offset: p.equidistant_offset,
    radial_offset: p.radial_offset,
    rotary_offset_deg: p.rotary_offset_deg,
    ring_wall: p.ring_wall,
    pin_hole_count: p.pin_hole_count,
    pin_hole_circle_radius: p.pin_hole_circle_radius,
    pin_sleeve_radius: p.pin_sleeve_radius,
    clearance: p.clearance,
    center_bore_radius: p.center_bore_radius,
    samples_per_tooth: p.samples_per_tooth,
    pin_count: g.Zp,
    effective_pin_circle_radius: g.Rzp,
    effective_pin_radius: g.rpp,
    short_amplitude_coefficient: k1,
    ratio: p.tooth_count,
  };

  const derived = {
    pin_count: g.Zp,
    ratio: p.tooth_count,
    short_amplitude_coefficient: k1,
    root_radius: rootRadius,
    tip_radius: tipRadius,
    theoretical_tip_radius: theoreticalTipRadius,
    tooth_height: tipRadius - rootRadius,
    tooth_pitch_angle_deg: 360.0 / p.tooth_count,
    min_curvature_radius: minCurv, // 理论齿廓（针齿中心轨迹）的最小曲率半径
    normal_scale_min: sMin,
    star_shaped: star.star_shaped,
    fold_ratio: star.fold_ratio,
    theoretical_backlash: p.equidistant_offset,
    pin_ring: { pin_outer: ring.pin_outer, inner: ring.inner, outer: ring.outer },
    output: {
      count: out.count,
      circle_radius: out.circle_radius,
      sleeve_radius: out.sleeve_radius,
      hole_radius: out.hole_radius,
      center_bore_radius: out.center_bore_radius,
    },
    profile_length: profileLength,
    undercut_limit: tipClearanceLimit,
  };

  const result = {
    params: paramsOut,
    derived,
    warnings,
    profile: {
      actual: act.map(roundPt),
      theoretical: theo.map(roundPt),
    },
    pin_ring: {
      pin_centers: pinCenters(p).map(roundPt),
      pin_radius: p.pin_radius,
      pitch_radius: p.pin_circle_radius,
      outer_radius: ring.outer,
      inner_radius: ring.inner,
      pin_outer_radius: ring.pin_outer,
    },
    output: {
      hole_centers: out.hole_centers.map(roundPt),
      hole_radius: out.hole_radius,
      sleeve_radius: out.sleeve_radius,
      circle_radius: out.circle_radius,
      count: out.count,
      center_bore_radius: out.center_bore_radius,
    },
  };

  // --- 接触表（contact_steps = 0 时后端不返回，这里同样省略）
  // 选项名兼容 contactSteps / contact_steps / steps（前端三种写法都用过）
  const requested = Math.trunc(num(options.contactSteps ?? options.contact_steps ?? options.steps, 0));
  const steps = Math.min(Math.max(requested, 0), 1440);
  if (steps > 0) result.contacts = contactTable(p, steps, digits);

  return result;
}
