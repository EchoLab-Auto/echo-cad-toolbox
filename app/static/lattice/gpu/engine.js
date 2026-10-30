/* =============================================================================
 * 浏览器端晶格生成引擎（WebGPU）
 *
 * 分工：
 *   服务器 —— 只做**一次性**体素化（三角网格 → 有符号距离场），结果按分辨率缓存；
 *   浏览器 —— 每一次参数调整都在显卡上算：晶格隐式场 × 实体场 → 等值面提取。
 *
 * 为什么这样做：
 *   * 交互延迟从「网络往返 + 服务端排队 + 单核 numpy」变成「一次 GPU dispatch」，
 *     实测 160³ 体素在 RTX 4090 上约 15~25 ms（服务端同参数是 1~3 s）；
 *   * 服务端那台机器只有 1 GiB 内存、分辨率被迫压到 128~160，浏览器不受这个限制；
 *   * 等值面提取用 Surface Nets + Newton 投影（而不是 marching cubes + 抽稀），
 *     顶点会被推到真实零等值面上，毫米级小孔不会因为“体素阶梯 + 抽稀”而糊掉。
 *
 * 生成的网格为索引网格（positions/normals/indices），可直接给 three.js 用，
 * 也可以直接导出 STL / OBJ / PLY / 3MF（见 exporters.js）。
 * ========================================================================== */

'use strict';

/** 晶格类型 → 着色器里的编号（与 lattice.wgsl 的 kind 分支一致） */
export const KIND_INDEX = {
  gyroid: 0,
  schwarz_p: 1,
  diamond: 2,
  iwp: 3,
  sc: 4,
  grid: 5,
  bcc: 6,
  fcc: 7,
  octet: 8,
};

/** 应用方式 → 编号 */
export const MODE_INDEX = { intersect: 0, shell: 1, subtract: 2, union: 3 };

const UNIFORM_BYTES = 7 * 16; // 7 个 vec4
const FLOATS_PER_VERTEX = 6; // position.xyz + normal.xyz

function align4(n) {
  return (n + 3) & ~3;
}

export class GpuLatticeEngine {
  constructor(device, adapterInfo, limits) {
    this.device = device;
    this.adapterInfo = adapterInfo;
    this.limits = limits;
    this.module = null;
    this.pipelines = {};
    this.bindGroupLayout = null;
    this.uniform = null;
    this.solidBuffer = null;
    this.fieldBuffer = null;
    this.slotsBuffer = null;
    this.counters = null;
    this.grid = null; // { nx, ny, nz, cells }
    this.lastSolidMeta = null;
  }

  /* ---------------------------------------------------------------------- */
  /* 设备初始化                                                              */
  /* ---------------------------------------------------------------------- */

  static async isAvailable() {
    try {
      if (!navigator.gpu) return false;
      const adapter = await navigator.gpu.requestAdapter();
      return Boolean(adapter);
    } catch {
      return false;
    }
  }

  /** 创建引擎；不支持 WebGPU 时返回 null（调用方回退到服务端生成） */
  static async create() {
    if (!navigator.gpu) return null;
    let adapter = null;
    try {
      adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' });
      if (!adapter) adapter = await navigator.gpu.requestAdapter();
    } catch {
      return null;
    }
    if (!adapter) return null;

    const limits = adapter.limits || {};
    const device = await adapter.requestDevice({
      requiredLimits: {
        maxStorageBufferBindingSize: Math.min(limits.maxStorageBufferBindingSize || 1 << 28, 1 << 30),
        maxBufferSize: Math.min(limits.maxBufferSize || 1 << 28, 1 << 30),
      },
    });

    const info = adapter.info || (adapter.requestAdapterInfo ? await adapter.requestAdapterInfo() : {});
    const label = [info.vendor, info.architecture].filter(Boolean).join(' ') || 'WebGPU';
    return new GpuLatticeEngine(device, { ...info, label }, limits);
  }

