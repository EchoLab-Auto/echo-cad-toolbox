/* ---------------------------------------------------------------------------
 * 晶格化工作台前端
 *
 * * 两个模型来源：摆线轮参数（服务端生成 3D 轮盘）/ 上传 STL·STEP
 * * 参数表单 → POST /api/lattice/jobs/{id}/generate → 轮询状态 → three.js 预览
 * * 支持从摆线设计器的分享链接（?p=…）带入参数，做到"设计完直接晶格化"
 * ------------------------------------------------------------------------ */

import * as THREE from 'three';
import { OrbitControls } from '/static/lattice/vendor/three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from '/static/lattice/vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from '/static/lattice/vendor/three/examples/jsm/environments/RoomEnvironment.js';

const API = '/api/lattice';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  job: null,
  source: 'cycloid',
  view: 'original',
  wireframe: false,
  autoRotate: false,
  busy: false,
  pollTimer: null,
  latticeVersion: -1,
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
scene.background = new THREE.Color(0x080b10);

const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.05).texture;

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

// 调试 / 支持用钩子（不参与业务逻辑）：控制台里 __lattice.state 可看作业状态
window.__lattice = {
  state, scene, camera, renderer, controls,
  groups: { original: groupOriginal, lattice: groupLattice },
  frameAll,
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
    // trimesh 导出的 GLB 只带 POSITION，没有 NORMAL。缺法向时 PBR 光照失效，
    // 模型会渲染成纯黑（看起来像"没加载出来"），这里补算一次。
    const geom = o.geometry;
    if (geom && !geom.attributes.normal) {
      if (!geom.attributes.position) return;
      geom.computeVertexNormals();
    }
  });
}

function sceneBounds() {
  const box = new THREE.Box3();
  let has = false;
  for (const group of [groupLattice, groupOriginal]) {
    if (group.children.length === 0) continue;
    box.expandByObject(group);
    has = true;
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
  el.classList.toggle('ok', ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 2600 : 6500);
}

const setStatus = (text, right = '') => {
  $('#statusText').textContent = text;
  $('#statusRight').textContent = right;
};

function showProgress(pct, stage) {
  $('#progressOverlay').hidden = false;
  $('#progressFill').style.width = `${Math.round(pct * 100)}%`;
  $('#progressPct').textContent = `${Math.round(pct * 100)}%`;
  $('#progressStage').textContent = stage || '处理中…';
}
const hideProgress = () => { $('#progressOverlay').hidden = true; };

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

  syncLatticeUI();
  syncOutputs();
}

/**
 * 按服务端探测到的内存能力限制分辨率选项。
 *
 * 体素化的内存开销随分辨率线性上涨（160→380MB，200→830MB，256→1.4GB+），
 * 在内存紧张的机器上选高分辨率会把容器 OOM 掉、连带丢掉所有作业。
 * 这里直接把超限选项禁用，并把默认值拉到允许范围内。
 */
function applyResourceLimits(cfg) {
  const res = cfg.resources || {};
  const ceiling = Number(res.max_resolution) || 320;
  const sel = $('#resSel');
  let best = null;
  for (const opt of sel.options) {
    const value = Number(opt.value);
    const allowed = value <= ceiling;
    opt.disabled = !allowed;
    if (allowed) best = opt.value;
    opt.textContent = opt.textContent.replace(/（.*?不允许）$/, '');
    if (!allowed) opt.textContent += '（内存不允许）';
  }
  const wanted = String(cfg.defaults.resolution);
  sel.value = Number(wanted) <= ceiling ? wanted : String(best || ceiling);

  const note = $('#resHint');
  if (note) {
    const mem = res.memory_limit_mb;
    note.textContent = mem
      ? `本机可用内存约 ${mem} MB，分辨率上限 ${ceiling}（再高会因内存不足中断）。`
      : '';
    note.hidden = !mem;
  }
}

function syncLatticeUI() {
  const kind = $('#latticeSel').value;
  const info = state.config?.lattices?.[kind];
  $('#latticeDesc').textContent = info?.desc || '';
  const isTpms = info?.family === 'tpms';
  $('#wallSel').closest('.field').hidden = !isTpms;
  $('#thickLabel').textContent = isTpms ? '壁厚' : '杆直径';
  $('#thickRange').max = isTpms ? '12' : '10';
}

