/* =============================================================================
 * 摆线针轮轮廓生成与仿真器 —— 前端主逻辑
 *
 * 几何与后端 app/geometry.py 完全一致：
 *   理论齿廓 P(τ) = ( Rz'·cosτ - e·cos(Zp·τ),  Rz'·sinτ - e·sin(Zp·τ) )
 *   S(τ)         = sqrt( Rz'² + (eZp)² - 2·Rz'·eZp·cos(Zc·τ) )
 *   外法线 N(τ)  = ( Rz'cosτ - eZp·cos(Zpτ),  Rz'sinτ - eZp·sin(Zpτ) ) / S
 *   实际齿廓 Q(τ)= P(τ) - rp'·N(τ)
 * 其中 Rz' = Rz - ΔRz，rp' = rp + Δrp。
 *
 * 运动学：摆线轮中心 C = e·(cosφ, sinφ)，自转角 θc = -φ/Zc，
 * 第 i 个针齿（ψi = 2πi/Zp）与齿廓的接触点为 Q(ψi + φ/Zc)。
 * ========================================================================== */

'use strict';

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

/* ---------------------------------------------------------------------------
 * 参数 schema（顺序即 UI 顺序）
 * ------------------------------------------------------------------------ */

const GROUPS = [
  {
    name: '基本参数',
    items: [
      { key: 'tooth_count', label: '摆线轮齿数 Zc', unit: '', min: 6, max: 120, step: 1, int: true, hint: 'Zp = Zc+1' },
      { key: 'eccentricity', label: '偏心距 e', unit: 'mm', min: 0.2, max: 16, step: 0.01 },
      { key: 'pin_circle_radius', label: '针齿中心圆半径 Rz', unit: 'mm', min: 12, max: 300, step: 0.5 },
      { key: 'pin_radius', label: '针齿半径 rp', unit: 'mm', min: 0.2, max: 40, step: 0.1 },
    ],
  },
  {
    name: '齿廓修形',
    items: [
      { key: 'equidistant_offset', label: '等距修形 Δrp', unit: 'mm', min: -0.3, max: 0.4, step: 0.005 },
      { key: 'radial_offset', label: '移距修形 ΔRz', unit: 'mm', min: -0.3, max: 0.4, step: 0.005 },
      { key: 'rotary_offset_deg', label: '转角修形', unit: '°', min: -0.4, max: 0.4, step: 0.005 },
    ],
  },
  {
    name: '输出机构 · 柱销',
    items: [
      { key: 'pin_hole_count', label: '柱销数 Zw', unit: '', min: 0, max: 32, step: 1, int: true, auto: '自动' },
      { key: 'pin_hole_circle_radius', label: '柱销分布圆半径 Rw', unit: 'mm', min: 0, max: 240, step: 0.5, auto: '自动' },
      { key: 'pin_sleeve_radius', label: '柱销套半径 rw', unit: 'mm', min: 0, max: 30, step: 0.1, auto: '自动' },
      { key: 'clearance', label: '柱销孔单边间隙', unit: 'mm', min: 0, max: 0.4, step: 0.005 },
      { key: 'center_bore_radius', label: '中心孔半径', unit: 'mm', min: 0, max: 160, step: 0.5, auto: '自动' },
    ],
  },
  {
    name: '壳体与精度',
    items: [
      { key: 'ring_wall', label: '针齿壳壁厚', unit: 'mm', min: 2, max: 60, step: 0.5 },
      { key: 'samples_per_tooth', label: '每齿采样点数', unit: '', min: 8, max: 256, step: 8, int: true },
    ],
  },
];

const DEFAULTS = {
  tooth_count: 39,
  eccentricity: 1.43,
  pin_circle_radius: 88,
  pin_radius: 5.5,
  equidistant_offset: 0.02,
  radial_offset: 0,
  rotary_offset_deg: 0,
  ring_wall: 12,
  pin_hole_count: 0,
  pin_hole_circle_radius: 0,
  pin_sleeve_radius: 0,
  clearance: 0,
  center_bore_radius: 0,
  samples_per_tooth: 64,
};

const TOGGLES = [
  { key: 'pins', label: '针齿', on: true },
  { key: 'ring', label: '壳体', on: true },
  { key: 'theory', label: '理论廓', on: true },
  { key: 'holes', label: '柱销孔', on: true },
  { key: 'sleeves', label: '柱销', on: true },
  { key: 'contacts', label: '接触点', on: true },
  { key: 'grid', label: '极坐标网格', on: false },
];

/* ---------------------------------------------------------------------------
 * 状态
 * ------------------------------------------------------------------------ */

