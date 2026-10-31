/* ---------------------------------------------------------------------------
 * 晶格化工作台 —— 前端主逻辑
 *
 * 两条算路，用户可切换（默认走浏览器 GPU）：
 *
 *   GPU（WebGPU）  **体素化也在浏览器里做**（Web Worker + 精确 EDT），服务端
 *                  只把三角网格发过来（STEP 这类必须用 OCCT 解格式的才需要它）。
 *                  之后晶格场 × 实体场 → Surface Nets + Newton 投影 → 索引网格
 *                  全在客户机上。分辨率只受本机内存与显存限制，不再被服务端
 *                  容器内存钳住 —— 薄壁零件因此能真正画出来而不是碎成尖刺。
 *   服务器         传统路径：POST /generate → 轮询 → 拉 GLB 预览。
 *                  老浏览器 / 没有 WebGPU 时的兜底。
 *
 * 导出：GPU 生成的网格直接在浏览器里写 STL / 3MF / OBJ / PLY，不经过服务器。
 * ------------------------------------------------------------------------ */

import * as THREE from 'three';
import { OrbitControls } from '/static/vendor/three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from '/static/vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from '/static/vendor/three/examples/jsm/environments/RoomEnvironment.js';
import { GpuLatticeEngine } from './gpu/engine.js';
import { exportMesh } from './gpu/exporters.js';
import { parseMeshBuffer } from './client/loaders.js';

const API = '/api/lattice';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  job: null,
  view: 'original',
  wireframe: false,
  autoRotate: false,
  busy: false,
  pollTimer: null,
  latticeVersion: -1,
  /** 'gpu' | 'server' */
  engineMode: 'server',
  engine: null,           // GpuLatticeEngine 实例
  engineProbe: null,      // { supported, label }
  volume: null,           // { meta, jobId, resolution, features }
  /** 本地三角网格（浏览器端体素化的输入）：{ positions, indices, format, jobId } */
  localMesh: null,
  mesh: null,             // { positions, normals, indices, stats }
  features: null,
  lastMs: 0,
};

/* =========================================================================
 * 三维视图
 * ====================================================================== */
const canvas = $('#canvas');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x06090d);

const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.02).texture;

const camera = new THREE.PerspectiveCamera(42, 1, 0.5, 20000);
camera.position.set(150, 120, 190);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.075;
controls.rotateSpeed = 0.85;
controls.minDistance = 1;
controls.maxDistance = 8000;

scene.add(new THREE.HemisphereLight(0xbfd9ff, 0x1b232c, 0.55));
const keyLight = new THREE.DirectionalLight(0xffffff, 1.7);
keyLight.position.set(1, 1.35, 0.85);
scene.add(keyLight);
const rimLight = new THREE.DirectionalLight(0x69c8ff, 0.8);
rimLight.position.set(-1, 0.25, -1);
scene.add(rimLight);

const grid = new THREE.GridHelper(400, 40, 0x1b3b45, 0x121b24);
grid.material.transparent = true;
grid.material.opacity = 0.5;
scene.add(grid);

const groupOriginal = new THREE.Group();
const groupLattice = new THREE.Group();
scene.add(groupOriginal, groupLattice);

const matOriginal = new THREE.MeshStandardMaterial({
  color: 0x9db0c4, metalness: 0.25, roughness: 0.5,
  transparent: true, opacity: 0.9, side: THREE.DoubleSide, envMapIntensity: 0.7,
});
const matLattice = new THREE.MeshStandardMaterial({
  color: 0x56d4b8, metalness: 0.6, roughness: 0.28, envMapIntensity: 1.15,
  side: THREE.DoubleSide,
});

const loader = new GLTFLoader();

function resize() {
  const parent = canvas.parentElement;
  const w = parent.clientWidth || 1;
  const h = parent.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas.parentElement);

// 调试 / 自动化测试钩子（不参与业务逻辑）
window.__lattice = {
  state, scene, camera, renderer, controls,
  groups: { original: groupOriginal, lattice: groupLattice },
  frameAll, generate, setEngine,
};

function animate() {
  requestAnimationFrame(animate);
  controls.autoRotate = state.autoRotate;
  controls.autoRotateSpeed = 0.9;
  controls.update();
  renderer.render(scene, camera);
}

function clearGroup(group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.traverse?.((o) => { if (o.isMesh) o.geometry?.dispose?.(); });
  }
}

function applyMaterial(root, material) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.material = material;
    o.frustumCulled = false;
    const geom = o.geometry;
    if (geom && !geom.attributes.normal && geom.attributes.position) {
      geom.computeVertexNormals();
    }
  });
}

function sceneBounds() {
  const box = new THREE.Box3();
  let has = false;
  for (const group of [groupLattice, groupOriginal]) {
    if (group.children.length === 0 || !group.visible) continue;
    box.expandByObject(group);
    has = true;
  }
  if (!has) {
    for (const group of [groupLattice, groupOriginal]) {
      if (group.children.length === 0) continue;
      box.expandByObject(group);
      has = true;
    }
  }
  return has ? box : null;
}

function frameAll(resetAngle = true) {
  const box = sceneBounds();
  if (!box) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const dist = (maxDim / 2) / Math.tan((camera.fov * Math.PI) / 360) * 1.9;

  if (resetAngle) {
    camera.position.set(center.x + dist * 0.62, center.y + dist * 0.55, center.z + dist * 0.78);
  }
  camera.near = Math.max(maxDim / 800, 0.05);
  camera.far = maxDim * 40 + 1000;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();

  const gridSize = Math.ceil((maxDim * 1.8) / 10) * 10;
  grid.scale.setScalar(gridSize / 400);
  grid.position.set(center.x, box.min.y - maxDim * 0.02, center.z);
}

/* =========================================================================
 * 通用工具
 * ====================================================================== */
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data?.detail || `请求失败 (${res.status})`);
  return data;
}

let toastTimer = null;
function toast(message, ok = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('err', !ok);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ok ? 2400 : 6000);
}

const setStatus = (text, right = '') => {
  $('#statusText').textContent = text;
  $('#statusRight').textContent = right;
};

function showProgress(pct, stage) {
  $('#progressOverlay').hidden = false;
  $('#progressFill').style.width = `${Math.max(0, Math.min(1, pct)) * 100}%`;
  $('#progressPct').textContent = `${Math.round(pct * 100)}%`;
  $('#progressStage').textContent = stage || '处理中…';
}
const hideProgress = () => { $('#progressOverlay').hidden = true; };

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

/* =========================================================================
 * 计算引擎探测
 * ====================================================================== */
