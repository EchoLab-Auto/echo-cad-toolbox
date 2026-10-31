/**
 * 浏览器端代码的 Node 测试（无浏览器依赖）。
 *
 * 覆盖 `app/static/lattice/client/` 下的三块：
 *   1. loaders.js       STL 二进制 / ASCII 解析、顶点焊接、退化面丢弃、坏文件报错
 *   2. voxelize.js      mesh → SDF（用解析解校验：盒子中心到最近面的距离）
 *   3. voxelize.worker  「主线程 ↔ Worker」的消息约定与 Transferable
 *
 * 用法：``node --experimental-default-type=module tools/test-client.mjs``
 * 退出码 0 = 全通过。CI 里由 tests/test_client_js.py 调用（没有 node 就跳过）。
 */

import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLIENT = path.join(HERE, '..', 'app', 'static', 'lattice', 'client');

const { parseStl, parseMeshBuffer } = await import(path.join(CLIENT, 'loaders.js'));
const { voxelize } = await import(path.join(CLIENT, 'voxelize.js'));

let failed = 0;
function check(ok, label, detail = '') {
  if (ok) {
    console.log(`  ✓ ${label}`);
  } else {
    failed++;
    console.log(`  ✗ ${label}${detail ? `  —— ${detail}` : ''}`);
  }
}

/* =========================================================================
 * 1. STL 解析
 * ====================================================================== */
console.log('STL 解析');

const enc = (s) => new TextEncoder().encode(s);

/** 造一个二进制 STL：`tris` 是 [[v0, v1, v2], ...]，每点 [x,y,z] */
function makeBinaryStl(tris, header = '') {
  const buf = new ArrayBuffer(84 + tris.length * 50);
  const dv = new DataView(buf);
  if (header) new Uint8Array(buf, 0, Math.min(80, header.length)).set(enc(header).slice(0, 80));
  dv.setUint32(80, tris.length, true);
  tris.forEach((vs, t) => {
    let p = 84 + t * 50 + 12;                 // 跳过法向 12 字节
    for (const [x, y, z] of vs) {
      dv.setFloat32(p, x, true);
      dv.setFloat32(p + 4, y, true);
      dv.setFloat32(p + 8, z, true);
      p += 12;
    }
  });
  return buf;
}

{
  const ascii = `solid box
 facet normal 0 0 1
  outer loop
   vertex 0 0 0
   vertex 10 0 0
   vertex 10 10 0
  endloop
 endfacet
 facet normal 0 0 1
  outer loop
   vertex 0 0 0
   vertex 10 10 0
   vertex 0 10 0
  endloop
 endfacet
endsolid box`;
  const m = parseStl(enc(ascii).buffer);
  check(m.format === 'stl-ascii', 'ASCII 识别为 stl-ascii');
  check(m.positions.length / 3 === 4, 'ASCII 顶点焊接成 4 个（原本 6 个）', `实得 ${m.positions.length / 3}`);
  check(m.indices.length / 3 === 2, 'ASCII 2 个三角面', `实得 ${m.indices.length / 3}`);
}

{
  // 80 字节头恰好以 "solid" 开头 —— 很多导出器把文件名塞进头里。
  // 判别必须靠文件长度，不能只看开头。
  const buf = makeBinaryStl(
    [[[0, 0, 0], [5, 0, 0], [0, 5, 0]], [[0, 0, 0], [5, 0, 0], [5, 5, 0]]],
    'solid exported-by-some-cad',
  );
  const m = parseStl(buf);
  check(m.format === 'stl-binary', '头含 "solid" 仍判为二进制');
  check(m.positions.length / 3 === 4, '二进制顶点焊接成 4 个', `实得 ${m.positions.length / 3}`);
  check(m.indices.length / 3 === 2, '二进制 2 个三角面', `实得 ${m.indices.length / 3}`);
}

