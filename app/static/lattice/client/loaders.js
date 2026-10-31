/**
 * 浏览器端网格解析：把用户选的文件读成三角网格，全程不上传。
 *
 * 目前覆盖 STL（二进制 / ASCII）—— 它是最常见的打印格式，也是服务端
 * ``/jobs/{id}/mesh`` 回传时用的格式。OBJ / PLY / 3MF 后续按同一接口补，
 * 调用方只依赖 ``parseMeshFile`` / ``parseMeshBuffer``，不用改。
 *
 * 关键点：**顶点必须去重**。STL 每个三角形独立存三个顶点，⌀167 的盘就有
 * 4.8 万个顶点位置、实际只有 8 千个——不去重会让后续体素化的三角形索引膨胀
 * 三倍，射线求交和空间网格都跟着慢。
 */

export const MESH_EXTENSIONS = ['.stl'];

/** 顶点去重的量化精度（mm）。打印件的坐标精度远高于此。 */
const WELD_TOLERANCE = 1e-5;

/**
 * 解析网格文件。
 * @param {File|Blob} file
 * @param {(fraction: number, stage: string) => void} [onProgress]
 * @returns {Promise<{positions: Float32Array, indices: Uint32Array, format: string}>}
 */
export async function parseMeshFile(file, onProgress) {
  const buffer = await file.arrayBuffer();
  const ext = (file.name || '').toLowerCase().match(/\.[a-z0-9]+$/)?.[0] || '';
  return parseMeshBuffer(buffer, ext, onProgress);
}

/**
 * 解析网格二进制内容。
 * @param {ArrayBuffer} buffer
 * @param {string} [ext] 扩展名（含点）；省略时按内容嗅探
 * @param {(fraction: number, stage: string) => void} [onProgress]
 */
export function parseMeshBuffer(buffer, ext, onProgress) {
  const name = (ext || '').toLowerCase();
  if (name === '.stl' || name === '') return parseStl(buffer, onProgress);
  throw new Error(`暂不支持 ${name} 格式：目前浏览器端只解析 STL（其它格式走服务端转换）`);
}

/* =========================================================================
 * STL
 * ====================================================================== */

/**
 * STL 解析，自动判别二进制与 ASCII。
 *
 * 判别依据是**文件长度**而不是头部字符串：很多二进制 STL 的 80 字节头恰好
 * 以 "solid" 开头（导出器把文件名塞进去），只看开头会把二进制当成 ASCII 读，
 * 然后解析出一堆 NaN。
 */
export function parseStl(buffer, onProgress) {
  if (buffer.byteLength < 84) {
    throw new Error(`STL 解析失败：文件只有 ${buffer.byteLength} 字节，不像合法的 STL`);
  }
  const view = new DataView(buffer);
  const triCount = view.getUint32(80, true);
  const expected = 84 + triCount * 50;

  if (triCount > 0 && Math.abs(expected - buffer.byteLength) <= 2) {
    return parseBinaryStl(buffer, triCount, onProgress);
  }
  const head = new TextDecoder('utf-8', { fatal: false })
    .decode(new Uint8Array(buffer, 0, Math.min(512, buffer.byteLength)))
    .trimStart()
    .slice(0, 5)
    .toLowerCase();
  if (head === 'solid') return parseAsciiStl(buffer, onProgress);
  throw new Error(
    `STL 解析失败：面数 ${triCount} 与文件长度 ${buffer.byteLength} 字节对不上，也不是 ASCII STL`
  );
}

function parseBinaryStl(buffer, triCount, onProgress) {
  const view = new DataView(buffer);
  // 3 个顶点 × 3 个 float32 = 9 个 float32 需要保留（法向用不上）
  const raw = new Float32Array(triCount * 9);
  let out = 0;
  for (let t = 0; t < triCount; t++) {
    const base = 84 + t * 50 + 12;   // 跳过法向 3 个 float32
    for (let k = 0; k < 9; k++) {
      raw[out++] = view.getFloat32(base + k * 4, true);
    }
    if (onProgress && t % 20000 === 0) onProgress(t / triCount, '解析 STL');
  }
  if (onProgress) onProgress(1, '解析 STL');
  return weld(raw, triCount, 'stl-binary');
}

function parseAsciiStl(buffer, onProgress) {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(new Uint8Array(buffer));
  const coords = [];
  // 逐行扫 vertex，比正则整体匹配省内存（大文件几十 MB 文本）
  const re = /vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)/g;
  let m;
  let lines = 0;
  const total = text.length;
  while ((m = re.exec(text)) !== null) {
    coords.push(+m[1], +m[2], +m[3]);
    if (onProgress && (lines++ & 0x3fff) === 0) onProgress(m.index / total, '解析 STL');
  }
  if (coords.length < 9 || coords.length % 9 !== 0) {
    throw new Error(`STL 解析失败：读到 ${coords.length / 3} 个顶点，不是 3 的倍数`);
  }
  if (onProgress) onProgress(1, '解析 STL');
  return weld(Float32Array.from(coords), coords.length / 9, 'stl-ascii');
}

/**
 * 顶点焊接 + 去掉退化三角形。
 *
 * @param {Float32Array} raw  每 9 个数一个三角形（3 个顶点 × xyz）
 * @param {number} triCount
 * @param {string} format
 */
function weld(raw, triCount, format) {
  const map = new Map();
  const verts = [];
  const indices = new Uint32Array(triCount * 3);
  let nIdx = 0;
  const scale = 1 / WELD_TOLERANCE;

  for (let t = 0; t < triCount; t++) {
    const base = t * 9;
    const tri = [0, 0, 0];
    for (let v = 0; v < 3; v++) {
      const x = raw[base + v * 3];
      const y = raw[base + v * 3 + 1];
      const z = raw[base + v * 3 + 2];
      if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
        tri[v] = -1;
        continue;
      }
      const key = `${Math.round(x * scale)},${Math.round(y * scale)},${Math.round(z * scale)}`;
      let id = map.get(key);
      if (id === undefined) {
        id = verts.length / 3;
        map.set(key, id);
        verts.push(x, y, z);
      }
      tri[v] = id;
    }
    // 退化三角形（有重复顶点 / 非法坐标）直接丢：体素化时它们对奇偶填充
    // 毫无贡献，只会拖慢求交。
    if (tri[0] < 0 || tri[1] < 0 || tri[2] < 0) continue;
    if (tri[0] === tri[1] || tri[1] === tri[2] || tri[0] === tri[2]) continue;
    indices[nIdx++] = tri[0];
    indices[nIdx++] = tri[1];
    indices[nIdx++] = tri[2];
  }

  if (nIdx === 0) throw new Error('STL 解析失败：没有有效三角形（可能全是退化面）');
  return {
    positions: Float32Array.from(verts),
    indices: indices.slice(0, nIdx),
    format,
  };
}