async function probeEngine() {
  const chip = $('#engineChip');
  const label = $('#engineLabel');
  const hint = $('#engineHint');
  const gpuBtn = $('#engineSeg button[data-engine="gpu"]');

  try {
    state.engine = await GpuLatticeEngine.create();
  } catch (err) {
    console.warn('[lattice] WebGPU 初始化失败：', err);
    state.engine = null;
  }

  if (!state.engine) {
    state.engineMode = 'server';
    gpuBtn.disabled = true;
    chip.classList.add('server');
    label.textContent = '服务器计算';
    hint.textContent = '当前浏览器不支持 WebGPU（Chrome/Edge 113+、Safari 26+），改用服务端生成。';
    setEngine('server');
    return;
  }

  state.engineMode = 'gpu';
  gpuBtn.disabled = false;
  chip.classList.add('gpu');
  const maxRes = estimateMaxResolution(state.engine);
  label.textContent = `GPU · ${state.engine.adapterLabel}`;
  hint.textContent = `浏览器 GPU 直接生成（${state.engine.adapterLabel}），本机可跑 ${maxRes} 分辨率；只向服务器要一次性体素场。`;
  setEngine('gpu');
}

/** 按适配器缓冲区上限估算可用分辨率（立方体网格） */
function estimateMaxResolution(engine) {
  const voxels = engine.maxVoxels;
  return Math.max(48, Math.min(400, Math.floor(Math.cbrt(voxels / 1.6))));
}

function setEngine(mode) {
  state.engineMode = mode;
  $$('#engineSeg button').forEach((b) => b.classList.toggle('active', b.dataset.engine === mode));
  // 平滑 / 抽稀只在服务端生成时存在（GPU 路径不抽稀，保证小孔不被抹掉）
  $$('.server-only').forEach((el) => el.classList.toggle('server-only-off', mode !== 'server'));
  const badge = $('#engineChip');
  if (badge) {
    badge.classList.toggle('gpu', mode === 'gpu');
    badge.classList.toggle('server', mode === 'server');
  }
  if (state.config) applyResourceLimits(state.config);
}

/* =========================================================================
 * 配置 → 表单
 * ====================================================================== */
const TPMS_DENSITY = { gyroid: 2.9, schwarz_p: 2.3, diamond: 3.45, iwp: 3.55 };
const STRUT_LENGTH = { sc: 3.0, grid: 3.0, bcc: 6.93, fcc: 8.49, octet: 15.42 };
const STRUT_OVERLAP = { sc: 0.96, grid: 0.96, bcc: 0.87, fcc: 0.88, octet: 0.9 };

function populateConfig(cfg) {
  const latticeSel = $('#latticeSel');
  latticeSel.innerHTML = '';
  for (const [key, info] of Object.entries(cfg.lattices)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = info.label;
    latticeSel.appendChild(opt);
  }
  latticeSel.value = cfg.defaults.lattice || 'gyroid';

  const modeSel = $('#modeSel');
  modeSel.innerHTML = '';
  for (const [key, label] of Object.entries(cfg.modes)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = label;
    modeSel.appendChild(opt);
  }
  modeSel.value = cfg.defaults.mode || 'intersect';

  $('#cellRange').value = cfg.defaults.cell_size;
  $('#thickRange').value = cfg.defaults.thickness;
  $('#shellRange').value = cfg.defaults.shell_thickness;
  applyResourceLimits(cfg);
  $('#warpRange').value = cfg.defaults.warp;
  $('#gradRange').value = cfg.defaults.grad;
  $('#smoothRange').value = cfg.defaults.smooth;
  $('#decRange').value = cfg.defaults.decimate;
  $('#wallSel').value = cfg.defaults.wall_mode || 'sheet';
  $('#skinChk').checked = !!cfg.defaults.keep_skin;
  $('#thinWallChk').checked = cfg.defaults.thin_wall !== 'off';

  syncLatticeLabels();
  syncModeHint();
  syncDensity();
  syncOutputs();
}

/** 服务端内存上限决定的分辨率上限（GPU 模式下不受限） */
function applyResourceLimits(cfg) {
  const resSel = $('#resSel');
  const serverLimit = cfg?.resources?.max_resolution ?? 320;
  state.serverMaxRes = serverLimit;

  // GPU 模式下体素化在客户机上做，服务端内存上限**不再约束**分辨率 ——
  // 只受本机显存（estimateMaxResolution）与参数上限 400 约束。
  const gpuLimit = state.engine ? estimateMaxResolution(state.engine) : serverLimit;
  const limit = state.engineMode === 'gpu' ? Math.max(48, Math.min(400, gpuLimit)) : serverLimit;

  const allowed = [];
  for (const opt of resSel.options) {
    const v = Number(opt.value);
    opt.disabled = v > limit;
    if (v <= limit) allowed.push(v);
  }
  if (+resSel.value > limit) resSel.value = String(allowed[allowed.length - 1] || 96);

  const hint = $('#resHint');
  hint.hidden = false;
  hint.textContent = state.engineMode === 'gpu'
    ? `GPU 模式：体素化与晶格都在本机算，分辨率只受你的显卡限制，可跑 ${limit}。`
    : `服务端检测到内存上限 ${cfg?.resources?.memory_limit_mb ?? '?'} MB，最高 ${serverLimit} 分辨率；切到浏览器 GPU 用你自己的机器，可以更高。`;
}

function syncLatticeLabels() {
  const key = $('#latticeSel').value;
  const info = state.config?.lattices?.[key];
  $('#latticeDesc').textContent = info?.desc || '';
  const isTpms = info?.family === 'tpms';
  const thickLabel = $('#thickLabel');
  if (thickLabel) thickLabel.textContent = isTpms ? '壁厚' : '杆径';
  $('#wallSel').disabled = !isTpms;
}

function syncModeHint() {
  const mode = $('#modeSel').value;
  $('#shellField').hidden = mode !== 'shell';
  const hints = {
    intersect: '只在实体内部生成晶格，外形与原模型一致。',
    shell: '仅保留表层壳内的一层晶格，内部空心，减重最多。',
    subtract: '从实体里挖出晶格空腔（做减重腔，保留外表面）。',
    union: '晶格向实体外延伸，得到外扩的复合结构。',
  };
  $('#modeDesc').textContent = hints[mode] || '';
  syncDensity();
}

