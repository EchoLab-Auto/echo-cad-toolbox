/* =============================================================================
 * 网格导出（纯浏览器，不经过服务器）
 *
 * 支持 STL（二进制）/ OBJ / PLY（二进制小端）/ 3MF（ZIP store，不压缩）。
 * 单位统一毫米 —— 3MF 里显式写 unit="millimeter"。
 * ========================================================================== */

'use strict';

/** 面法向（单位化） */
function faceNormal(p, ia, ib, ic, out) {
  const ax = p[ia];
  const ay = p[ia + 1];
  const az = p[ia + 2];
  const ux = p[ib] - ax;
  const uy = p[ib + 1] - ay;
  const uz = p[ib + 2] - az;
  const vx = p[ic] - ax;
  const vy = p[ic + 1] - ay;
  const vz = p[ic + 2] - az;
  let nx = uy * vz - uz * vy;
  let ny = uz * vx - ux * vz;
  let nz = ux * vy - uy * vx;
  const len = Math.hypot(nx, ny, nz) || 1;
  out[0] = nx / len;
  out[1] = ny / len;
  out[2] = nz / len;
  return out;
}

/* ---------------------------------------------------------------------------
 * STL（二进制）：80 字节头 + uint32 面数 + 每面 50 字节
 * ------------------------------------------------------------------------ */
export function toSTL(positions, indices, header = 'echo-cad-toolbox lattice') {
  const tris = Math.floor(indices.length / 3);
  const buffer = new ArrayBuffer(84 + tris * 50);
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);

  const enc = new TextEncoder().encode(header.slice(0, 79));
  bytes.set(enc, 0);
  view.setUint32(80, tris, true);

  const n = [0, 0, 0];
  let off = 84;
  for (let t = 0; t < tris; t += 1) {
    const ia = indices[t * 3] * 3;
    const ib = indices[t * 3 + 1] * 3;
    const ic = indices[t * 3 + 2] * 3;
    faceNormal(positions, ia, ib, ic, n);
    view.setFloat32(off, n[0], true);
    view.setFloat32(off + 4, n[1], true);
    view.setFloat32(off + 8, n[2], true);
    view.setFloat32(off + 12, positions[ia], true);
    view.setFloat32(off + 16, positions[ia + 1], true);
    view.setFloat32(off + 20, positions[ia + 2], true);
    view.setFloat32(off + 24, positions[ib], true);
    view.setFloat32(off + 28, positions[ib + 1], true);
    view.setFloat32(off + 32, positions[ib + 2], true);
    view.setFloat32(off + 36, positions[ic], true);
    view.setFloat32(off + 40, positions[ic + 1], true);
    view.setFloat32(off + 44, positions[ic + 2], true);
    view.setUint16(off + 48, 0, true);
    off += 50;
  }
  return buffer;
}

/* ---------------------------------------------------------------------------
 * OBJ：文本，v / vn / f v//vn
 * ------------------------------------------------------------------------ */
export function toOBJ(positions, normals, indices, name = 'lattice') {
  const parts = [`# echo-cad-toolbox · ${name} · unit: mm\n`, `o ${name}\n`];
  const f = (v) => (Math.abs(v) < 1e-6 ? '0' : v.toFixed(4));
  for (let i = 0; i < positions.length; i += 3) {
    parts.push(`v ${f(positions[i])} ${f(positions[i + 1])} ${f(positions[i + 2])}\n`);
  }
  if (normals && normals.length === positions.length) {
    for (let i = 0; i < normals.length; i += 3) {
      parts.push(`vn ${f(normals[i])} ${f(normals[i + 1])} ${f(normals[i + 2])}\n`);
    }
    for (let t = 0; t < indices.length; t += 3) {
      const a = indices[t] + 1;
      const b = indices[t + 1] + 1;
      const c = indices[t + 2] + 1;
      parts.push(`f ${a}//${a} ${b}//${b} ${c}//${c}\n`);
    }
  } else {
    for (let t = 0; t < indices.length; t += 3) {
      parts.push(`f ${indices[t] + 1} ${indices[t + 1] + 1} ${indices[t + 2] + 1}\n`);
    }
  }
  return parts.join('');
}

/* ---------------------------------------------------------------------------
 * PLY（二进制小端）：vertex(x,y,z,nx,ny,nz) + face(list uchar int)
 * ------------------------------------------------------------------------ */
export function toPLY(positions, normals, indices) {
  const nv = Math.floor(positions.length / 3);
  const nf = Math.floor(indices.length / 3);
  const header =
    'ply\n' +
    'format binary_little_endian 1.0\n' +
    'comment echo-cad-toolbox lattice (unit: mm)\n' +
    `element vertex ${nv}\n` +
    'property float x\nproperty float y\nproperty float z\n' +
    'property float nx\nproperty float ny\nproperty float nz\n' +
    `element face ${nf}\n` +
    'property list uchar int vertex_indices\n' +
    'end_header\n';

  const headerBytes = new TextEncoder().encode(header);
  const vBytes = nv * 24;
  const fBytes = nf * 13; // 1 字节计数 + 3×4 字节索引
  const buffer = new ArrayBuffer(headerBytes.length + vBytes + fBytes);
  const bytes = new Uint8Array(buffer);
  bytes.set(headerBytes, 0);

  const dv = new DataView(buffer);
  let off = headerBytes.length;
  const hasNormals = normals && normals.length === positions.length;
  for (let i = 0; i < nv; i += 1) {
    dv.setFloat32(off, positions[i * 3], true);
    dv.setFloat32(off + 4, positions[i * 3 + 1], true);
    dv.setFloat32(off + 8, positions[i * 3 + 2], true);
    dv.setFloat32(off + 12, hasNormals ? normals[i * 3] : 0, true);
    dv.setFloat32(off + 16, hasNormals ? normals[i * 3 + 1] : 0, true);
    dv.setFloat32(off + 20, hasNormals ? normals[i * 3 + 2] : 0, true);
    off += 24;
  }
  for (let t = 0; t < nf; t += 1) {
    bytes[off] = 3;
    dv.setInt32(off + 1, indices[t * 3], true);
    dv.setInt32(off + 5, indices[t * 3 + 1], true);
    dv.setInt32(off + 9, indices[t * 3 + 2], true);
    off += 13;
  }
  return buffer;
}