function syncOutputs() {
  const cell = +$('#cellRange').value;
  const thick = +$('#thickRange').value;
  $('#cellOut').textContent = `${cell.toFixed(1)} mm`;
  $('#thickOut').textContent = `${thick.toFixed(2)} mm`;
  $('#shellOut').textContent = `${(+$('#shellRange').value).toFixed(1)} mm`;
  $('#resOut').textContent = $('#resSel').value;
  $('#warpOut').textContent = (+$('#warpRange').value).toFixed(2);
  $('#gradOut').textContent = (+$('#gradRange').value).toFixed(2);
  $('#smoothOut').textContent = $('#smoothRange').value;
  $('#decOut').textContent = `${Math.round(+$('#decRange').value * 100)}%`;

  const mode = $('#modeSel').value;
  $('#shellField').hidden = mode !== 'shell';
  $('#modeDesc').textContent = state.config?.modes?.[mode] || '';

  // 摆线轮参数读数
  $('#czcOut').textContent = $('#czc').value;
  $('#ceOut').textContent = `${(+$('#ce').value).toFixed(2)} mm`;
  $('#crzOut').textContent = `${(+$('#crz').value).toFixed(1)} mm`;
  $('#crpOut').textContent = `${(+$('#crp').value).toFixed(2)} mm`;
  $('#cwidthOut').textContent = `${(+$('#cwidth').value).toFixed(1)} mm`;
  renderCycloidKpi();

  // 相对密度预估（与后端 estimate_relative_density 同一套校准系数）
  const kind = $('#latticeSel').value;
  const info = state.config?.lattices?.[kind];
  const t = Math.min(thick, 0.8 * cell);
  let rho = 0.2;
  if (info?.family === 'tpms' && TPMS_DENSITY[kind]) {
    rho = (TPMS_DENSITY[kind] * t) / cell;
  } else if (STRUT_LENGTH[kind]) {
    rho = ((STRUT_LENGTH[kind] * Math.PI * (t / 2) ** 2) / (cell * cell)) * STRUT_OVERLAP[kind];
  }
  rho = Math.max(0.005, Math.min(0.95, rho));
  $('#densityFill').style.width = `${(rho * 100).toFixed(1)}%`;
  $('#densityPct').textContent = `${(rho * 100).toFixed(1)} %`;
}