const state = {
  params: { ...DEFAULTS },
  data: null,
  busy: false,
  error: null,
  view: { scale: 4, cx: 0, cy: 0 },
  anim: { playing: false, speed: 0.7, phi: 0 },
  show: Object.fromEntries(TOGGLES.map((t) => [t.key, t.on])),
  exportPart: 'cycloid_disc',
};

const size = { w: 800, h: 600, dpr: 1 };

let canvas, ctx;
let analyzeTimer = null;
let analyzeSeq = 0;

/* ---------------------------------------------------------------------------
 * 小工具
 * ------------------------------------------------------------------------ */

const $ = (sel) => document.querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const box = $('#toast');
  box.textContent = msg;
  box.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.className = 'toast'), 2600);
}

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

function fmt(v, digits = 2) {
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/* ---------------------------------------------------------------------------
 * API
 * ------------------------------------------------------------------------ */

async function postJson(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res;
}

function scheduleAnalyze(immediate = false) {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(analyze, immediate ? 0 : 70);
}

async function analyze() {
  const seq = ++analyzeSeq;
  state.busy = true;
  try {
    const res = await postJson('/api/analyze', { params: state.params, contact_steps: 0 });
    const data = await res.json();
    if (seq !== analyzeSeq) return; // 丢弃过期响应
    state.data = data;
    state.error = null;
    renderMetrics();
    updateUrl();
    if (!state.view.fitted) fitView();
    else draw();
  } catch (err) {
    if (seq !== analyzeSeq) return;
    state.error = String(err.message || err);
    renderMetrics();
    toast('计算失败：' + state.error, true);
  } finally {
    state.busy = false;
    draw();
  }
}

/* ---------------------------------------------------------------------------
 * 前端几何（与后端一致）
 * ------------------------------------------------------------------------ */

function geom(params = state.params) {
  const Zc = Math.max(1, Math.round(params.tooth_count));
  return {
    Zc,
    Zp: Zc + 1,
    e: params.eccentricity,
    Rz: params.pin_circle_radius,
    rp: params.pin_radius,
    Rzp: params.pin_circle_radius - params.radial_offset,
    rpp: params.pin_radius + params.equidistant_offset,
    rot: params.rotary_offset_deg * DEG,
  };
}

/** 实际齿廓上参数 τ 处的点（摆线轮系） */
function profilePoint(tau, g = geom()) {
  const { Zc, Zp, e, Rzp, rpp } = g;
  const c = Math.cos(tau);
  const s = Math.sin(tau);
  const cz = Math.cos(Zp * tau);
  const sz = Math.sin(Zp * tau);

  const dx = -Rzp * s + e * Zp * sz;
  const dy = Rzp * c - e * Zp * cz;
  const S = Math.sqrt(Math.max(Rzp * Rzp + (e * Zp) ** 2 - 2 * Rzp * e * Zp * Math.cos(Zc * tau), 1e-18));
  const nx = dy / S;
  const ny = -dx / S;

  let x = Rzp * c - e * cz - rpp * nx;
  let y = Rzp * s - e * sz - rpp * ny;

  if (Math.abs(g.rot) > 1e-12) {
    const cr = Math.cos(-g.rot);
    const sr = Math.sin(-g.rot);
    const rx = x * cr - y * sr;
    const ry = x * sr + y * cr;
    x = rx;
    y = ry;
  }
  return [x, y];
}

/** 摆线轮系 → 世界系的变换参数 */
function discPose(phi = state.anim.phi, g = geom()) {
  const theta = -phi / g.Zc;
  return {
    cx: g.e * Math.cos(phi),
    cy: g.e * Math.sin(phi),
    cos: Math.cos(theta),
    sin: Math.sin(theta),
    theta,
  };
}

function discToWorld(p, pose) {
  return [pose.cx + p[0] * pose.cos - p[1] * pose.sin, pose.cy + p[0] * pose.sin + p[1] * pose.cos];
}

/* ---------------------------------------------------------------------------
 * 视图变换
 * ------------------------------------------------------------------------ */

function w2s(x, y) {
  return [
    size.w / 2 + (x - state.view.cx) * state.view.scale,
    size.h / 2 - (y - state.view.cy) * state.view.scale,
  ];
}

function s2w(sx, sy) {
  return [
    state.view.cx + (sx - size.w / 2) / state.view.scale,
    state.view.cy - (sy - size.h / 2) / state.view.scale,
  ];
}

function fitView() {
  const outer = state.data?.pin_ring?.outer_radius || 110;
  const r = outer * 1.1;
  state.view.scale = Math.min(size.w, size.h) / (2 * r);
  state.view.cx = 0;
  state.view.cy = 0;
  state.view.fitted = true;
  draw();
}

/* ---------------------------------------------------------------------------
 * 渲染
 * ------------------------------------------------------------------------ */

function resize() {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  size.w = Math.max(rect.width, 1);
  size.h = Math.max(rect.height, 1);
  size.dpr = dpr;
  canvas.width = Math.round(size.w * dpr);
  canvas.height = Math.round(size.h * dpr);
  draw();
}

function applyWorldTransform() {
  const { scale } = state.view;
  ctx.setTransform(
    size.dpr * scale, 0, 0, -size.dpr * scale,
    size.dpr * (size.w / 2 - state.view.cx * scale),
    size.dpr * (size.h / 2 + state.view.cy * scale),
  );
}

function strokeWidth(px) {
  return px / state.view.scale;
}

function drawGrid(g) {
  const { Zc, Zp, Rz } = g;
  ctx.save();
  applyWorldTransform();

  if (state.show.grid) {
    const rMax = (state.data?.pin_ring?.outer_radius || Rz * 1.2) * 1.05;
    const ringStep = niceStep(rMax / 5);
    ctx.strokeStyle = 'rgba(88,166,255,0.09)';
    ctx.lineWidth = strokeWidth(1);
    for (let r = ringStep; r <= rMax * 1.4; r += ringStep) {
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, TAU);
      ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(88,166,255,0.07)';
    for (let k = 0; k < 12; k++) {
      const a = (k / 12) * TAU;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(Math.cos(a) * rMax * 1.3, Math.sin(a) * rMax * 1.3);
      ctx.stroke();
    }
  }

  // 中心十字
  const ext = (state.data?.pin_ring?.outer_radius || Rz * 1.2) * 1.06;
  ctx.strokeStyle = 'rgba(248,81,73,0.45)';
  ctx.lineWidth = strokeWidth(0.8);
  ctx.setLineDash([strokeWidth(16), strokeWidth(4), strokeWidth(3), strokeWidth(4)]);
  ctx.beginPath();
  ctx.moveTo(-ext, 0); ctx.lineTo(ext, 0);
  ctx.moveTo(0, -ext); ctx.lineTo(0, ext);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-6))));
  const n = raw / p;
  const m = n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10;
  return m * p;
}

