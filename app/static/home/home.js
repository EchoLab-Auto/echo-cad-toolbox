/* =============================================================================
 * 工具箱首页脚本
 *
 * 只做三件事：
 *   1. 用真实几何公式画两张卡片上的矢量插画（摆线齿廓 / gyroid 等值线）；
 *   2. 探测服务健康状态与版本号；
 *   3. 「1 / 2」键盘快捷进入，以及把旧书签（/?zc=..&e=..）转发到 /cycloid。
 * ========================================================================== */

'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';
const TAU = Math.PI * 2;

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/* ---------------------------------------------------------------------------
 * 插画 1：摆线轮盘（解析短幅外摆线 + 内等距偏移）
 * ------------------------------------------------------------------------ */

function cycloidArt() {
  const W = 260;
  const H = 132;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet' });

  const defs = svgEl('defs');
  const grad = svgEl('linearGradient', { id: 'cycFill', x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.appendChild(svgEl('stop', { offset: '0', 'stop-color': '#3fb950', 'stop-opacity': '.30' }));
  grad.appendChild(svgEl('stop', { offset: '1', 'stop-color': '#3fb950', 'stop-opacity': '.07' }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  const cx = 76;
  const cy = H / 2;
  const Zc = 13;
  const Zp = Zc + 1;
  const Rz = 50;
  const K1 = 0.66;
  const e = (K1 * Rz) / Zp;
  const rp = 5.2;
  const scale = 1.02;

  const toPx = (x, y) => [cx + x * scale, cy + y * scale];

  // 针齿外圈（虚线导圆）
  svg.appendChild(
    svgEl('circle', {
      cx, cy, r: (Rz + rp) * scale, fill: 'none', stroke: '#58a6ff',
      'stroke-width': 1, 'stroke-dasharray': '3 4', opacity: '.35',
    })
  );

  // 针齿
  for (let i = 0; i < Zp; i += 1) {
    const a = (TAU * i) / Zp;
    const [px, py] = toPx(Rz * Math.cos(a), Rz * Math.sin(a));
    svg.appendChild(
      svgEl('circle', { cx: px, cy: py, r: rp * scale, fill: '#0f1720', stroke: '#58a6ff', 'stroke-width': 1.1, opacity: '.85' })
    );
  }

  // 齿廓
  const N = Zc * 26;
  const pts = [];
  for (let i = 0; i < N; i += 1) {
    const tau = (TAU * i) / N;
    const Px = Rz * Math.cos(tau) - e * Math.cos(Zp * tau);
    const Py = Rz * Math.sin(tau) - e * Math.sin(Zp * tau);
    const S = Math.hypot(
      Rz * Math.cos(tau) - e * Zp * Math.cos(Zp * tau),
      Rz * Math.sin(tau) - e * Zp * Math.sin(Zp * tau)
    );
    const Nx = (Rz * Math.cos(tau) - e * Zp * Math.cos(Zp * tau)) / S;
    const Ny = (Rz * Math.sin(tau) - e * Zp * Math.sin(Zp * tau)) / S;
    pts.push(toPx(Px - rp * Nx, Py - rp * Ny));
  }
  const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join('') + 'Z';
  svg.appendChild(svgEl('path', { d, fill: 'url(#cycFill)', stroke: '#7ee787', 'stroke-width': 1.5 }));

  // 柱销孔
  const Zw = 8;
  const Rw = Rz * 0.52;
  for (let i = 0; i < Zw; i += 1) {
    const a = (TAU * i) / Zw + 0.2;
    const [px, py] = toPx(Rw * Math.cos(a), Rw * Math.sin(a));
    svg.appendChild(svgEl('circle', { cx: px, cy: py, r: 3.4 * scale, fill: '#0a1017', stroke: '#d9a441', 'stroke-width': 1.1, opacity: '.9' }));
  }
  // 中心孔
  svg.appendChild(svgEl('circle', { cx, cy, r: 8 * scale, fill: '#0a1017', stroke: '#7ee787', 'stroke-width': 1.2, opacity: '.75' }));
  svg.appendChild(svgEl('circle', { cx, cy, r: 1.8, fill: '#7ee787', opacity: '.9' }));

  // 右侧数值注记
  const notes = [
    ['Zc', String(Zc)],
    ['Zp', String(Zp)],
    ['K1', K1.toFixed(2)],
    ['i', `${Zc}:1`],
  ];
  notes.forEach(([k, v], i) => {
    const x = 168;
    const y = 30 + i * 21;
    const t1 = svgEl('text', { x, y, fill: '#7c8899', 'font-size': '9', 'font-family': 'monospace', 'letter-spacing': '.6' });
    t1.textContent = k;
    const t2 = svgEl('text', { x: x + 28, y, fill: '#eef4fa', 'font-size': '11', 'font-family': 'monospace' });
    t2.textContent = v;
    svg.appendChild(t1);
    svg.appendChild(t2);
  });
  const rule = svgEl('line', { x1: 168, y1: 22, x2: 236, y2: 22, stroke: '#24313e', 'stroke-width': 1 });
  svg.appendChild(rule);

  return svg;
}

/* ---------------------------------------------------------------------------
 * 插画 2：gyroid 等值线（真实的 s = sinX·cosY + sinY·cosZ + sinZ·cosX = 0）
 * ------------------------------------------------------------------------ */

function gyroidArt() {
  const W = 260;
  const H = 132;
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet' });
  const defs = svgEl('defs');
  const grad = svgEl('radialGradient', { id: 'gyrGlow', cx: '50%', cy: '50%', r: '60%' });
  grad.appendChild(svgEl('stop', { offset: '0', 'stop-color': '#38d6b0', 'stop-opacity': '.16' }));
  grad.appendChild(svgEl('stop', { offset: '1', 'stop-color': '#38d6b0', 'stop-opacity': '0' }));
  defs.appendChild(grad);
  svg.appendChild(defs);
  svg.appendChild(svgEl('rect', { x: 0, y: 0, width: W, height: H, fill: 'url(#gyrGlow)' }));

  const s = (x, y, z) => Math.sin(x) * Math.cos(y) + Math.sin(y) * Math.cos(z) + Math.sin(z) * Math.cos(x);

  const K = TAU / 21;          // 单胞约 21px
  const step = 2.1;
  const slices = [
    { z: 0.0, color: '#38d6b0', op: 0.95, w: 1.25 },
    { z: 0.9, color: '#38d6b0', op: 0.45, w: 1.0 },
    { z: 1.8, color: '#8ff0d8', op: 0.22, w: 0.9 },
  ];
  const ox = 12;
  const oy = 8;

  for (const sl of slices) {
    const cols = Math.floor((W - 2 * ox) / step);
    const rows = Math.floor((H - 2 * oy) / step);
    const grid = [];
    for (let j = 0; j <= rows; j += 1) {
      const row = [];
      for (let i = 0; i <= cols; i += 1) {
        row.push(s(i * step * K, j * step * K, sl.z));
      }
      grid.push(row);
    }
    let d = '';
    const interp = (a, b, va, vb) => a + ((0 - va) / (vb - va)) * (b - a);
    for (let j = 0; j < rows; j += 1) {
      for (let i = 0; i < cols; i += 1) {
        const v00 = grid[j][i];
        const v10 = grid[j][i + 1];
        const v11 = grid[j + 1][i + 1];
        const v01 = grid[j + 1][i];
        const x0 = ox + i * step;
        const x1 = x0 + step;
        const y0 = oy + j * step;
        const y1 = y0 + step;
        const edges = [];
        if ((v00 < 0) !== (v10 < 0)) edges.push([interp(x0, x1, v00, v10), y0]);
        if ((v10 < 0) !== (v11 < 0)) edges.push([x1, interp(y0, y1, v10, v11)]);
        if ((v11 < 0) !== (v01 < 0)) edges.push([interp(x1, x0, v11, v01), y1]);
        if ((v01 < 0) !== (v00 < 0)) edges.push([x0, interp(y1, y0, v01, v00)]);
        if (edges.length === 2) {
          d += `M${edges[0][0].toFixed(1)} ${edges[0][1].toFixed(1)}L${edges[1][0].toFixed(1)} ${edges[1][1].toFixed(1)}`;
        } else if (edges.length === 4) {
          // 鞍点：两条短线段连起来（视觉上够用）
          d += `M${edges[0][0].toFixed(1)} ${edges[0][1].toFixed(1)}L${edges[1][0].toFixed(1)} ${edges[1][1].toFixed(1)}`;
          d += `M${edges[2][0].toFixed(1)} ${edges[2][1].toFixed(1)}L${edges[3][0].toFixed(1)} ${edges[3][1].toFixed(1)}`;
        }
      }
    }
    svg.appendChild(
      svgEl('path', {
        d, fill: 'none', stroke: sl.color, 'stroke-width': sl.w,
        'stroke-linecap': 'round', opacity: sl.op,
      })
    );
  }

  // 右上角小注记
  const lbl = svgEl('text', {
    x: W - 12, y: 18, 'text-anchor': 'end', fill: '#8ff0d8', opacity: '.65',
    'font-size': '9', 'font-family': 'monospace', 'letter-spacing': '.8',
  });
  lbl.textContent = 'gyroid';
  svg.appendChild(lbl);

  return svg;
}

/* ---------------------------------------------------------------------------
 * 装配
 * ------------------------------------------------------------------------ */

function mountArt() {
  const c = document.getElementById('artCycloid');
  if (c) c.appendChild(cycloidArt());
  const l = document.getElementById('artLattice');
  if (l) l.appendChild(gyroidArt());
}

async function probeHealth() {
  const pill = document.getElementById('statusPill');
  const text = document.getElementById('statusText');
  const versionEl = document.getElementById('versionText');
  try {
    const res = await fetch('/api/health', { cache: 'no-store' });
    const json = await res.json();
    pill.classList.add('online');
    text.textContent = '服务在线';
    if (versionEl) versionEl.textContent = `v${json.version || '—'}`;
  } catch {
    pill.classList.add('offline');
    text.textContent = '服务不可达';
  }
}

/** 旧书签兼容：/?tooth_count=39&… 直接转到 /cycloid 并保留参数。 */
function migrateLegacyLink() {
  const q = location.search.replace(/^\?/, '');
  if (!q) return false;
  const hash = location.hash || '';
  location.replace(`/cycloid?${q}${hash}`);
  return true;
}

function bindKeys() {
  window.addEventListener('keydown', (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === '1') location.href = '/cycloid';
    if (ev.key === '2') location.href = '/lattice';
  });
}

mountArt();
bindKeys();
if (!migrateLegacyLink()) probeHealth();