function renderCycloidKpi() {
  const zc = +$('#czc').value;
  const e = +$('#ce').value;
  const rz = +$('#crz').value;
  const rp = +$('#crp').value;
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

function clampThickness() {
  const cell = +$('#cellRange').value;
  const thickEl = $('#thickRange');
  const limit = Math.max(0.2, Math.min(+thickEl.max, 0.8 * cell));
  if (+thickEl.value > limit) thickEl.value = limit.toFixed(2);
}

function collectParams() {
  return {
    lattice: $('#latticeSel').value,
    cell_size: +$('#cellRange').value,
    thickness: +$('#thickRange').value,
    wall_mode: $('#wallSel').value,
    mode: $('#modeSel').value,
    shell_thickness: +$('#shellRange').value,
    keep_skin: $('#skinChk').checked,
    resolution: +$('#resSel').value,
    warp: +$('#warpRange').value,
    grad: +$('#gradRange').value,
    smooth: +$('#smoothRange').value,
    decimate: +$('#decRange').value,
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
 * 作业
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

function renderStats(stats) {
  if (!stats) return;
  const rows = [
    ['面数', Number(stats.faces).toLocaleString()],
    ['体积', `${(stats.volume_mm3 / 1000).toFixed(2)} cm³`],
    ['材料体积', `${(stats.material_volume_mm3 / 1000).toFixed(2)} cm³`],
    ['相对密度', `${(stats.relative_density * 100).toFixed(1)} %`],
    ['网格闭合', stats.boundary_edges === 0 ? '是' : `否（${stats.boundary_edges} 条开口边）`],
    ['体素', `${stats.voxel_pitch.toFixed(3)} mm`],
    ['耗时', `${stats.seconds} s`],
  ];
  $('#statsKv').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  $('#statsPanel').hidden = false;
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

function applyJob(job) {
  state.job = job;
  if (job.info) renderModelInfo(job);

  const running = job.status === 'preparing' || job.status === 'running';
  if (running) showProgress(job.progress || 0.05, job.stage || '处理中…');
  else hideProgress();

  if (job.status === 'error') {
    setBusy(false);
    setStatus('出错', '');
    toast(job.error || '处理失败');
    return;
  }

  if (job.status === 'ready') {
    setBusy(false);
    setExportEnabled(false);
    setStatus(`模型就绪 · ${job.filename}`, job.info?.voxel
      ? `体素 ${job.info.voxel.resolution} · ${job.info.voxel.pitch.toFixed(3)} mm` : '');
  }

  if (job.status === 'done') {
    setBusy(false);
    renderStats(job.stats);
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
  setView('original');
  $('#statsPanel').hidden = true;
  $('#modelInfo').hidden = true;
  setExportEnabled(false);
}

async function adoptJob(job) {
  applyJob(job);
  await loadPreview('original');
  setView('original');
  pollJob(job.id);
}

/* =========================================================================
 * 建立模型：摆线轮盘 / 上传
 * ====================================================================== */
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

/* =========================================================================
 * 生成 / 导出
 * ====================================================================== */
async function generate() {
  if (!state.job || state.busy) return;
  setBusy(true);
  showProgress(0.02, '提交参数…');
  setStatus('生成中…', '');
  try {
    await fetchJSON(`${API}/jobs/${state.job.id}/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectParams()),
    });
    pollJob(state.job.id);
  } catch (err) {
    setBusy(false);
    hideProgress();
    toast(err.message || String(err));
  }
}

function downloadAs(fmt) {
  if (!state.job || state.latticeVersion < 0) {
    toast('请先生成晶格模型');
    return;
  }
  setStatus(`正在导出 ${fmt.toUpperCase()}…`, '');
  window.location.href = `${API}/jobs/${state.job.id}/download/${fmt}`;
  setTimeout(() => setStatus('导出已开始', ''), 1200);
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
  note.textContent = `已带入摆线设计器参数（Zc=${$('#czc').value}）。`;
  syncOutputs();
  return true;
}

/* =========================================================================
 * 事件绑定
 * ====================================================================== */
function switchSource(source) {
  state.source = source;
  $$('#sourceSeg button').forEach((b) => b.classList.toggle('active', b.dataset.source === source));
  $('#cycloidSource').hidden = source !== 'cycloid';
  $('#uploadSource').hidden = source !== 'upload';
}

function bindUI() {
  $$('#sourceSeg button').forEach((btn) =>
    btn.addEventListener('click', () => switchSource(btn.dataset.source))
  );

  // --- 摆线轮参数 ---
  for (const id of ['#czc', '#ce', '#crz', '#crp', '#cwidth']) {
    $(id).addEventListener('input', syncOutputs);
  }
  $('#btnBuildCycloid').addEventListener('click', buildCycloid);

  // --- 上传 ---
  const dropzone = $('#dropzone');
  const fileInput = $('#fileInput');
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener('change', () => {
    handleFile(fileInput.files?.[0]);
    fileInput.value = '';
  });
  ['dragenter', 'dragover'].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add('dragover'); })
  );
  ['dragleave', 'drop'].forEach((type) =>
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove('dragover'); })
  );
  dropzone.addEventListener('drop', (e) => handleFile(e.dataTransfer?.files?.[0]));
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  $$('[data-sample]').forEach((btn) =>
    btn.addEventListener('click', () => loadSample(btn.dataset.sample))
  );

  $('#clearBtn').addEventListener('click', () => {
    if (state.job) fetchJSON(`${API}/jobs/${state.job.id}`, { method: 'DELETE' }).catch(() => {});
    state.job = null;
    resetResult();
    setBusy(false);
    $('#btnGenerate').disabled = true;
    setStatus('等待模型…', '');
  });

  // --- 晶格参数 ---
  const paramIds = [
    'latticeSel', 'cellRange', 'thickRange', 'modeSel', 'shellRange',
    'resSel', 'warpRange', 'gradRange', 'smoothRange', 'decRange', 'wallSel', 'skinChk',
  ];
  for (const id of paramIds) {
    $(`#${id}`).addEventListener('input', () => {
      if (id === 'latticeSel') syncLatticeUI();
      if (id === 'cellRange') clampThickness();
      syncOutputs();
    });
  }

  $('#btnGenerate').addEventListener('click', generate);
  $('#btnStl').addEventListener('click', () => downloadAs('stl'));
  $('#btn3mf').addEventListener('click', () => downloadAs('3mf'));
  $('#btnObj').addEventListener('click', () => downloadAs('obj'));
  $('#btnPly').addEventListener('click', () => downloadAs('ply'));

  // --- 视图 ---
  $$('#viewSeg button').forEach((btn) =>
    btn.addEventListener('click', () => setView(btn.dataset.view))
  );
  $('#wireBtn').addEventListener('click', () => {
    state.wireframe = !state.wireframe;
    matLattice.wireframe = state.wireframe;
    $('#wireBtn').classList.toggle('active', state.wireframe);
  });
  $('#rotateBtn').addEventListener('click', () => {
    state.autoRotate = !state.autoRotate;
    $('#rotateBtn').classList.toggle('active', state.autoRotate);
  });
  $('#resetBtn').addEventListener('click', () => frameAll(true));

  // 快捷键：G 生成、F 适配、W 线框
  window.addEventListener('keydown', (e) => {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
    if (e.key === 'g' || e.key === 'G') generate();
    else if (e.key === 'f' || e.key === 'F') frameAll(true);
    else if (e.key === 'w' || e.key === 'W') $('#wireBtn').click();
  });
}

/* =========================================================================
 * 启动
 * ====================================================================== */
async function boot() {
  resize();
  animate();
  bindUI();
  switchSource('cycloid');
  try {
    state.config = await fetchJSON(`${API}/config`);
    populateConfig(state.config);
  } catch (err) {
    toast(`配置加载失败：${err.message}`);
  }
  const shared = applySharedCycloid();
  setStatus('等待模型…', shared ? '已带入摆线参数，可直接生成轮盘' : 'G 生成 · F 适配视图');
  if (shared) buildCycloid();
}

boot();