function draw() {
  if (!ctx) return;
  ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  ctx.clearRect(0, 0, size.w, size.h);

  const g = geom();
  const d = state.data;

  drawGrid(g);

  if (!d) {
    drawHint('正在生成几何…');
    return;
  }

  const pose = discPose(state.anim.phi, geom(d.params));
  const gd = geom(d.params);

  applyWorldTransform();

  // ---- 针齿壳 ----
  if (state.show.ring) {
    ctx.strokeStyle = 'rgba(88,166,255,0.55)';
    ctx.lineWidth = strokeWidth(2.2);
    ctx.beginPath();
    ctx.arc(0, 0, d.pin_ring.inner_radius, 0, TAU);
    ctx.stroke();
    ctx.strokeStyle = 'rgba(88,166,255,0.3)';
    ctx.lineWidth = strokeWidth(1.4);
    ctx.beginPath();
    ctx.arc(0, 0, d.pin_ring.outer_radius, 0, TAU);
    ctx.stroke();

    // 针齿中心圆
    ctx.setLineDash([strokeWidth(7), strokeWidth(6)]);
    ctx.strokeStyle = 'rgba(139,148,158,0.4)';
    ctx.lineWidth = strokeWidth(1);
    ctx.beginPath();
    ctx.arc(0, 0, d.pin_ring.pitch_radius, 0, TAU);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---- 针齿 ----
  if (state.show.pins) {
    ctx.fillStyle = 'rgba(88,166,255,0.13)';
    ctx.strokeStyle = 'rgba(88,166,255,0.95)';
    ctx.lineWidth = strokeWidth(1.4);
    ctx.beginPath();
    for (const [px, py] of d.pin_ring.pin_centers) {
      ctx.moveTo(px + d.pin_ring.pin_radius, py);
      ctx.arc(px, py, d.pin_ring.pin_radius, 0, TAU);
    }
    ctx.fill();
    ctx.stroke();
  }

  // ---- 摆线轮（含孔，evenodd 镂空）----
  ctx.save();
  ctx.translate(pose.cx, pose.cy);
  ctx.rotate(pose.theta);

  ctx.beginPath();
  addDiscPath(d.profile.actual);
  if (d.output.center_bore_radius > 0) {
    ctx.moveTo(d.output.center_bore_radius, 0);
    ctx.arc(0, 0, d.output.center_bore_radius, 0, TAU, true);
  }
  if (state.show.holes) {
    for (const [hx, hy] of d.output.hole_centers) {
      ctx.moveTo(hx + d.output.hole_radius, hy);
      ctx.arc(hx, hy, d.output.hole_radius, 0, TAU, true);
    }
  }
  const grad = ctx.createRadialGradient(0, 0, d.derived.root_radius * 0.2, 0, 0, d.derived.tip_radius);
  grad.addColorStop(0, 'rgba(46,160,67,0.20)');
  grad.addColorStop(1, 'rgba(63,185,80,0.08)');
  ctx.fillStyle = grad;
  ctx.fill('evenodd');

  ctx.strokeStyle = '#3fb950';
  ctx.lineWidth = strokeWidth(1.8);
  ctx.beginPath();
  addDiscPath(d.profile.actual);
  ctx.stroke();

  // 理论齿廓
  if (state.show.theory) {
    ctx.setLineDash([strokeWidth(6), strokeWidth(5)]);
    ctx.strokeStyle = 'rgba(139,148,158,0.65)';
    ctx.lineWidth = strokeWidth(1);
    ctx.beginPath();
    addDiscPath(d.profile.theoretical, false);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // 中心孔与柱销孔轮廓
  ctx.strokeStyle = 'rgba(210,153,34,0.85)';
  ctx.lineWidth = strokeWidth(1.3);
  ctx.beginPath();
  if (d.output.center_bore_radius > 0) {
    ctx.moveTo(d.output.center_bore_radius, 0);
    ctx.arc(0, 0, d.output.center_bore_radius, 0, TAU);
  }
  if (state.show.holes) {
    for (const [hx, hy] of d.output.hole_centers) {
      ctx.moveTo(hx + d.output.hole_radius, hy);
      ctx.arc(hx, hy, d.output.hole_radius, 0, TAU);
    }
  }
  ctx.stroke();

  // 柱销（与孔同心，半径 rw —— 展示偏心摆动机构的原理）
  if (state.show.sleeves) {
    ctx.fillStyle = 'rgba(210,153,34,0.30)';
    ctx.strokeStyle = 'rgba(210,153,34,0.95)';
    ctx.lineWidth = strokeWidth(1.2);
    ctx.beginPath();
    for (const [hx, hy] of d.output.hole_centers) {
      ctx.moveTo(hx + d.output.sleeve_radius, hy);
      ctx.arc(hx, hy, d.output.sleeve_radius, 0, TAU);
    }
    ctx.fill();
    ctx.stroke();
  }

  ctx.restore();

  // ---- 偏心指示（转臂）----
  ctx.strokeStyle = 'rgba(255,123,114,0.85)';
  ctx.lineWidth = strokeWidth(1.2);
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(pose.cx, pose.cy);
  ctx.stroke();
  ctx.fillStyle = 'rgba(255,123,114,0.9)';
  ctx.beginPath();
  ctx.arc(pose.cx, pose.cy, strokeWidth(4), 0, TAU);
  ctx.fill();
  ctx.strokeStyle = 'rgba(255,123,114,0.5)';
  ctx.beginPath();
  ctx.arc(0, 0, Math.max(gd.e, strokeWidth(3)), 0, TAU);
  ctx.stroke();

  // ---- 接触点 ----
  if (state.show.contacts) {
    const Zp = gd.Zp;
    const rDot = strokeWidth(2.4);
    ctx.fillStyle = 'rgba(240,246,252,0.85)';
    ctx.beginPath();
    for (let i = 0; i < Zp; i++) {
      const tau = (TAU * i) / Zp + state.anim.phi / gd.Zc;
      const p = profilePoint(tau, gd);
      const w = discToWorld(p, pose);
      ctx.moveTo(w[0] + rDot, w[1]);
      ctx.arc(w[0], w[1], rDot, 0, TAU);
    }
    ctx.fill();
  }

  // ---- 屏幕层：HUD 数值由 DOM 负责；这里画比例尺 ----
  ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  drawScaleBar();
  updateHud(gd, pose);
}

function addDiscPath(pts, moveFirst = true) {
  if (!pts || !pts.length) return;
  if (moveFirst) ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.closePath();
}

function drawHint(text) {
  ctx.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  ctx.fillStyle = 'rgba(201,209,217,0.55)';
  ctx.font = '13px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(text, size.w / 2, size.h / 2);
  ctx.textAlign = 'start';
}

function drawScaleBar() {
  const target = size.w * 0.13;
  const step = niceStep(target / state.view.scale);
  const px = step * state.view.scale;
  if (!Number.isFinite(px) || px < 12 || px > size.w * 0.5) return;

  const x0 = 16;
  const y0 = size.h - 22;
  ctx.strokeStyle = 'rgba(201,209,217,0.6)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x0, y0 - 4); ctx.lineTo(x0, y0);
  ctx.lineTo(x0 + px, y0); ctx.lineTo(x0 + px, y0 - 4);
  ctx.moveTo(x0, y0 - 2); ctx.lineTo(x0 + px, y0 - 2);
  ctx.stroke();
  ctx.fillStyle = 'rgba(201,209,217,0.75)';
  ctx.font = '10px ui-monospace, monospace';
  ctx.fillText(`${step} mm`, x0, y0 - 8);
}

function updateHud(g, pose) {
  const hud = $('#hud');
  const ratio = g.Zc;
  const phiDeg = (state.anim.phi * 180) / Math.PI;
  const outDeg = (-state.anim.phi / g.Zc) * 180 / Math.PI;
  const d = state.data?.derived;
  const warnCount = (state.data?.warnings || []).filter((w) => w.level !== 'info').length;
  hud.innerHTML = `
    <div>Zc=<b>${g.Zc}</b> &nbsp; Zp=<b>${g.Zp}</b> &nbsp; K1=<b>${fmt(d?.short_amplitude_coefficient ?? 0, 3)}</b></div>
    <div>φ=<b>${fmt(phiDeg, 1)}°</b> &nbsp; 输出=<b>${fmt(outDeg, 2)}°</b> &nbsp; i=<b>${ratio}:1</b></div>
    <div>${state.view.scale.toFixed(2)} px/mm &nbsp; ${warnCount ? `<span class="bad">■ ${warnCount} 项告警</span>` : '<span class="ok">■ 几何有效</span>'}</div>
  `;
}

/* ---------------------------------------------------------------------------
 * 指标面板
 * ------------------------------------------------------------------------ */

function renderMetrics() {
  const root = $('#metricsPanel');
  root.innerHTML = '';

  if (state.error) {
    const box = el('div', 'alert error');
    box.innerHTML = `<span class="tag">ERR</span><div>${escapeHtml(state.error)}</div>`;
    root.appendChild(box);
    return;
  }
  const d = state.data?.derived;
  if (!d) {
    root.appendChild(el('div', 'group-head', '指标'));
    root.appendChild(el('div', 'kv', '计算中…'));
    return;
  }

  const head = el('div', 'group-head', '传动指标');
  root.appendChild(head);

  const grid = el('div', 'metric-grid');
  const cards = [
    { k: '传动比 i（针轮固定）', v: `${d.ratio.toFixed(0)} : 1`, cls: 'wide good' },
    { k: '齿数 Zc / Zp', v: `${d.pin_count - 1} / ${d.pin_count}`, cls: '' },
    { k: '短幅系数 K1', v: fmt(d.short_amplitude_coefficient, 3), cls: d.short_amplitude_coefficient < 1 ? 'good' : 'bad' },
    { k: '齿顶圆半径', v: `${fmt(d.tip_radius, 3)}<small>mm</small>`, cls: '' },
    { k: '齿根圆半径', v: `${fmt(d.root_radius, 3)}<small>mm</small>`, cls: '' },
    { k: '齿高', v: `${fmt(d.tooth_height, 3)}<small>mm</small>`, cls: '' },
    { k: '齿距角', v: `${fmt(d.tooth_pitch_angle_deg, 3)}<small>°</small>`, cls: '' },
    { k: '齿廓周长', v: `${fmt(d.profile_length, 1)}<small>mm</small>`, cls: '' },
    { k: '理论法向侧隙', v: `${fmt(d.theoretical_backlash, 3)}<small>mm</small>`, cls: '' },
    { k: '柱销数 Zw', v: `${d.output.count}<small>个</small>`, cls: '' },
    { k: '柱销孔/套', v: `${fmt(d.output.hole_radius, 2)}/${fmt(d.output.sleeve_radius, 2)}`, cls: '' },
    { k: '中心孔半径', v: `${fmt(d.output.center_bore_radius, 2)}<small>mm</small>`, cls: '' },
  ];
  for (const c of cards) {
    const box = el('div', 'metric ' + c.cls);
    box.appendChild(el('div', 'k', c.k));
    const v = el('div', 'v');
    v.innerHTML = c.v;
    box.appendChild(v);
    grid.appendChild(box);
  }
  root.appendChild(grid);

  // 齿廓状态
  const statusHead = el('div', 'group-head', '齿廓状态');
  root.appendChild(statusHead);
  const kv = el('div', 'kv-list');
  const rows = [
    ['星形闭合', d.star_shaped ? '是' : '否（自交）'],
    ['最小曲率半径', `${fmt(d.min_curvature_radius, 3)} mm`],
    ['根切临界 Rz−e·Zp', `${fmt(d.undercut_limit, 3)} mm`],
    ['法线尺度 S 最小值', fmt(d.normal_scale_min, 3)],
  ];
  for (const [k, v] of rows) {
    const row = el('div', 'kv');
    row.appendChild(el('span', 'k', k));
    row.appendChild(el('span', 'v', v));
    kv.appendChild(row);
  }
  root.appendChild(kv);

  // 告警
  const warns = state.data.warnings || [];
  root.appendChild(el('div', 'group-head', `诊断（${warns.length}）`));
  const list = el('div', 'alerts');
  if (!warns.length) {
    const ok = el('div', 'alert info');
    ok.innerHTML = '<span class="tag">OK</span><div>几何参数合法，齿廓可用于加工。</div>';
    list.appendChild(ok);
  } else {
    for (const w of warns) {
      const box = el('div', 'alert ' + w.level);
      box.innerHTML = `<span class="tag">${w.level === 'error' ? 'ERR' : w.level === 'warn' ? 'WARN' : 'INFO'}</span><div>${escapeHtml(w.message)}</div>`;
      list.appendChild(box);
    }
  }
  root.appendChild(list);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------------------------------------------------------------------
 * UI 构建
 * ------------------------------------------------------------------------ */

function buildParamsUI() {
  const root = $('#paramsPanel');
  root.innerHTML = '';
  for (const group of GROUPS) {
    const g = el('div', 'group');
    g.appendChild(el('div', 'group-head', group.name));
    for (const item of group.items) g.appendChild(buildControl(item));
    root.appendChild(g);
  }
}

const controlRefs = new Map();

function buildControl(item) {
  const wrap = el('div', 'ctrl');
  const head = el('div', 'ctrl-head');
  head.appendChild(el('span', 'name', item.label));
  head.appendChild(el('span', 'hint', item.hint || (item.unit ? `[${item.unit}]` : '')));

  const body = el('div', 'ctrl-body');
  const range = document.createElement('input');
  range.type = 'range';
  range.min = item.min;
  range.max = item.max;
  range.step = item.step;
  range.value = state.params[item.key];

  const num = document.createElement('input');
  num.type = 'number';
  num.min = item.min;
  num.max = item.max;
  num.step = item.step;
  if (item.auto) num.placeholder = item.auto;

  const sync = (value, from) => {
    let v = Number(value);
    if (!Number.isFinite(v)) return;
    if (item.int) v = Math.round(v);
    v = clamp(v, item.min, item.max);
    state.params[item.key] = v;
    if (from !== 'range') range.value = v;
    if (from !== 'num') num.value = item.auto && v === 0 ? '' : v;
    scheduleAnalyze();
  };

  range.addEventListener('input', () => sync(range.value, 'range'));
  num.addEventListener('input', () => {
    if (num.value === '') {
      state.params[item.key] = 0;
      range.value = 0;
      scheduleAnalyze();
      return;
    }
    sync(num.value, 'num');
  });

  num.value = item.auto && state.params[item.key] === 0 ? '' : state.params[item.key];
  body.appendChild(range);
  body.appendChild(num);
  wrap.appendChild(head);
  wrap.appendChild(body);
  controlRefs.set(item.key, { range, num, item });
  return wrap;
}

function syncControls() {
  for (const [key, ref] of controlRefs) {
    const v = state.params[key];
    ref.range.value = v;
    ref.num.value = ref.item.auto && v === 0 ? '' : v;
  }
}

function buildToggles() {
  const root = $('#toggles');
  root.innerHTML = '';
  for (const t of TOGGLES) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!state.show[t.key];
    input.addEventListener('change', () => {
      state.show[t.key] = input.checked;
      draw();
    });
    label.appendChild(input);
    label.appendChild(document.createTextNode(t.label));
    root.appendChild(label);
  }
}

async function loadPresets() {
  const sel = $('#presetSelect');
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '自定义…';
  sel.appendChild(placeholder);
  try {
    const res = await fetch('/api/presets');
    const json = await res.json();
    for (const p of json.presets) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      opt.title = p.note || '';
      opt.dataset.params = JSON.stringify(p.params);
      sel.appendChild(opt);
    }
  } catch {
    /* 预设加载失败不影响主流程 */
  }
  sel.addEventListener('change', () => {
    const opt = sel.selectedOptions[0];
    if (!opt || !opt.dataset.params) return;
    state.params = { ...DEFAULTS, ...JSON.parse(opt.dataset.params) };
    syncControls();
    state.view.fitted = false;
    scheduleAnalyze(true);
    toast('已载入预设：' + opt.textContent);
  });
}

/* ---------------------------------------------------------------------------
 * 动画
 * ------------------------------------------------------------------------ */

let lastFrame = 0;

function loop(t) {
  const dt = lastFrame ? Math.min((t - lastFrame) / 1000, 0.1) : 0;
  lastFrame = t;
  if (state.anim.playing) {
    state.anim.phi = (state.anim.phi + state.anim.speed * 0.9 * dt) % TAU;
    syncPhiUI();
    draw();
  }
  requestAnimationFrame(loop);
}

function syncPhiUI() {
  $('#phi').value = ((state.anim.phi * 180) / Math.PI).toFixed(1);
  $('#phiVal').textContent = `${((state.anim.phi * 180) / Math.PI).toFixed(1)}°`;
}

function setPlaying(on) {
  state.anim.playing = on;
  $('#btnPlay').textContent = on ? '⏸' : '▶';
  $('#btnPlay').classList.toggle('primary', !on);
  lastFrame = 0;
}

/* ---------------------------------------------------------------------------
 * 导出
 * ------------------------------------------------------------------------ */

function exportPayload() {
  const part = state.exportPart;
  if (part === 'all_separate') {
    return { parts: ['cycloid_disc', 'pin_ring', 'output_disc'], options: {} };
  }
  return { parts: [part], options: {} };
}

async function exportDxf() {
  const btn = $('#btnDxf');
  btn.disabled = true;
  try {
    const { parts } = exportPayload();
    const res = await postJson('/api/export/dxf', {
      params: state.params,
      parts,
      include_theoretical: true,
      include_centerlines: true,
      label: true,
    });
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = /filename="([^"]+)"/.exec(cd);
    downloadBlob(blob, m ? m[1] : 'cycloid.dxf');
    toast(`已导出 DXF（${(blob.size / 1024).toFixed(0)} KB）`);
  } catch (err) {
    toast('DXF 导出失败：' + err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function exportSvg() {
  try {
    const { parts } = exportPayload();
    const res = await postJson('/api/export/svg', { params: state.params, parts });
    downloadBlob(await res.blob(), 'cycloid.svg');
    toast('已导出 SVG');
  } catch (err) {
    toast('SVG 导出失败：' + err.message, true);
  }
}

function exportPng() {
  canvas.toBlob((blob) => {
    if (!blob) return toast('PNG 导出失败', true);
    downloadBlob(blob, 'cycloid_view.png');
    toast('已导出 PNG 截图');
  }, 'image/png');
}

function exportJson() {
  const payload = {
    generator: 'echo-cad-toolbox',
    version: '1.0.0',
    exported_at: new Date().toISOString(),
    params: state.params,
    derived: state.data?.derived ?? null,
    warnings: state.data?.warnings ?? [],
  };
  downloadBlob(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }), 'cycloid_params.json');
  toast('已导出参数 JSON');
}