{
  const buf = makeBinaryStl([
    [[0, 0, 0], [0, 0, 0], [0, 0, 0]],        // 三点重合 —— 退化
    [[0, 0, 0], [1, 0, 0], [0, 1, 0]],        // 正常
  ]);
  const m = parseStl(buf);
  check(m.indices.length / 3 === 1, '退化面被丢弃（剩 1 面）', `实得 ${m.indices.length / 3}`);
}

{
  let threw = '';
  try { parseStl(new ArrayBuffer(10)); } catch (e) { threw = e.message; }
  check(threw.includes('字节'), '过短文件报错且提示字节数', threw);

  threw = '';
  try { parseStl(new ArrayBuffer(200)); } catch (e) { threw = e.message; }
  check(threw.length > 0, '无效文件报错', threw);
}

/* =========================================================================
 * 2. 体素化（解析解校验）
 * ====================================================================== */
console.log('体素化');

const boxTris = (() => {
  const h = [20, 15, 12];                     // 40 × 30 × 24
  const v = [
    [-h[0], -h[1], -h[2]], [h[0], -h[1], -h[2]], [h[0], h[1], -h[2]], [-h[0], h[1], -h[2]],
    [-h[0], -h[1], h[2]], [h[0], -h[1], h[2]], [h[0], h[1], h[2]], [-h[0], h[1], h[2]],
  ];
  const idx = [
    [0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
    [0, 4, 5], [0, 5, 1], [1, 5, 6], [1, 6, 2],
    [2, 6, 7], [2, 7, 3], [3, 7, 4], [3, 4, 0],
  ];
  return { positions: v.flat(), indices: idx.flat() };
})();

const boxMesh = reindex(boxTris);
const vol = voxelize(boxMesh, { resolution: 64 });
const [nx, ny, nz] = vol.shape;
const at = (x, y, z) => vol.sdf[(x * ny + y) * nz + z];
const toVoxel = (mm, axis) => Math.round((mm - vol.origin[axis]) / vol.pitch - 0.5);

check(vol.sdf.length === nx * ny * nz, 'SDF 长度 = nx*ny*nz');
check(Math.abs(vol.pitch - 40 / 64) < 1e-6, 'pitch = 最长边 / 分辨率', `实得 ${vol.pitch}`);

{
  // 盒子中心：到最近面 = min(halfExtent) = 12 mm，符号为负
  const cx = toVoxel(0, 0); const cy = toVoxel(0, 1); const cz = toVoxel(0, 2);
  const d = at(cx, cy, cz);
  check(d < 0, '盒子中心 SDF 为负（在实体内部）', `实得 ${Number.isFinite(d) ? d.toFixed(3) : d}`);
  check(Math.abs(-d - 12) < 0.8, '盒子中心到最近面 ≈ 12 mm', `实得 ${(-d).toFixed(3)}`);
}

{
  // 沿 Z 穿过顶面（z = +12）：内部为负、外部为正，且符号在表面处翻转
  const cx = toVoxel(0, 0); const cy = toVoxel(0, 1);
  const insideZ = toVoxel(6, 2);             // z=6，实体内部
  const outsideZ = toVoxel(14, 2);           // z=14，实体上方 2 mm
  check(at(cx, cy, insideZ) < 0, 'z=6 处为实体内部');
  check(at(cx, cy, outsideZ) > 0, 'z=14 处为实体外部');
  const near = Math.abs(at(cx, cy, outsideZ) - 2) < 0.8;
  check(near, 'z=14 处距离 ≈ 2 mm', `实得 ${at(cx, cy, outsideZ).toFixed(3)}`);
}

{
  // 角点外侧：到盒子的距离 = |(dx,dy,dz)|。探针要留在 pad 以内（网格只有
  // 模型尺寸 + 每侧 3 层体素），否则 toVoxel 会越界。
  const ox = toVoxel(20.9, 0);               // 超出 +20 面 0.9 mm
  const oy = toVoxel(15.9, 1);               // 超出 +15 面 0.9 mm
  const oz = toVoxel(12.9, 2);               // 超出 +12 面 0.9 mm
  const expect = Math.hypot(0.9, 0.9, 0.9);
  const d = at(ox, oy, oz);
  check(Number.isFinite(d), '角点探针在体场内', `at(${ox},${oy},${oz}) = ${d}`);
  if (Number.isFinite(d)) {
    check(d > 0, '角点外侧符号为正', `实得 ${Number.isFinite(d) ? d.toFixed(3) : d}`);
    check(Math.abs(d - expect) < 1.2, `角点外侧距离 ≈ ${expect.toFixed(2)} mm`, `实得 ${Number.isFinite(d) ? d.toFixed(3) : d}`);
  }
}

{
  // 体素场应当"包住"整个模型：所有边界体素都在实体外
  let edgeInside = 0;
  for (let x = 0; x < nx; x++) {
    for (let y = 0; y < ny; y++) {
      if (at(x, y, 0) < 0 || at(x, y, nz - 1) < 0) edgeInside++;
    }
  }
  check(edgeInside === 0, '体场边界不含实体（pad 生效，等值面不会开口）', `${edgeInside} 个`);
}

{
  // 空网格要给出可读报错
  let threw = '';
  try { voxelize({ positions: [], indices: [] }); } catch (e) { threw = e.message; }
  check(threw.includes('网格'), '空网格报错可读', threw);
}

/* =========================================================================
 * 3. Worker 消息契约
 * ====================================================================== */
console.log('Worker 契约');

{
  const posted = [];
  globalThis.self = {
    onmessage: null,
    postMessage(msg, transfer) { posted.push({ msg, transfer }); },
  };
  await import(path.join(CLIENT, 'voxelize.worker.js'));
  check(typeof globalThis.self.onmessage === 'function', 'worker 挂上了 onmessage');

  self.onmessage({
    data: { id: 'k1', positions: boxMesh.positions, indices: boxMesh.indices, options: { resolution: 48 } },
  });

  const progress = posted.filter((p) => p.msg.type === 'progress');
  const done = posted.find((p) => p.msg.type === 'done');
  const err = posted.find((p) => p.msg.type === 'error');

  check(!err, '没有 error 消息', err?.msg.message);
  check(progress.length > 0, '有进度消息', `${progress.length} 条`);
  check(progress.every((p) => p.msg.id === 'k1'), '进度消息带同一个 id');
  check(progress.every((p) => typeof p.msg.fraction === 'number'), '进度分数是数字');
  check(!!done, '收到 done 消息');
  if (done) {
    const r = done.msg.result;
    const n = r.shape[0] * r.shape[1] * r.shape[2];
    check(r.sdf.length === n, 'done 里 SDF 长度与 shape 一致');
    check(done.transfer?.length === 1 && done.transfer[0] === r.sdf.buffer,
      'SDF 以 Transferable 回传（零拷贝）');
  }

  // 缺数据的请求要报错而不是静默
  posted.length = 0;
  self.onmessage({ data: { id: 'k2' } });
  check(posted.some((p) => p.msg.type === 'error' && p.msg.id === 'k2'), '缺网格数据时报 error');
}

/* =========================================================================
 * 汇总
 * ====================================================================== */
console.log('');
if (failed === 0) {
  console.log('✓ 全部通过');
} else {
  console.log(`✗ ${failed} 项失败`);
  process.exitCode = 1;
}

/** 把 [[三角形顶点索引], ...] 展开成 positions/indices（顶点按出现顺序去重） */
function reindex({ positions, indices }) {
  const map = new Map();
  const out = [];
  const idx = [];
  for (let i = 0; i < indices.length; i++) {
    const p = indices[i] * 3;
    const key = `${positions[p]},${positions[p + 1]},${positions[p + 2]}`;
    let id = map.get(key);
    if (id === undefined) {
      id = out.length / 3;
      map.set(key, id);
      out.push(positions[p], positions[p + 1], positions[p + 2]);
    }
    idx.push(id);
  }
  return { positions: Float32Array.from(out), indices: Uint32Array.from(idx) };
}