/* ---------------------------------------------------------------------------
 * 3MF：一个最小 OPC 包（ZIP store，无压缩）
 * ------------------------------------------------------------------------ */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let c = i;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** 最小 ZIP 写入器（store 法，足够 3MF 用） */
function zipStore(files) {
  const chunks = [];
  const central = [];
  let offset = 0;
  const enc = new TextEncoder();

  for (const file of files) {
    const nameBytes = enc.encode(file.name);
    const data = file.data instanceof Uint8Array ? file.data : new Uint8Array(file.data);
    const crc = crc32(data);

    const local = new Uint8Array(30 + nameBytes.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);           // version needed
    lv.setUint16(6, 0, true);            // flags
    lv.setUint16(8, 0, true);            // method: store
    lv.setUint16(10, 0, true);           // time
    lv.setUint16(12, 0x21, true);        // date (1996-01-01, 固定值)
    lv.setUint32(14, crc, true);
    lv.setUint32(18, data.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, nameBytes.length, true);
    lv.setUint16(28, 0, true);
    local.set(nameBytes, 30);

    chunks.push(local, data);

    const entry = new Uint8Array(46 + nameBytes.length);
    const cv = new DataView(entry.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(8, 0, true);
    cv.setUint16(10, 0, true);
    cv.setUint16(12, 0, true);
    cv.setUint16(14, 0x21, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, nameBytes.length, true);
    cv.setUint16(30, 0, true);
    cv.setUint16(32, 0, true);
    cv.setUint16(34, 0, true);
    cv.setUint16(36, 0, true);
    cv.setUint32(38, 0, true);
    cv.setUint32(42, offset, true);
    entry.set(nameBytes, 46);
    central.push(entry);

    offset += local.length + data.length;
  }

  const centralSize = central.reduce((sum, c) => sum + c.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, files.length, true);
  ev.setUint16(10, files.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);

  return new Blob([...chunks, ...central, end], { type: 'application/vnd.ms-package.3dmodel' });
}

export function to3MF(positions, indices, name = 'lattice') {
  const nv = Math.floor(positions.length / 3);
  const nf = Math.floor(indices.length / 3);
  const v = new Array(nv);
  for (let i = 0; i < nv; i += 1) {
    v[i] = `<vertex x="${positions[i * 3].toFixed(4)}" y="${positions[i * 3 + 1].toFixed(4)}" z="${positions[i * 3 + 2].toFixed(4)}"/>`;
  }
  const t = new Array(nf);
  for (let i = 0; i < nf; i += 1) {
    t[i] = `<triangle v1="${indices[i * 3]}" v2="${indices[i * 3 + 1]}" v3="${indices[i * 3 + 2]}"/>`;
  }

  const model =
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<model unit="millimeter" xml:lang="zh-CN" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n' +
    ` <metadata name="Title">${name}</metadata>\n` +
    ' <metadata name="Application">echo-cad-toolbox</metadata>\n' +
    ' <resources>\n' +
    '  <object id="1" type="model">\n' +
    '   <mesh>\n' +
    '    <vertices>\n' +
    v.join('\n') +
    '\n    </vertices>\n    <triangles>\n' +
    t.join('\n') +
    '\n    </triangles>\n   </mesh>\n  </object>\n </resources>\n' +
    ' <build><item objectid="1"/></build>\n</model>\n';

  const contentTypes =
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n' +
    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n' +
    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n' +
    '</Types>\n';

  const rels =
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n' +
    ' <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' +
    '</Relationships>\n';

  const enc = new TextEncoder();
  return zipStore([
    { name: '[Content_Types].xml', data: enc.encode(contentTypes) },
    { name: '_rels/.rels', data: enc.encode(rels) },
    { name: '3D/3dmodel.model', data: enc.encode(model) },
  ]);
}

/** 统一入口：按格式返回 Blob */
export function exportMesh(fmt, mesh, name = 'lattice') {
  const { positions, normals, indices } = mesh;
  switch (fmt) {
    case 'stl':
      return new Blob([toSTL(positions, indices, `echo-cad-toolbox ${name}`)], { type: 'model/stl' });
    case 'obj':
      return new Blob([toOBJ(positions, normals, indices, name)], { type: 'text/plain' });
    case 'ply':
      return new Blob([toPLY(positions, normals, indices)], { type: 'application/octet-stream' });
    case '3mf':
      return to3MF(positions, indices, name);
    default:
      throw new Error(`不支持的导出格式 ${fmt}`);
  }
}