/* ---------------------------------------------------------------------------
 * URL 分享
 * ------------------------------------------------------------------------ */

function encodeParams(params) {
  const json = JSON.stringify(params);
  // URL-safe base64（+ → -，/ → _，去掉填充），可直接放进查询串分享
  return btoa(unescape(encodeURIComponent(json)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

function decodeParams(str) {
  try {
    const b64 = str.replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(escape(atob(b64)));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function updateUrl() {
  const hash = encodeParams(state.params);
  history.replaceState(null, '', `?p=${hash}`);
}

function loadFromUrl() {
  const m = /[?&]p=([^&]+)/.exec(location.search);
  if (!m) return false;
  const parsed = decodeParams(m[1]);
  if (!parsed) return false;
  state.params = { ...DEFAULTS, ...parsed };
  return true;
}

/* ---------------------------------------------------------------------------
 * 事件
 * ------------------------------------------------------------------------ */

function bindEvents() {
  // --- 画布交互 ---
  let dragging = false;
  let last = null;

  canvas.addEventListener('pointerdown', (ev) => {
    dragging = true;
    last = [ev.clientX, ev.clientY];
    canvas.classList.add('dragging');
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener('pointermove', (ev) => {
    if (!dragging || !last) return;
    const dx = ev.clientX - last[0];
    const dy = ev.clientY - last[1];
    last = [ev.clientX, ev.clientY];
    state.view.cx -= dx / state.view.scale;
    state.view.cy += dy / state.view.scale;
    draw();
  });
  const endDrag = () => {
    dragging = false;
    last = null;
    canvas.classList.remove('dragging');
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);

  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    const before = s2w(mx, my);
    const factor = Math.exp(-ev.deltaY * 0.0016);
    state.view.scale = clamp(state.view.scale * factor, 0.05, 400);
    const after = s2w(mx, my);
    state.view.cx += before[0] - after[0];
    state.view.cy += before[1] - after[1];
    draw();
  }, { passive: false });

  canvas.addEventListener('dblclick', fitView);

  // --- 动画控制 ---
  $('#btnPlay').addEventListener('click', () => setPlaying(!state.anim.playing));
  $('#btnStep').addEventListener('click', () => {
    setPlaying(false);
    state.anim.phi = (state.anim.phi + TAU / 360) % TAU;
    syncPhiUI();
    draw();
  });
  $('#btnStepBack').addEventListener('click', () => {
    setPlaying(false);
    state.anim.phi = (state.anim.phi - TAU / 360 + TAU) % TAU;
    syncPhiUI();
    draw();
  });
  $('#btnReset').addEventListener('click', () => {
    setPlaying(false);
    state.anim.phi = 0;
    syncPhiUI();
    draw();
  });
  $('#speed').addEventListener('input', (ev) => {
    state.anim.speed = Number(ev.target.value);
    $('#speedVal').textContent = `${state.anim.speed.toFixed(2)}×`;
  });
  $('#phi').addEventListener('input', (ev) => {
    setPlaying(false);
    state.anim.phi = (Number(ev.target.value) * Math.PI) / 180;
    $('#phiVal').textContent = `${Number(ev.target.value).toFixed(1)}°`;
    draw();
  });

  // --- 顶栏 ---
  $('#btnFit').addEventListener('click', fitView);
  $('#btnShare').addEventListener('click', async () => {
    const url = location.origin + location.pathname + `?p=${encodeParams(state.params)}`;
    const ok = await copyText(url);
    toast(ok ? '已复制分享链接' : '复制失败，请手动复制地址栏', !ok);
  });
  $('#btnDxf').addEventListener('click', exportDxf);
  $('#btnSvg').addEventListener('click', exportSvg);
  $('#btnPng').addEventListener('click', exportPng);
  $('#btnJson').addEventListener('click', exportJson);

  // --- 键盘 ---
  window.addEventListener('keydown', (ev) => {
    if (ev.target instanceof HTMLInputElement || ev.target instanceof HTMLSelectElement) return;
    switch (ev.key) {
      case ' ':
        ev.preventDefault();
        setPlaying(!state.anim.playing);
        break;
      case 'ArrowRight':
        ev.preventDefault();
        state.anim.phi = (state.anim.phi + TAU / 360) % TAU;
        syncPhiUI(); draw();
        break;
      case 'ArrowLeft':
        ev.preventDefault();
        state.anim.phi = (state.anim.phi - TAU / 360 + TAU) % TAU;
        syncPhiUI(); draw();
        break;
      case 'f': case 'F':
        fitView();
        break;
      case '0':
        state.anim.phi = 0; syncPhiUI(); draw();
        break;
      default:
        break;
    }
  });

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas.parentElement);
  window.addEventListener('resize', resize);
}

/* ---------------------------------------------------------------------------
 * 初始化
 * ------------------------------------------------------------------------ */

async function init() {
  canvas = $('#view');
  ctx = canvas.getContext('2d', { alpha: false });

  const fromUrl = loadFromUrl();
  buildParamsUI();
  buildToggles();
  await loadPresets();
  bindEvents();
  resize();

  // 导出对象选择器（动态插入到顶栏导出按钮之前）
  const sel = document.createElement('select');
  sel.id = 'exportPart';
  sel.title = '选择导出内容';
  sel.innerHTML = `
    <option value="cycloid_disc">摆线轮</option>
    <option value="pin_ring">针齿壳</option>
    <option value="output_disc">输出盘</option>
    <option value="assembly">装配图</option>
    <option value="all_separate">全部（分件）</option>`;
  sel.addEventListener('change', () => (state.exportPart = sel.value));
  $('.export-group').prepend(sel);

  $('#speedVal').textContent = `${state.anim.speed.toFixed(2)}×`;
  syncPhiUI();
  state.view.fitted = false;
  if (fromUrl) {
    syncControls();
    toast('已从链接载入参数');
  }
  await analyze();
  fitView();
  requestAnimationFrame(loop);
}

document.addEventListener('DOMContentLoaded', init);