function collectParams() {
  const cell = +$('#cellRange').value;
  const thickness = Math.min(+$('#thickRange').value, 0.8 * cell);
  return {
    lattice: $('#latticeSel').value,
    cell_size: cell,
    thickness,
    wall_mode: $('#wallSel').value,
    mode: $('#modeSel').value,
    shell_thickness: +$('#shellRange').value,
    keep_skin: $('#skinChk').checked,
    thin_wall: $('#thinWallChk').checked ? 'auto' : 'off',
    skin_thickness: 1.2,
    resolution: +$('#resSel').value,
    warp: +$('#warpRange').value,
    grad: +$('#gradRange').value,
    grad_height: state.job?.info?.extents?.[2] || null,
    smooth: +$('#smoothRange').value,
    decimate: 0,
    align: 'min',
  };
}

function collectCycloid() {
  return {
    params: {
      tooth_count: +$('#czc').value,
      eccentricity: +$('#ce').value,
      pin_circle_radius: +$('#crz').value,
      pin_radius: +$('#crp').value,
    },
    width: +$('#cwidth').value,
  };
}

function clampThickness() {
  const cell = +$('#cellRange').value;
  const thickEl = $('#thickRange');
  const limit = Math.max(0.2, Math.min(+thickEl.max, 0.8 * cell));
  if (+thickEl.value > limit) thickEl.value = limit.toFixed(2);
}

function syncDensity() {
  const key = $('#latticeSel').value;
  const cell = +$('#cellRange').value || 8;
  const t = Math.min(+$('#thickRange').value, 0.8 * cell);
  const mode = $('#modeSel').value;
  let rho;
  if (TPMS_DENSITY[key]) {
    rho = Math.min(0.95, Math.max(0.005, TPMS_DENSITY[key] * (t / cell)));
  } else if (STRUT_LENGTH[key]) {
    const r = t / 2;
    rho = Math.min(0.97, Math.max(0.002, (STRUT_LENGTH[key] * Math.PI * r * r * STRUT_OVERLAP[key]) / (cell * cell)));
  } else {
    rho = 0.2;
  }
  $('#densityPct').textContent = `${(rho * 100).toFixed(1)} %`;
  $('#densityFill').style.width = `${Math.min(100, rho * 100).toFixed(1)}%`;
  $('#densityPct').style.color = rho > 0.6 ? 'var(--warn)' : '';
  if (mode === 'subtract') {
    $('#densityPct').textContent = `${((1 - rho) * 100).toFixed(1)} %（挖空 ${(rho * 100).toFixed(0)}%）`;
  }
}

function syncOutputs() {
  $('#cellOut').textContent = `${(+$('#cellRange').value).toFixed(1)} mm`;
  $('#thickOut').textContent = `${(+$('#thickRange').value).toFixed(2)} mm`;
  $('#shellOut').textContent = `${(+$('#shellRange').value).toFixed(1)} mm`;
  $('#warpOut').textContent = (+$('#warpRange').value).toFixed(2);
  $('#gradOut').textContent = (+$('#gradRange').value).toFixed(2);
  $('#smoothOut').textContent = $('#smoothRange').value;
  $('#decOut').textContent = `${Math.round(+$('#decRange').value * 100)}%`;
  $('#resOut').textContent = $('#resSel').value;
  syncCycloidOutputs();
}

function syncCycloidOutputs() {
  const zc = +$('#czc').value;
  const e = +$('#ce').value;
  const rz = +$('#crz').value;
  const rp = +$('#crp').value;
  $('#czcOut').textContent = String(zc);
  $('#ceOut').textContent = `${e.toFixed(2)} mm`;
  $('#crzOut').textContent = `${rz.toFixed(1)} mm`;
  $('#crpOut').textContent = `${rp.toFixed(2)} mm`;
  $('#cwidthOut').textContent = `${(+$('#cwidth').value).toFixed(1)} mm`;

  const k1 = (e * (zc + 1)) / rz;
  const tip = rz - e * (zc + 1);
  const ok = k1 > 0.2 && k1 < 0.95 && tip > 0;
  $('#cycloidKpi').innerHTML = `
    <div><span>针齿数 Zp</span><b>${zc + 1}</b></div>
    <div><span>传动比 i</span><b>${zc}:1</b></div>
    <div><span>短幅系数 K1</span><b style="color:${ok ? 'var(--lattice)' : 'var(--warn)'}">${k1.toFixed(3)}</b></div>
    <div><span>齿顶圆 ⌀</span><b>${(2 * (rz - rp - e)).toFixed(0)} mm</b></div>
  `;
}

/* =========================================================================
 * 视图切换
 * ====================================================================== */