  /** 适配器描述，用于界面角标 */
  get adapterLabel() {
    return this.adapterInfo?.label || 'WebGPU';
  }

  /** 当前设备大致能承受多少体素（按显存/缓冲区上限折算） */
  get maxVoxels() {
    const binding = Number(this.limits.maxStorageBufferBindingSize || 1 << 28);
    // solid + field + slots(+verts/indices) 大约 9~13 字节/体素，留 45% 余量
    return Math.floor((binding * 0.45) / 13);
  }

  async _ensurePipeline() {
    if (this.module) return;
    const res = await fetch('/static/lattice/gpu/lattice.wgsl', { cache: 'force-cache' });
    if (!res.ok) throw new Error(`着色器加载失败（${res.status}）`);
    const code = await res.text();

    const device = this.device;
    this.module = device.createShaderModule({ code, label: 'lattice' });

    const info = await this.module.getCompilationInfo?.();
    if (info && info.messages && info.messages.length) {
      const errs = info.messages.filter((m) => m.type === 'error');
      if (errs.length) {
        throw new Error('着色器编译失败：' + errs.map((m) => `${m.lineNum}:${m.linePos} ${m.message}`).join('; '));
      }
    }

    this.bindGroupLayout = device.createBindGroupLayout({
      entries: [
        { binding: 0, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'uniform' } },
        { binding: 1, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'read-only-storage' } },
        { binding: 2, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
        { binding: 3, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
        { binding: 4, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
        { binding: 5, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
        { binding: 6, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
      ],
    });

    const layout = device.createPipelineLayout({ bindGroupLayouts: [this.bindGroupLayout] });
    const make = (entryPoint) =>
      device.createComputePipeline({
        layout,
        compute: { module: this.module, entryPoint },
      });

    this.pipelines = {
      field: make('field_main'),
      count: make('count_main'),
      vert: make('vert_main'),
      index: make('index_main'),
    };

    this.uniform = device.createBuffer({
      size: UNIFORM_BYTES,
      usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
    });
    this.counters = device.createBuffer({
      size: 16,
      usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC | GPUBufferUsage.COPY_DST,
    });
    // 两个各自独立的占位缓冲：同一可写 storage buffer 不能同时绑到两个 binding
    const scratch = () =>
      device.createBuffer({ size: 16, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST });
    this._scratchA = scratch();
    this._scratchB = scratch();
  }

  /* ---------------------------------------------------------------------- */
  /* 实体体场（服务端一次性体素化的结果）                                     */
  /* ---------------------------------------------------------------------- */

  /**
   * 上传实体 SDF。
   * @param {{shape:number[], pitch:number, origin:number[]}} meta
   * @param {ArrayBuffer} buffer float32 原始字节（C 序，长度 = nx*ny*nz*4）
   */
  setSolid(meta, buffer) {
    const [nx, ny, nz] = meta.shape.map((v) => int(v));
    const count = nx * ny * nz;
    if (buffer.byteLength < count * 4) {
      throw new Error(`实体体场长度不符：期望 ${count * 4} 字节，收到 ${buffer.byteLength}`);
    }
    const device = this.device;

    this._destroySolid();
    this.solidBuffer = device.createBuffer({
      size: align4(count * 4),
      usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST | GPUBufferUsage.COPY_SRC,
      label: 'solid-sdf',
    });
    device.queue.writeBuffer(this.solidBuffer, 0, buffer, 0, count * 4);

    this.fieldBuffer = device.createBuffer({
      size: align4(count * 4),
      usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC,
      label: 'combined-field',
    });

    const cells = Math.max(1, (nx - 1) * (ny - 1) * (nz - 1));
    this.slotsBuffer = device.createBuffer({
      size: align4(cells * 4),
      usage: GPUBufferUsage.STORAGE, // WebGPU 保证新缓冲区已清零（0 = 该单元无顶点）
      label: 'cell-slots',
    });

    this.grid = { nx, ny, nz, cells, count };
    this.lastSolidMeta = { ...meta };
    return this.grid;
  }

  _destroySolid() {
    for (const buf of [this.solidBuffer, this.fieldBuffer, this.slotsBuffer]) {
      try {
        buf?.destroy?.();
      } catch {
        /* ignore */
      }
    }
    this.solidBuffer = null;
    this.fieldBuffer = null;
    this.slotsBuffer = null;
  }

  dispose() {
    this._destroySolid();
    try {
      this.counters?.destroy?.();
      this.uniform?.destroy?.();
    } catch {
      /* ignore */
    }
  }

  /* ---------------------------------------------------------------------- */
  /* 生成                                                                    */
  /* ---------------------------------------------------------------------- */

  /**
   * 生成晶格网格。
   * @param {object} p 参数（与后端 pipeline.DEFAULT_PARAMS 同名）
   * @returns {Promise<{positions:Float32Array, normals:Float32Array,
   *                    indices:Uint32Array, stats:object}>}
   */
  async generate(p) {
    await this._ensurePipeline();
    if (!this.grid) throw new Error('尚未上传实体体场');
    const { nx, ny, nz, cells } = this.grid;
    const device = this.device;
    const kind = KIND_INDEX[p.lattice] ?? 0;
    const mode = MODE_INDEX[p.mode] ?? 0;
    if (kind === undefined) throw new Error(`未知晶格类型 ${p.lattice}`);

    // 体素量上限保护（避免在集显上直接爆显存）
    if (this.grid.count > this.maxVoxels) {
      throw new Error(
        `分辨率过高：${this.grid.count.toLocaleString()} 个体素超出本机显卡能力，请降低分辨率或改用服务器生成`
      );
    }

    const cellZ = p.cell_size_z || p.cell_size;
    const bounds = this.lastSolidMeta?.bounds || null;
    const offset = this._latticeOffset(p, bounds, cellZ);

    this._writeUniform(p);
    const u32 = this._uniformScratch;

    const wg = (n) => Math.max(1, Math.ceil(n / 4));

    // —— 第一段：组合场 + 统计（只数个数，不写几何） ——
    device.queue.writeBuffer(this.counters, 0, new Uint32Array([0, 0, 0, 0]));
    let bindGroup = this._makeBindGroup(this._scratchA, this._scratchB);

    let enc = device.createCommandEncoder({ label: 'field+count' });
    let pass = enc.beginComputePass({ label: 'field' });
    pass.setPipeline(this.pipelines.field);
    pass.setBindGroup(0, bindGroup);
    pass.dispatchWorkgroups(wg(nx), wg(ny), wg(nz));
    pass.end();

    pass = enc.beginComputePass({ label: 'count' });
    pass.setPipeline(this.pipelines.count);
    pass.setBindGroup(0, bindGroup);
    pass.dispatchWorkgroups(wg(nx - 1), wg(ny - 1), wg(nz - 1));
    pass.end();

    let readCounters = device.createBuffer({
      size: 16,
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    });
    enc.copyBufferToBuffer(this.counters, 0, readCounters, 0, 16);
    device.queue.submit([enc.finish()]);

    await readCounters.mapAsync(GPUMapMode.READ);
    const counts = new Uint32Array(readCounters.getMappedRange().slice(0));
    readCounters.unmap();
    readCounters.destroy();

    const nVertsMax = counts[0];
    const nIndicesMax = counts[1];
    if (nVertsMax === 0 || nIndicesMax === 0) {
      throw new Error('体场里没有可提取的等值面（壁厚可能小于体素尺寸）');
    }

    // —— 按上界精确分配输出缓冲 ——
    const vertBytes = align4(nVertsMax * FLOATS_PER_VERTEX * 4);
    const indexBytes = align4(nIndicesMax * 4);
    const vertsBuffer = device.createBuffer({
      size: vertBytes,
      usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC,
      label: 'lattice-verts',
    });
    const indexBuffer = device.createBuffer({
      size: indexBytes,
      usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC,
      label: 'lattice-indices',
    });
    this._writeUniform(p, nVertsMax, nIndicesMax);
    device.queue.writeBuffer(this.counters, 0, new Uint32Array([0, 0, 0, 0]));

    bindGroup = this._makeBindGroup(vertsBuffer, indexBuffer);

    // —— 第二段：顶点 + 四边形索引 ——
    enc = device.createCommandEncoder({ label: 'mesh' });
    pass = enc.beginComputePass({ label: 'verts' });
    pass.setPipeline(this.pipelines.vert);
    pass.setBindGroup(0, bindGroup);
    pass.dispatchWorkgroups(wg(nx - 1), wg(ny - 1), wg(nz - 1));
    pass.end();

    pass = enc.beginComputePass({ label: 'indices' });
    pass.setPipeline(this.pipelines.index);
    pass.setBindGroup(0, bindGroup);
    pass.dispatchWorkgroups(wg(nx - 1), wg(ny - 1), wg(nz - 1));
    pass.end();

    const vertRead = device.createBuffer({
      size: vertBytes,
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    });
    const indexRead = device.createBuffer({
      size: indexBytes,
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    });
    readCounters = device.createBuffer({ size: 16, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    enc.copyBufferToBuffer(vertsBuffer, 0, vertRead, 0, vertBytes);
    enc.copyBufferToBuffer(indexBuffer, 0, indexRead, 0, indexBytes);
    enc.copyBufferToBuffer(this.counters, 0, readCounters, 0, 16);
    device.queue.submit([enc.finish()]);

    await Promise.all([
      vertRead.mapAsync(GPUMapMode.READ),
      indexRead.mapAsync(GPUMapMode.READ),
      readCounters.mapAsync(GPUMapMode.READ),
    ]);

    // 第二次统计才是真实写入量：pass 1 只是上界（个别退化单元会少写）
    const written = new Uint32Array(readCounters.getMappedRange().slice(0));
    const nVerts = Math.min(written[0], nVertsMax);
    const nIndices = Math.min(written[1] - (written[1] % 3), nIndicesMax);

    const interleaved = new Float32Array(vertRead.getMappedRange().slice(0, nVerts * FLOATS_PER_VERTEX * 4));
    const indices = new Uint32Array(indexRead.getMappedRange().slice(0, nIndices * 4));
    vertRead.unmap();
    indexRead.unmap();
    readCounters.unmap();
    vertRead.destroy();
    indexRead.destroy();
    readCounters.destroy();
    vertsBuffer.destroy();
    indexBuffer.destroy();

    const positions = new Float32Array(nVerts * 3);
    const normals = new Float32Array(nVerts * 3);
    for (let i = 0, j = 0; i < nVerts; i += 1, j += FLOATS_PER_VERTEX) {
      positions[i * 3] = interleaved[j];
      positions[i * 3 + 1] = interleaved[j + 1];
      positions[i * 3 + 2] = interleaved[j + 2];
      normals[i * 3] = interleaved[j + 3];
      normals[i * 3 + 1] = interleaved[j + 4];
      normals[i * 3 + 2] = interleaved[j + 5];
    }

    const stats = computeMeshStats(positions, indices);
    stats.vertices = nVerts;
    stats.faces = indices.length / 3;
    stats.voxel_shape = [nx, ny, nz];
    stats.voxel_pitch = this.lastSolidMeta.pitch;
    stats.engine = 'gpu';
    return { positions, normals, indices, stats };
  }

  /** 写 uniform（网格 dims / 单胞 / 场参数 / 上限） */
  _writeUniform(p, maxVerts = 0, maxIndices = 0) {
    const { nx, ny, nz } = this.grid;
    const u32 = this._uniformScratch || (this._uniformScratch = new Uint32Array(UNIFORM_BYTES / 4));
    const f32 = new Float32Array(u32.buffer);
    u32.fill(0);
    u32[0] = nx;
    u32[1] = ny;
    u32[2] = nz;
    u32[3] = 0;
    const cellZ = p.cell_size_z || p.cell_size;
    f32[4] = p.cell_size;
    f32[5] = p.cell_size;
    f32[6] = cellZ;
    f32[7] = p.thickness;
    const meta = this.lastSolidMeta;
    f32[8] = meta.origin[0];
    f32[9] = meta.origin[1];
    f32[10] = meta.origin[2];
    f32[11] = meta.pitch;
    const offset = this._latticeOffset(p, meta.bounds, cellZ);
    f32[12] = offset[0];
    f32[13] = offset[1];
    f32[14] = offset[2];
    f32[15] = p.warp || 0;
    f32[16] = p.grad || 0;
    f32[17] = p.grad_height || 0;
    f32[18] = p.shell_thickness || 2.5;
    f32[19] = p.skin_thickness || 1.2;
    u32[20] = KIND_INDEX[p.lattice] ?? 0;
    u32[21] = MODE_INDEX[p.mode] ?? 0;
    u32[22] = p.wall_mode === 'solid' ? 1 : 0;
    u32[23] = p.keep_skin ? 1 : 0;
    u32[24] = maxVerts;
    u32[25] = maxIndices;
    u32[26] = 0;
    u32[27] = 0;
    this.device.queue.writeBuffer(this.uniform, 0, u32);
    this._uniformDirty = false;
    return u32;
  }

  /** 调试用：把组合场整块读回（小体场才用，大网格会很慢） */
  async debugFieldRead() {
    await this._ensurePipeline();
    const { count } = this.grid;
    const bytes = align4(count * 4);
    const read = this.device.createBuffer({
      size: bytes,
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    });
    const enc = this.device.createCommandEncoder();
    enc.copyBufferToBuffer(this.fieldBuffer, 0, read, 0, bytes);
    this.device.queue.submit([enc.finish()]);
    await read.mapAsync(GPUMapMode.READ);
    const out = new Float32Array(read.getMappedRange().slice(0, count * 4));
    read.unmap();
    read.destroy();
    return out;
  }

  /** 调试用：读回实体体场前 n 个值 */
  async debugSolidRead(n = 8) {
    const meaning = 4 * n;
    const read = this.device.createBuffer({
      size: align4(meaning),
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    });
    const enc = this.device.createCommandEncoder();
    enc.copyBufferToBuffer(this.solidBuffer, 0, read, 0, align4(meaning));
    this.device.queue.submit([enc.finish()]);
    await read.mapAsync(GPUMapMode.READ);
    const out = Array.from(new Float32Array(read.getMappedRange().slice(0, meaning)));
    read.unmap();
    read.destroy();
    return out;
  }

  /** 调试用：只跑第一段（组合场 + 统计），返回计数 */
  async debugCounts(params = {}) {
    await this._ensurePipeline();
    this._writeUniform({
      lattice: 'gyroid', cell_size: 200, cell_size_z: 200, thickness: 400, wall_mode: 'sheet',
      mode: 'intersect', shell_thickness: 2.5, skin_thickness: 1.2, keep_skin: false,
      warp: 0, grad: 0, align: 'min', ...params,
    });
    this.device.pushErrorScope('validation');
    const { nx, ny, nz } = this.grid;
    this.device.queue.writeBuffer(this.counters, 0, new Uint32Array([0, 0, 0, 0]));
    const bg = this._makeBindGroup(this._scratchA, this._scratchB);
    const wg = (n) => Math.max(1, Math.ceil(n / 4));
    const enc = this.device.createCommandEncoder();
    let pass = enc.beginComputePass();
    pass.setPipeline(this.pipelines.field);
    pass.setBindGroup(0, bg);
    pass.dispatchWorkgroups(wg(nx), wg(ny), wg(nz));
    pass.end();
    pass = enc.beginComputePass();
    pass.setPipeline(this.pipelines.count);
    pass.setBindGroup(0, bg);
    pass.dispatchWorkgroups(wg(nx - 1), wg(ny - 1), wg(nz - 1));
    pass.end();
    const read = this.device.createBuffer({ size: 16, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    enc.copyBufferToBuffer(this.counters, 0, read, 0, 16);
    this.device.queue.submit([enc.finish()]);
    await read.mapAsync(GPUMapMode.READ);
    const arr = new Uint32Array(read.getMappedRange().slice(0));
    read.unmap();
    read.destroy();
    const err = await this.device.popErrorScope();
    if (err) throw new Error('WebGPU 校验错误：' + err.message);
    return Array.from(arr);
  }

  /* ---------------------------------------------------------------------- */
  /* 内部                                                                    */
  /* ---------------------------------------------------------------------- */

  _makeBindGroup(vertsBuffer, indexBuffer) {
    const verts = vertsBuffer || this._scratchA;
    const idx = indexBuffer || this._scratchB;
    return this.device.createBindGroup({
      layout: this.bindGroupLayout,
      entries: [
        { binding: 0, resource: { buffer: this.uniform } },
        { binding: 1, resource: { buffer: this.solidBuffer } },
        { binding: 2, resource: { buffer: this.fieldBuffer } },
        { binding: 3, resource: { buffer: verts } },
        { binding: 4, resource: { buffer: this.slotsBuffer } },
        { binding: 5, resource: { buffer: idx } },
        { binding: 6, resource: { buffer: this.counters } },
      ],
    });
  }

  /** 与后端 pipeline.build 的单胞对齐规则一致（min / center） */
  _latticeOffset(p, bounds, cellZ) {
    if (!bounds) return [0, 0, 0];
    const lo = bounds[0];
    const hi = bounds[1];
    if (p.align === 'center') {
      const cx = (lo[0] + hi[0]) / 2;
      const cy = (lo[1] + hi[1]) / 2;
      const cz = (lo[2] + hi[2]) / 2;
      return [cx - 0.5 * p.cell_size, cy - 0.5 * p.cell_size, cz - 0.5 * cellZ];
    }
    return [lo[0], lo[1], lo[2]];
  }
}

function int(v) {
  return Math.max(1, Math.round(Number(v) || 0));
}

/** 三角网格的表面积 / 体积（体积用散度定理，要求绕序一致且朝外） */
export function computeMeshStats(positions, indices) {
  let area = 0;
  let volume = 0;
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;

  for (let t = 0; t < indices.length; t += 3) {
    const a = indices[t] * 3;
    const b = indices[t + 1] * 3;
    const c = indices[t + 2] * 3;
    const ax = positions[a];
    const ay = positions[a + 1];
    const az = positions[a + 2];
    const bx = positions[b];
    const by = positions[b + 1];
    const bz = positions[b + 2];
    const cx = positions[c];
    const cy = positions[c + 1];
    const cz = positions[c + 2];

    const ux = bx - ax;
    const uy = by - ay;
    const uz = bz - az;
    const vx = cx - ax;
    const vy = cy - ay;
    const vz = cz - az;
    const nx = uy * vz - uz * vy;
    const ny = uz * vx - ux * vz;
    const nz = ux * vy - uy * vx;
    area += 0.5 * Math.hypot(nx, ny, nz);
    // 有向体积：六面体 (0,a,b,c) 的 1/6
    volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6;

    minX = Math.min(minX, ax, bx, cx);
    minY = Math.min(minY, ay, by, cy);
    minZ = Math.min(minZ, az, bz, cz);
    maxX = Math.max(maxX, ax, bx, cx);
    maxY = Math.max(maxY, ay, by, cy);
    maxZ = Math.max(maxZ, az, bz, cz);
  }

  return {
    area_mm2: area,
    volume_mm3: Math.abs(volume),
    signed_volume_mm3: volume,
    extents: Number.isFinite(minX) ? [maxX - minX, maxY - minY, maxZ - minZ] : [0, 0, 0],
  };
}
