/**
 * 用浏览器端的体素化实现处理夹具，导出 SDF 供 Python 对比。
 *
 * 用法：node --experimental-default-type=module tmp-verify/run_voxelize.mjs box
 */
import fs from 'node:fs';

const name = process.argv[2] || 'box';
const HERE = new URL('.', import.meta.url);
const OUT = process.env.FIX_DIR || new URL('fix/', HERE).pathname;

const { voxelize } = await import(new URL('../app/static/lattice/client/voxelize.js', HERE).href);

const posBuf = fs.readFileSync(`${OUT}/${name}_pos.bin`);
const idxBuf = fs.readFileSync(`${OUT}/${name}_idx.bin`);
const positions = new Float32Array(posBuf.buffer, posBuf.byteOffset, posBuf.length / 4);
const indices = new Uint32Array(idxBuf.buffer, idxBuf.byteOffset, idxBuf.length / 4);

const meshInfo = JSON.parse(fs.readFileSync(`${OUT}/${name}_mesh.json`, 'utf8'));
const resolution = meshInfo.resolution;

console.log(`[${name}] ${positions.length / 3} 顶点 / ${indices.length / 3} 面  res=${resolution}`);

const t0 = Date.now();
let lastStage = '';
const result = voxelize(
  { positions, indices },
  {
    resolution,
    onProgress: (f, stage) => {
      if (stage !== lastStage) {
        console.log(`   ${stage} …`);
        lastStage = stage;
      }
    },
  },
);
const ms = Date.now() - t0;

const sdf = result.sdf;
fs.writeFileSync(`${OUT}/${name}_js_sdf.bin`, Buffer.from(sdf.buffer, sdf.byteOffset, sdf.byteLength));
fs.writeFileSync(`${OUT}/${name}_js_meta.json`, JSON.stringify({
  shape: result.shape,
  pitch: result.pitch,
  origin: result.origin,
  bounds: result.bounds,
  fill_ratio: result.fillRatio,
  ms,
}, null, 1));

console.log(`   shape=${result.shape.join('x')} pitch=${result.pitch.toFixed(4)} `
  + `fill=${result.fillRatio.toFixed(4)} 耗时 ${ms} ms`);