function setView(view) {
  state.view = view;
  $$('#viewSeg button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  const hasLattice = groupLattice.children.length > 0;
  const effective = view === 'lattice' && !hasLattice ? 'original' : view;

  groupOriginal.visible = effective !== 'lattice';
  groupLattice.visible = effective !== 'original' && hasLattice;
  matOriginal.opacity = effective === 'overlay' ? 0.14 : 0.9;
  matOriginal.depthWrite = effective !== 'overlay';
}

/* =========================================================================
 * 作业状态
 * ====================================================================== */
function setBusy(busy) {
  state.busy = busy;
  const btn = $('#btnGenerate');
  btn.disabled = busy || !state.job;
  btn.querySelector('.spinner').hidden = !busy;
  btn.querySelector('.btn-label').textContent = busy ? '生成中…' : '生成晶格';
}

function setExportEnabled(on) {
  for (const id of ['#btnStl', '#btn3mf', '#btnObj', '#btnPly']) $(id).disabled = !on;
}

function renderModelInfo(job) {
  const info = job.info || {};
  const rows = [
    ['文件', job.filename || '-'],
    ['格式', String(info.format || '').toUpperCase() || '-'],
    ['面数', Number(info.faces || 0).toLocaleString()],
    ['尺寸', (info.extents || []).map((v) => v.toFixed(1)).join(' × ') + ' mm'],
    ['体积', info.volume ? `${(info.volume / 1000).toFixed(2)} cm³` : '—'],
    ['闭合', info.watertight ? '是' : '否'],
  ];
  if (info.tessellated_from_cad) rows.push(['来源', 'CAD 曲面离散']);
  $('#modelKv').innerHTML = rows
    .map(([k, v]) => `<dt>${k}</dt><dd title="${String(v)}">${v}</dd>`)
    .join('');

  const warn = $('#modelWarn');
  if (info.watertight === false) {
    warn.hidden = false;
    warn.textContent = '模型不是闭合网格，体素化结果可能不准确；建议先在 CAD 中修补为实体。';
  } else {
    warn.hidden = true;
  }

  const cy = info.cycloid;
  const box = $('#cycloidInfo');
  if (cy) {
    box.hidden = false;
    box.innerHTML = [
      ['齿数', `${cy.tooth_count} / ${cy.pin_count}`],
      ['盘厚', `${cy.width.toFixed(1)} mm`],
      ['柱销孔', `${cy.hole_count} × ⌀${(cy.hole_radius * 2).toFixed(1)}`],
      ['中心孔', cy.center_bore_radius > 0 ? `⌀${(cy.center_bore_radius * 2).toFixed(1)} mm` : '无'],
      ['钢件质量', `${cy.mass_kg_steel.toFixed(2)} kg`],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  } else {
    box.hidden = true;
  }
  $('#modelInfo').hidden = false;
}

function renderStats(stats, extra = {}) {
  if (!stats) return;
  const origVol = state.job?.info?.volume || stats.original_volume_mm3 || 0;
  const rows = [
    ['面数', Number(stats.faces).toLocaleString()],
    ['顶点', Number(stats.vertices || 0).toLocaleString()],
    ['体积', `${(stats.volume_mm3 / 1000).toFixed(2)} cm³`],
    ['表面积', `${(stats.area_mm2 / 100).toFixed(1)} cm²`],
  ];
  if (origVol) rows.push(['相对密度', `${((stats.volume_mm3 / origVol) * 100).toFixed(1)} %`]);
  rows.push(['体素', `${Number(stats.voxel_pitch).toFixed(3)} mm`]);
  rows.push(['引擎', extra.engine || '服务器']);
  rows.push(['耗时', extra.seconds !== undefined ? `${extra.seconds} s` : `${(state.lastMs / 1000).toFixed(2)} s`]);
  $('#statsKv').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  $('#statsPanel').hidden = false;

  // 服务端（或下面 GPU 分支）算出来的"这个分辨率画不出这层壁"之类的诊断，
  // 必须落在用户看得见的地方 —— 否则尖刺只能靠肉眼发现，参数却看不出问题。
  const warnBox = $('#statsWarn');
  const warnings = Array.isArray(stats.warnings) ? stats.warnings : [];
  if (warnings.length) {
    warnBox.hidden = false;
    warnBox.innerHTML = warnings.map((w) => `<div>${w}</div>`).join('');
  } else {
    warnBox.hidden = true;
    warnBox.innerHTML = '';
  }
}

/** 与 app/lattice/pipeline.py 的 MIN_RENDER_WALL_VOXELS 保持一致 */
const MIN_RENDER_WALL_VOXELS = 1.5;

/**
 * 薄壁兜底（前端版）：GPU 生成不经过服务端 pipeline，规则必须在这里再实现一遍。
 * 体素比壁还粗时等值面根本连不起来，硬画就是一张刺球 —— 和服务器一样，先把
 * 壁厚抬到这套体素画得出来的最小值，并把这件事写进警告里。
 */
function thinWallFix(params, pitch) {
  const out = Object.assign({}, params);
  out.notes = [];
  if (!(pitch > 0) || params.thin_wall === 'off') return out;
  const wall = Math.min(+params.thickness, 0.8 * +params.cell_size);
  const floor = MIN_RENDER_WALL_VOXELS * pitch;
  if (wall >= floor * 0.98) return out;
  out.thickness = Math.round(floor * 1000) / 1000;
  out.notes.push(
    `ℹ 已按壁厚 ${floor.toFixed(2)} mm 渲染（你填的是 ${wall.toFixed(2)} mm）：体素 ${pitch.toFixed(3)} mm，` +
    `${wall.toFixed(2)} mm 只占 ${(wall / pitch).toFixed(2)} 个体素，等值面画不出连续的壁，硬画只会得到满身尖刺。` +
    `${floor.toFixed(2)} mm（${MIN_RENDER_WALL_VOXELS} 个体素）是这套体素下的最薄可画壁厚。`
  );
  return out;
}

/**
 * 客户端诊断：GPU 生成不经过服务端 pipeline，这里按同样的阈值算一遍。
 * 规则与 app/lattice/pipeline.py 的 WALL_VOXELS_GOOD / CELL_VOXELS_MIN 一致。
 */
function latticeWarnings(params, pitch) {
  const out = [];
  if (!(pitch > 0)) return out;
  const wall = Math.min(+params.thickness, 0.8 * +params.cell_size);
  const wallVox = wall / pitch;
  const cell = Math.min(+params.cell_size, +(params.cell_size_z || params.cell_size));
  // 阈值与后端一致：低于 MIN_RENDER_WALL_VOXELS 才画不出连续的壁；已经兜底加厚过
  // （params.notes 非空）就不用再念一遍，否则两句话会互相打架。
  const fixed = Array.isArray(params.notes) && params.notes.length > 0;
  if (wallVox < MIN_RENDER_WALL_VOXELS && !fixed) {
    const extent = Math.max(...(state.job?.info?.extents || [0]));
    const need = extent > 0 ? Math.ceil((extent * MIN_RENDER_WALL_VOXELS) / Math.max(wall, 1e-9)) : 0;
    out.push(
      `⚠ 壁厚 ${wall.toFixed(2)} mm 只有 ${wallVox.toFixed(2)} 个体素（体素 ${pitch.toFixed(3)} mm），` +
      `薄壁会被网格漏掉，表面会碎成尖刺/毛边。把壁厚加到 ≥ ${(MIN_RENDER_WALL_VOXELS * pitch).toFixed(1)} mm` +
      (need ? `，或分辨率提到 ≥ ${need}（受本机内存上限约束）` : '') + '，或把模型缩小。'
    );
  }
  if (cell / pitch < 5) {
    out.push(`⚠ 单胞 ${cell.toFixed(1)} mm 只有 ${(cell / pitch).toFixed(1)} 个体素（建议 ≥ 5），晶格形状会被抹平。`);
  }
  return out;
}

/** 精度诊断：体素尺寸 vs 模型最小特征 / 晶格壁厚 */
function renderPrecision(meta) {
  const box = $('#precisionBox');
  const features = meta?.features || null;
  state.features = features;
  const pitch = meta?.pitch;
  if (!pitch) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  const holeR = features?.min_hole_radius ?? null;
  const holeRatio = holeR ? (holeR * 2) / pitch : null;      // 孔径 / 体素
  const thickness = Math.min(+$('#thickRange').value, 0.8 * +$('#cellRange').value);
  const wallRatio = thickness / pitch;                        // 壁厚 / 体素
  const extent = Math.max(...(state.job?.info?.extents || [1]));

  const rows = [];
  rows.push(`<div class="row"><span>体素尺寸</span><b>${pitch.toFixed(3)} mm</b></div>`);
  rows.push(`<div class="row"><span>网格</span><b>${(meta.shape || []).join(' × ')}</b></div>`);
  if (holeR) {
    rows.push(`<div class="row"><span>最小内孔半径</span><b>${holeR.toFixed(2)} mm</b></div>`);
    rows.push(
      `<div class="row"><span>孔径 / 体素</span><b class="${holeRatio >= 4 ? 'ok' : 'bad'}">${holeRatio.toFixed(1)}</b></div>`
    );
  }
  // 壁厚 ≥ MIN_RENDER_WALL_VOXELS 就画得出连续的壁（自动兜底也用它当门槛）；
  // 3 个体素只是"更漂亮"，所以颜色分三档，别把能画的情况标成红色。
  const wallTone = wallRatio >= 3 ? 'ok' : (wallRatio >= MIN_RENDER_WALL_VOXELS ? '' : 'bad');
  rows.push(
    `<div class="row"><span>壁厚 / 体素</span><b class="${wallTone}">${wallRatio.toFixed(1)}</b></div>`
  );

  // 两条硬要求：小孔至少 4 个体素宽，壁厚至少能连起来
  const warnings = [...(features?.warnings || [])];
  if (wallRatio < MIN_RENDER_WALL_VOXELS) {
    warnings.push(`壁厚 / 杆径 ${thickness.toFixed(2)} mm 只有 ${wallRatio.toFixed(1)} 个体素，画不出连续的壁（会碎成尖刺），建议分辨率 ≥ ${suggestResolution(extent, thickness / MIN_RENDER_WALL_VOXELS, pitch)}`);
  }
  if (holeRatio !== null && holeRatio < 4) {
    warnings.push(`最小孔 ⌀${(holeR * 2).toFixed(2)} mm 只有 ${holeRatio.toFixed(1)} 个体素宽，等值面会明显失真`);
  }
  if (warnings.length) {
    rows.push(`<div class="warnline">${warnings.map((w) => `⚠ ${w}`).join('<br>')}</div>`);
  }
  box.innerHTML = rows.join('');

  // 自动分辨率：同时满足「小孔 ≥ 4 体素」「壁厚 ≥ MIN_RENDER_WALL_VOXELS 体素」
  const need = Math.max(
    holeR ? suggestResolution(extent, holeR / 2, pitch) : 0,
    suggestResolution(extent, thickness / MIN_RENDER_WALL_VOXELS, pitch)
  );
  const btn = $('#btnAutoRes');
  if (features?.recommended_resolution || need > 0) {
    const rec = Math.max(features?.recommended_resolution || 0, need);
    btn.hidden = false;
    btn.dataset.target = String(clampResolution(rec));
    btn.textContent = `按最小特征自动选分辨率（建议 ${btn.dataset.target}）`;
  } else {
    btn.hidden = true;
  }
}

/** 目标体素尺寸 → 分辨率（按模型最长边） */
function suggestResolution(extent, pitchNeeded, currentPitch) {
  if (!(pitchNeeded > 0)) return 0;
  const res = Math.ceil(extent / pitchNeeded);
  return currentPitch && res <= 1 ? 0 : res;
}

function clampResolution(res) {
  const maxGpu = state.engine ? estimateMaxResolution(state.engine) : 320;
  const cap = state.engineMode === 'gpu' ? maxGpu : (state.serverMaxRes || 320);
  return Math.max(48, Math.min(cap, Math.round(res / 8) * 8)); // 取 8 的整数倍，缓冲更整齐
}

function applyJob(job) {
  state.job = job;
  if (job.info) renderModelInfo(job);

  const running = job.status === 'preparing' || job.status === 'running';
  if (running) {
    setBusy(true);
    showProgress(job.progress || 0.05, job.stage || '处理中…');
  } else {
    hideProgress();
  }

  if (job.status === 'error') {
    setBusy(false);
    setStatus('出错', '');
    toast(job.error || '处理失败');
    return;
  }

  if (job.status === 'ready') {
    setBusy(false);
    setExportEnabled(!!state.mesh);
    const voxel = job.info?.voxel;
    setStatus(
      `模型就绪 · ${job.filename}`,
      voxel ? `体素 ${voxel.resolution} · ${voxel.pitch.toFixed(3)} mm` : ''
    );
    // 服务端模式不拉体素场，但作业快照里已经带了体素尺寸与小特征诊断，
    // 精度面板照样能显示（GPU 模式由 prepareVolume 里的 renderPrecision 覆盖）
    if (voxel && !state.volume) {
      renderPrecision({ ...voxel, bytes: 0, features: voxel.features || null });
    }
  }

  if (job.status === 'done') {
    setBusy(false);
    // 服务端模式：体素化是在生成时才发生的，所以精度诊断要在这里补一次
    const voxelDone = job.info?.voxel;
    if (voxelDone && !state.volume) {
      renderPrecision({ ...voxelDone, bytes: 0, features: voxelDone.features || null });
    }
    if (state.engineMode === 'server') {
      renderStats(job.stats, { engine: '服务器', seconds: job.stats?.seconds });
      setExportEnabled(true);
      setStatus(`晶格已生成 · ${job.filename}`, `${job.stats?.seconds ?? '?'} s`);
      if (job.version !== state.latticeVersion) {
        state.latticeVersion = job.version;
        loadPreview('lattice', job.version).then(() => {
          if (state.view === 'original') setView('lattice');
          else setView(state.view);
        });
      }
    }
  }
}

async function loadPreview(kind, version = 0) {
  if (!state.job) return;
  const url = `${API}/jobs/${state.job.id}/preview/${kind}?v=${version}-${Date.now()}`;
  try {
    const gltf = await loader.loadAsync(url);
    const group = kind === 'original' ? groupOriginal : groupLattice;
    clearGroup(group);
    // trimesh 导出的 GLB 保持 Z-up，转成 three.js 的 Y-up
    gltf.scene.rotation.x = -Math.PI / 2;
    group.add(gltf.scene);
    applyMaterial(gltf.scene, kind === 'original' ? matOriginal : matLattice);
    matLattice.wireframe = state.wireframe;
    if (kind === 'original' || groupLattice.children.length === 0) frameAll();
    else frameAll(false);
  } catch (err) {
    console.error(err);
    toast(`预览加载失败：${err.message || err}`);
  }
}

function pollJob(jobId) {
  clearTimeout(state.pollTimer);
  const tick = async () => {
    try {
      const job = await fetchJSON(`${API}/jobs/${jobId}`);
      applyJob(job);
      if (job.status === 'preparing' || job.status === 'running') {
        state.pollTimer = setTimeout(tick, 550);
      } else if (job.status === 'error') {
        setBusy(false);
      }
    } catch (err) {
      console.error(err);
      setBusy(false);
      hideProgress();
      toast(err.message || '状态查询失败');
    }
  };
  tick();
}

function resetResult() {
  clearGroup(groupOriginal);
  clearGroup(groupLattice);
  groupOriginal.visible = true;
  groupLattice.visible = false;
  state.latticeVersion = -1;
  state.mesh = null;
  state.volume = null;
  setView('original');
  $('#statsPanel').hidden = true;
  $('#modelInfo').hidden = true;
  $('#precisionBox').hidden = true;
  setExportEnabled(false);
}

async function adoptJob(job) {
  // 换作业 = 换模型，本地缓存的网格与体场一律作废（否则会拿旧模型算新参数）
  state.localMesh = null;
  state.volume = null;
  applyJob(job);
  await loadPreview('original');
  setView('original');
  pollJob(job.id);
  if (state.engineMode === 'gpu') await prepareVolume();
}

/* =========================================================================
 * 体素场（GPU 路径的一次性输入）
 * ====================================================================== */
/* =========================================================================
 * 体素场：默认在浏览器里算（客户机算力），服务端只提供三角网格
 * ====================================================================== */

/**
 * 取原始三角网格（缓存）。服务端只做格式转换（STEP/IGES 必须用它），
 * 拿到 STL 之后就再也不用麻烦它了。
 */
async function ensureLocalMesh() {
  if (state.localMesh && state.localMesh.jobId === state.job.id) return state.localMesh;
  setStatus('获取三角网格…', '');
  const res = await fetch(`${API}/jobs/${state.job.id}/mesh`);
  if (!res.ok) {
    let detail = `网格获取失败（${res.status}）`;
    try {
      const j = await res.json();
      if (j.detail) detail = j.detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  const buffer = await res.arrayBuffer();
  const parsed = parseMeshBuffer(buffer, '.stl');
  state.localMesh = { ...parsed, jobId: state.job.id };
  return state.localMesh;
}

/** 在 Web Worker 里体素化（不阻塞渲染），返回 {meta, buffer} */
function voxelizeInWorker(mesh, resolution, onProgress) {
  return new Promise((resolve, reject) => {
    let worker;
    try {
      worker = new Worker(new URL('./client/voxelize.worker.js', import.meta.url), { type: 'module' });
    } catch (err) {
      reject(new Error(`无法启动体素化线程：${err.message || err}`));
      return;
    }
    const id = Math.random().toString(36).slice(2);
    const cleanup = () => { try { worker.terminate(); } catch { /* ignore */ } };
    worker.onmessage = (ev) => {
      const msg = ev.data || {};
      if (msg.id !== id) return;
      if (msg.type === 'progress') {
        if (onProgress) onProgress(msg.fraction, msg.stage);
      } else if (msg.type === 'done') {
        cleanup();
        resolve(msg.result);
      } else {
        cleanup();
        reject(new Error(msg.message || '体素化失败'));
      }
    };
    worker.onerror = (ev) => {
      cleanup();
      reject(new Error(ev.message || '体素化线程异常'));
    };
    // 网格用结构化克隆复制：主线程还要留着它做"原始模型"预览与重新体素化
    worker.postMessage({ id, positions: mesh.positions, indices: mesh.indices, options: { resolution } });
  });
}

async function prepareVolume() {
  if (!state.job || !state.engine) return null;
  const resolution = +$('#resSel').value;
  if (state.volume && state.volume.jobId === state.job.id && state.volume.resolution === resolution) {
    return state.volume;
  }

  await nextFrame();
  try {
    showProgress(0.05, '读取三角网格…');
    const mesh = await ensureLocalMesh();

    showProgress(0.1, `本机体素化（${resolution}³）…`);
    setStatus('本机体素化…', `${mesh.indices.length / 3} 面`);
    const result = await voxelizeInWorker(mesh, resolution, (f, stage) => {
      showProgress(0.1 + 0.85 * f, `${stage}（${resolution}³）`);
    });

    const meta = {
      resolution: result.resolution,
      shape: result.shape,
      pitch: result.pitch,
      origin: result.origin,
      bounds: result.bounds,
      bytes: result.sdf.byteLength,
      local: true,
    };
    const grid = state.engine.setSolid(meta, result.sdf.buffer);
    state.volume = { meta, jobId: state.job.id, resolution, features: result.features || null, grid };
    renderPrecision(meta);
    hideProgress();
    const mb = (meta.bytes / 1048576).toFixed(1);
    setStatus(
      `模型就绪 · ${state.job.filename}`,
      `本机算 · 体素 ${grid.nx}×${grid.ny}×${grid.nz} · ${meta.pitch.toFixed(3)} mm · ${mb} MB`
    );
    return state.volume;
  } catch (err) {
    // 本地算不了（老浏览器 / 线程被禁用）就退回服务端，别让用户卡住
    console.warn('本机体素化失败，退回服务端：', err);
    hideProgress();
    return prepareVolumeRemote(resolution, err);
  }
}

/** 兜底：服务端体素化（老浏览器 / 本地失败时） */
async function prepareVolumeRemote(resolution, cause) {
  showProgress(0.1, `服务端体素化（${resolution}³）…`);
  setStatus('准备体素场…', '');
  await nextFrame();
  try {
    const res = await fetch(`${API}/jobs/${state.job.id}/volume?resolution=${resolution}`);
    if (!res.ok) {
      let detail = `体素场获取失败（${res.status}）`;
      try {
        const j = await res.json();
        if (j.detail) detail = j.detail;
      } catch { /* ignore */ }
      throw new Error(detail);
    }
    const raw = res.headers.get('X-Volume-Meta');
    const meta = JSON.parse(raw || '{}');
    const buffer = await res.arrayBuffer();
    if (!meta.shape) throw new Error('体素场元数据缺失');

    const grid = state.engine.setSolid(meta, buffer);
    state.volume = { meta, jobId: state.job.id, resolution, features: meta.features || null, grid };
    renderPrecision(meta);
    hideProgress();
    const mb = (meta.bytes / 1048576).toFixed(1);
    const why = cause ? `（本机失败：${cause.message || cause}）` : '';
    setStatus(`模型就绪 · ${state.job.filename}`, `服务端算${why} · ${meta.pitch.toFixed(3)} mm · ${mb} MB`);
    return state.volume;
  } catch (err) {
    hideProgress();
    toast(err.message || String(err));
    setStatus('体素化失败', '');
    return null;
  }
}

/* =========================================================================
 * 生成
 * ====================================================================== */
async function generate() {
  if (!state.job || state.busy) return;
  if (state.engineMode === 'gpu' && state.engine) await generateGpu();
  else await generateServer();
}

function buildLatticeGeometry(mesh) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3));
  geom.setAttribute('normal', new THREE.BufferAttribute(mesh.normals, 3));
  geom.setIndex(new THREE.BufferAttribute(mesh.indices, 1));
  geom.computeBoundingSphere();
  return geom;
}

async function generateGpu() {
  const volume = await prepareVolume();
  if (!volume) return;

  setBusy(true);
  showProgress(0.45, 'GPU 生成晶格…');
  setStatus('GPU 生成中…', '');
  await nextFrame();

  const rawParams = collectParams();
  const pitch = state.volume?.meta?.pitch || 0;
  const params = thinWallFix(rawParams, pitch);   // 薄壁兜底：画不出来的壁厚先抬上来
  const t0 = performance.now();
  try {
    const mesh = await state.engine.generate(params);
    state.lastMs = performance.now() - t0;
    state.mesh = mesh;
    mesh.stats.warnings = [...params.notes, ...latticeWarnings(params, pitch)];

    clearGroup(groupLattice);
    const holder = new THREE.Group();
    holder.rotation.x = -Math.PI / 2;   // Z-up → Y-up
    const m = new THREE.Mesh(buildLatticeGeometry(mesh), matLattice);
    m.frustumCulled = false;
    holder.add(m);
    groupLattice.add(holder);
    matLattice.wireframe = state.wireframe;

    renderStats(mesh.stats, { engine: `浏览器 GPU · ${state.engine.adapterLabel}` });
    setExportEnabled(true);
    hideProgress();
    setBusy(false);
    setStatus(
      `晶格已生成 · ${params.lattice}`,
      `${mesh.stats.faces.toLocaleString()} 面 · ${(state.lastMs / 1000).toFixed(2)} s`
    );
    if (state.view === 'original') setView('lattice');
    else setView(state.view);
    if (groupLattice.children.length === 1) frameAll(false);
  } catch (err) {
    console.error(err);
    hideProgress();
    setBusy(false);
    setStatus('生成失败', '');
    toast(err.message || String(err));
  }
}

async function generateServer() {
  setBusy(true);
  showProgress(0.02, '提交参数…');
  setStatus('生成中…', '');
  state.mesh = null;
  try {
    await fetchJSON(`${API}/jobs/${state.job.id}/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...collectParams(), decimate: +$('#decRange').value }),
    });
    pollJob(state.job.id);
  } catch (err) {
    setBusy(false);
    hideProgress();
    toast(err.message || String(err));
  }
}

/* =========================================================================
 * 导出
 * ====================================================================== */
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

function exportFilename(fmt) {
  const stem = (state.job?.filename || 'lattice').replace(/\.[^.]+$/, '') || 'lattice';
  return `${stem}_lattice.${fmt}`;
}

async function exportAs(fmt) {
  if (state.mesh) {
    setStatus(`正在导出 ${fmt.toUpperCase()}…`, '');
    await nextFrame();
    try {
      const blob = exportMesh(fmt, state.mesh, exportFilename(fmt));
      downloadBlob(blob, exportFilename(fmt));
      toast(`已导出 ${fmt.toUpperCase()}（${(blob.size / 1048576).toFixed(1)} MB）`, true);
      setStatus('导出完成', `${(blob.size / 1048576).toFixed(1)} MB`);
    } catch (err) {
      toast(`导出失败：${err.message || err}`);
    }
    return;
  }
  if (!state.job || state.latticeVersion < 0) {
    toast('请先生成晶格模型');
    return;
  }
  setStatus(`正在导出 ${fmt.toUpperCase()}…`, '');
  window.location.href = `${API}/jobs/${state.job.id}/download/${fmt}`;
  setTimeout(() => setStatus('导出已开始', ''), 1200);
}

/* =========================================================================
 * 建立模型：上传 / 样例 / 摆线轮盘
 * ====================================================================== */
function uploadWithProgress(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API}/jobs`);
    xhr.responseType = 'text';
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = 0.02 + 0.28 * (e.loaded / e.total);
        showProgress(pct, `上传 ${(e.loaded / 1048576).toFixed(1)} MB / ${(e.total / 1048576).toFixed(1)} MB`);
      }
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText || '{}'); } catch { data = { detail: xhr.responseText }; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data?.detail || `上传失败 (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('网络错误，上传失败'));
    const fd = new FormData();
    fd.append('file', file);
    showProgress(0.02, `准备上传 ${file.name}`);
    xhr.send(fd);
  });
}

async function handleFile(file) {
  if (!file) return;
  resetResult();
  setBusy(true);
  setStatus(`正在解析 ${file.name}…`, '');
  try {
    const job = await uploadWithProgress(file);
    await adoptJob(job);
  } catch (err) {
    hideProgress();
    setBusy(false);
    setStatus('上传失败', '');
    toast(err.message || String(err));
  }
}

async function loadSample(name) {
  resetResult();
  setBusy(true);
  showProgress(0.05, `加载样例 ${name}…`);
  setStatus(`加载样例 ${name}…`, '');
  try {
    const job = await fetchJSON(`${API}/samples/${name}`, { method: 'POST' });
    await adoptJob(job);
  } catch (err) {
    hideProgress();
    setBusy(false);
    toast(err.message || String(err));
  }
}

async function buildCycloid() {
  resetResult();
  setBusy(true);
  showProgress(0.05, '生成摆线轮盘…');
  setStatus('正在由参数生成轮盘…', '');
  try {
    const job = await fetchJSON(`${API}/jobs/from-cycloid`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectCycloid()),
    });
    await adoptJob(job);
  } catch (err) {
    hideProgress();
    setBusy(false);
    setStatus('生成失败', '');
    toast(err.message || String(err));
  }
}

/* =========================================================================
 * 摆线设计器参数带入（?p= 分享令牌）
 * ====================================================================== */
function decodeShare(str) {
  try {
    const b64 = str.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(escape(atob(b64))));
  } catch {
    return null;
  }
}

function applySharedCycloid() {
  const m = /[?&]p=([^&]+)/.exec(location.search);
  if (!m) return false;
  const parsed = decodeShare(m[1]);
  if (!parsed) return false;

  const map = {
    tooth_count: '#czc',
    eccentricity: '#ce',
    pin_circle_radius: '#crz',
    pin_radius: '#crp',
  };
  let applied = 0;
  for (const [key, sel] of Object.entries(map)) {
    if (parsed[key] !== undefined && parsed[key] !== null) {
      $(sel).value = parsed[key];
      applied += 1;
    }
  }
  if (!applied) return false;

  const note = $('#cycloidFromUrl');
  note.hidden = false;
  note.textContent = `已带入摆线设计器参数（Zc=${$('#czc').value}、e=${$('#ce').value}、Rz=${$('#crz').value}、rp=${$('#crp').value}），点上面的按钮即可生成轮盘。`;
  syncCycloidOutputs();
  return true;
}

/* =========================================================================
 * 事件绑定
 * ====================================================================== */
function bindEvents() {
  // —— 上传 ——
  const dropzone = $('#dropzone');
  const fileInput = $('#fileInput');
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files?.[0]) handleFile(fileInput.files[0]);
    fileInput.value = '';
  });
  for (const type of ['dragenter', 'dragover']) {
    dropzone.addEventListener(type, (ev) => {
      ev.preventDefault();
      dropzone.classList.add('dragover');
    });
  }
  for (const type of ['dragleave', 'drop']) {
    dropzone.addEventListener(type, (ev) => {
      ev.preventDefault();
      dropzone.classList.remove('dragover');
    });
  }
  dropzone.addEventListener('drop', (ev) => {
    const file = ev.dataTransfer?.files?.[0];
    if (file) handleFile(file);
  });
  window.addEventListener('dragover', (ev) => ev.preventDefault());
  window.addEventListener('drop', (ev) => ev.preventDefault());

  for (const btn of $$('[data-sample]')) {
    btn.addEventListener('click', () => loadSample(btn.dataset.sample));
  }

  // —— 摆线轮盘对话框 ——
  const dlg = $('#cycloidDialog');
  $('#btnCycloid').addEventListener('click', () => {
    syncCycloidOutputs();
    dlg.showModal();
  });
  for (const sel of ['#czc', '#ce', '#crz', '#crp', '#cwidth']) {
    $(sel).addEventListener('input', syncCycloidOutputs);
  }
  $('#btnBuildCycloid').addEventListener('click', (ev) => {
    ev.preventDefault();
    dlg.close();
    buildCycloid();
  });

  // —— 参数表单 ——
  $('#latticeSel').addEventListener('change', () => {
    syncLatticeLabels();
    syncDensity();
    if (state.mesh) schedulePreview();
  });
  $('#modeSel').addEventListener('change', () => {
    syncModeHint();
    if (state.mesh) schedulePreview();
  });
  for (const sel of ['#cellRange', '#thickRange', '#shellRange', '#warpRange', '#gradRange']) {
    $(sel).addEventListener('input', () => {
      if (sel === '#cellRange') clampThickness();
      syncOutputs();
      syncDensity();
      // 壁厚变了，「壁厚 / 体素」的精度提示也要跟着变
      if (sel === '#thickRange' || sel === '#cellRange') {
        if (state.volume?.meta) renderPrecision(state.volume.meta);
      }
      if (state.mesh) schedulePreview();
    });
  }
  $('#wallSel').addEventListener('change', () => state.mesh && schedulePreview());
  $('#skinChk').addEventListener('change', () => state.mesh && schedulePreview());
  $('#smoothRange').addEventListener('input', syncOutputs);
  $('#decRange').addEventListener('input', syncOutputs);

  // —— 引擎切换 ——
  for (const btn of $$('#engineSeg button')) {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      setEngine(btn.dataset.engine);
      if (state.engineMode === 'gpu') {
        await prepareVolume();
        if (state.mesh) generate();
      } else {
        toast('已切到服务器生成：会用服务端的 marching cubes + 抽稀');
      }
    });
  }

  // —— 分辨率 ——
  $('#resSel').addEventListener('change', async () => {
    syncOutputs();
    if (!state.job) return;
    if (state.engineMode === 'gpu') {
      await prepareVolume();
      if (state.mesh) generate();
    } else {
      setStatus('分辨率已改变，点「生成晶格」生效', '');
    }
  });
  $('#btnAutoRes').addEventListener('click', async () => {
    const features = state.features || state.volume?.features;
    let target = Number($('#btnAutoRes').dataset.target || 0);
    if (!target) {
      const holeR = features?.min_hole_radius;
      if (!holeR) return toast('还没有体素场信息，先生成一次模型');
      const extent = Math.max(...(state.job?.info?.extents || [100]));
      target = extent / (holeR / 2);
    }
    target = clampResolution(target);
    const sel = $('#resSel');
    const exists = Array.from(sel.options).some((o) => Number(o.value) === target);
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = String(target);
      opt.textContent = `${target}（按特征推荐）`;
      sel.appendChild(opt);
    }
    sel.value = String(target);
    syncOutputs();
    if (state.engineMode === 'gpu') await prepareVolume();
    toast(`分辨率已设为 ${target}`, true);
  });

  // —— 生成 / 导出 ——
  $('#btnGenerate').addEventListener('click', generate);
  $('#btnStl').addEventListener('click', () => exportAs('stl'));
  $('#btn3mf').addEventListener('click', () => exportAs('3mf'));
  $('#btnObj').addEventListener('click', () => exportAs('obj'));
  $('#btnPly').addEventListener('click', () => exportAs('ply'));

  // —— 视图 ——
  for (const btn of $$('#viewSeg button')) {
    btn.addEventListener('click', () => setView(btn.dataset.view));
  }
  $('#wireBtn').addEventListener('click', (ev) => {
    state.wireframe = !state.wireframe;
    matLattice.wireframe = state.wireframe;
    ev.currentTarget.classList.toggle('on', state.wireframe);
  });
  $('#rotateBtn').addEventListener('click', (ev) => {
    state.autoRotate = !state.autoRotate;
    ev.currentTarget.classList.toggle('on', state.autoRotate);
  });
  $('#resetBtn').addEventListener('click', () => frameAll());
  $('#clearBtn').addEventListener('click', () => {
    if (state.job) fetchJSON(`${API}/jobs/${state.job.id}`, { method: 'DELETE' }).catch(() => {});
    state.job = null;
    state.mesh = null;
    state.volume = null;
    resetResult();
    setBusy(false);
    setStatus('等待模型…', '');
  });
}

/** 参数变化后的自动重算（GPU 很快，直接重算；服务端模式要手动点） */
let previewTimer = null;
function schedulePreview() {
  if (state.engineMode !== 'gpu' || !state.engine) return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => generate(), 120);
}

/* =========================================================================
 * 初始化
 * ====================================================================== */
async function init() {
  resize();
  bindEvents();
  animate();

  try {
    state.config = await fetchJSON(`${API}/config`);
    populateConfig(state.config);
  } catch (err) {
    toast(`配置加载失败：${err.message || err}`);
  }

  await probeEngine();
  applyResourceLimits(state.config);
  syncOutputs();
  applySharedCycloid();
}

init();
