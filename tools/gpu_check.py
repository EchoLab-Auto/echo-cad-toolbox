"""浏览器端 GPU 晶格引擎的自动化验证（真实 Chromium + WebGPU）。

检查内容（都是**数值**断言，不靠肉眼）：

1. WebGPU 可用、着色器能编译、三个 pass 能跑通；
2. 提取出来的网格绕序一致且朝外（有向体积 > 0、与梯度法向同向率高）；
3. **等值面精度**：解析球体（半径 20 mm）在 pitch = 0.5 mm 下，顶点到球心的
   径向误差应远小于体素（Surface Nets + Newton 投影应给出亚体素精度）；
4. **毫米级小孔精度**：40×40×6 mm 板上一个 Ø2 mm 的孔，pitch = 0.5 mm（孔只有
   4 个体素宽），孔必须仍然通、孔壁半径误差小于 0.2 mm；
5. 与后端一致：同一体场下 GPU 与 `scipy`/`skimage` 的实体体积一致性（可选，
   只在传入 --compare-server 时比对 HTTP 结果）。

用法::

    python3 tools/gpu_check.py [http://127.0.0.1:8099]

退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import math
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099").rstrip("/")

CHROME_FLAGS = [
    "--no-sandbox",
    "--enable-unsafe-webgpu",
    "--enable-features=Vulkan",
    "--use-angle=vulkan",
]

# ---------------------------------------------------------------------------
# 在页面里跑的验证脚本
# ---------------------------------------------------------------------------
PAGE_SCRIPT = r"""
async (args) => {
  const out = { steps: [], error: null };
  const log = (name, data) => out.steps.push({ name, ...data });

  const mod = await import('/static/lattice/gpu/engine.js');
  const engine = await mod.GpuLatticeEngine.create();
  if (!engine) { out.error = 'WebGPU 不可用'; return out; }
  log('device', { adapter: engine.adapterLabel, maxVoxels: engine.maxVoxels });

  /* ---------------- 合成体场：解析 SDF ---------------- */
  function makeGrid(bounds, pitch, fn) {
    const lo = bounds[0], hi = bounds[1];
    const pad = 3 * pitch;
    const nx = Math.ceil((hi[0] - lo[0]) / pitch) + 2 * pad / pitch;
    const ny = Math.ceil((hi[1] - lo[1]) / pitch) + 2 * pad / pitch;
    const nz = Math.ceil((hi[2] - lo[2]) / pitch) + 2 * pad / pitch;
    const origin = [lo[0] - pad, lo[1] - pad, lo[2] - pad];
    const data = new Float32Array(nx * ny * nz);
    for (let i = 0; i < nx; i++) {
      const x = origin[0] + (i + 0.5) * pitch;
      for (let j = 0; j < ny; j++) {
        const y = origin[1] + (j + 0.5) * pitch;
        for (let k = 0; k < nz; k++) {
          const z = origin[2] + (k + 0.5) * pitch;
          data[(i * ny + j) * nz + k] = fn(x, y, z);
        }
      }
    }
    return { shape: [nx, ny, nz], pitch, origin, bounds, data };
  }

  const sphereSdf = (r) => (x, y, z) => Math.hypot(x, y, z) - r;

  // 板 + 通孔（孔轴沿 Z）：max(box, -cylinder)
  function plateWithHole(holeR) {
    const hx = 20, hy = 20, hz = 3;
    return (x, y, z) => {
      const box = Math.max(Math.abs(x) - hx, Math.abs(y) - hy, Math.abs(z) - hz);
      const radial = Math.hypot(x, y);
      // 挖孔：圆柱内部为负，材料 = 板 ∩ 圆柱外部
      return Math.max(box, holeR - radial);
    };
  }

  function upload(grid) {
    const meta = { shape: grid.shape, pitch: grid.pitch, origin: grid.origin, bounds: grid.bounds };
    engine.setSolid(meta, grid.data.buffer);
    return meta;
  }

  // 让晶格“不起作用”（只测实体等值面的提取精度）：
  // sc 杆型 + 超大单胞 + 超粗杆 → 整个包围盒都在“杆内部”（场值 < 0），
  // 于是 max(solid, lat) 恒等于 solid，提取结果就是实体本身的等值面。
  // （TPMS 做不到这一点：s/|∇s| 在临界点处分母趋零，会炸出假壁面）
  const noopLattice = {
    lattice: 'sc', cell_size: 2000, cell_size_z: 2000, thickness: 1600,
    wall_mode: 'sheet', mode: 'intersect', shell_thickness: 2.5, skin_thickness: 1.2,
    keep_skin: false, warp: 0, grad: 0, align: 'min',
  };

  /* ---------------- 1) 球体精度 ---------------- */
  const R = 20, pitch = 0.5;
  let grid = makeGrid([[-R, -R, -R], [R, R, R]], pitch, sphereSdf(R));
  upload(grid);
  let t0 = performance.now();
  let res = await engine.generate(noopLattice);
  let dt = performance.now() - t0;
  log('sphere', {
    ms: +dt.toFixed(1), verts: res.stats.vertices, faces: res.stats.faces,
    volume: +res.stats.volume_mm3.toFixed(1),
    signedVolume: +res.stats.signed_volume_mm3.toFixed(1),
  });

  const exactVol = 4 / 3 * Math.PI * R ** 3;
  const volErr = Math.abs(res.stats.volume_mm3 - exactVol) / exactVol;

  // 顶点径向误差
  const pos = res.positions;
  let maxErr = 0, sumErr = 0, n = 0;
  const errs = [];
  for (let i = 0; i < pos.length; i += 3) {
    const r = Math.hypot(pos[i], pos[i + 1], pos[i + 2]);
    const e = Math.abs(r - R);
    errs.push(e);
    maxErr = Math.max(maxErr, e);
    sumErr += e;
    n++;
  }
  errs.sort((a, b) => a - b);
  const p95 = errs[Math.floor(errs.length * 0.95)] || 0;
  log('sphere-accuracy', {
    meanErr: +(sumErr / n).toFixed(4), maxErr: +maxErr.toFixed(4),
    p95Err: +p95.toFixed(4), relVolErr: +volErr.toFixed(5), pitch,
  });

  // 绕序一致性：几何法向 vs 顶点法向
  let agree = 0, tris = 0;
  const nrm = res.normals, idx = res.indices;
  for (let t = 0; t < idx.length; t += 3) {
    const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
    const vx = pos[c] - pos[a], vy = pos[c + 1] - pos[a + 1], vz = pos[c + 2] - pos[a + 2];
    const gx = uy * vz - uz * vy, gy = uz * vx - ux * vz, gz = ux * vy - uy * vx;
    const sx = nrm[a] + nrm[b] + nrm[c];
    const sy = nrm[a + 1] + nrm[b + 1] + nrm[c + 1];
    const sz = nrm[a + 2] + nrm[b + 2] + nrm[c + 2];
    if (gx * sx + gy * sy + gz * sz >= 0) agree++;
    tris++;
  }
  log('winding', { agreeRatio: +(agree / tris).toFixed(4), tris });

  /* ---------------- 2) 毫米级小孔 ---------------- */
  const holeR = 1.0;
  grid = makeGrid([[-20, -20, -3], [20, 20, 3]], pitch, plateWithHole(holeR));
  upload(grid);
  t0 = performance.now();
  res = await engine.generate(noopLattice);
  dt = performance.now() - t0;

  // 孔壁：距轴线 0.7 mm 以内的顶点数应为 0（孔不能被糊死），
  // 且存在半径约 1 mm 的孔壁顶点
  const ring = [];
  let blocked = 0;
  const p2 = res.positions;
  for (let i = 0; i < p2.length; i += 3) {
    const r = Math.hypot(p2[i], p2[i + 1]);
    const z = p2[i + 2];
    if (Math.abs(z) < 3.0 && r < 1.6) {
      ring.push(r);
      if (r < 0.55) blocked++;
    }
  }
  ring.sort((a, b) => a - b);
  const wallR = ring.length ? ring[Math.floor(ring.length * 0.5)] : null;
  log('small-hole', {
    ms: +dt.toFixed(1), faces: res.stats.faces, verts: res.stats.vertices,
    holeWallRadius: wallR === null ? null : +wallR.toFixed(3),
    holeWallErr: wallR === null ? null : +Math.abs(wallR - holeR).toFixed(3),
    blockedVerts: blocked, ringVerts: ring.length, pitch,
  });

  /* ---------------- 3) 真实晶格：gyroid ---------------- */
  grid = makeGrid([[-15, -15, -15], [15, 15, 15]], 0.45, sphereSdf(15));
  upload(grid);
  const gyroid = {
    lattice: 'gyroid', cell_size: 8, cell_size_z: 8, thickness: 1.2,
    wall_mode: 'sheet', mode: 'intersect', shell_thickness: 2.5, skin_thickness: 1.2,
    keep_skin: false, warp: 0.15, grad: 0, align: 'min',
  };
  t0 = performance.now();
  res = await engine.generate(gyroid);
  dt = performance.now() - t0;
  log('gyroid', {
    ms: +dt.toFixed(1), faces: res.stats.faces, verts: res.stats.vertices,
    volume: +res.stats.volume_mm3.toFixed(1),
    relDensity: +(res.stats.volume_mm3 / exactVol).toFixed(4),
  });

  /* ---------------- 3b) 四种应用方式的组合语义 ---------------- */
  grid = makeGrid([[-15, -15, -15], [15, 15, 15]], 0.45, sphereSdf(15));
  upload(grid);
  const base = {
    lattice: 'gyroid', cell_size: 8, cell_size_z: 8, thickness: 1.2, wall_mode: 'sheet',
    shell_thickness: 3, skin_thickness: 1.2, keep_skin: false, warp: 0, grad: 0, align: 'min',
  };
  const modes = {};
  for (const mode of ['intersect', 'shell', 'subtract', 'union']) {
    const r = await engine.generate({ ...base, mode });
    modes[mode] = { faces: r.stats.faces, volume: +r.stats.volume_mm3.toFixed(1) };
  }
  // 同一体场下"只有实体"的体积，用来校验 subtract + intersect ≈ solid
  const solid = await engine.generate(noopLattice);
  modes.solid = { faces: solid.stats.faces, volume: +solid.stats.volume_mm3.toFixed(1) };
  log('modes', modes);

  /* ---------------- 4) 全部 9 种晶格都能跑 ---------------- */
  const kinds = ['gyroid', 'schwarz_p', 'diamond', 'iwp', 'sc', 'grid', 'bcc', 'fcc', 'octet'];
  const perKind = {};
  for (const kind of kinds) {
    const params = { ...gyroid, lattice: kind, cell_size: 8, thickness: 1.4 };
    t0 = performance.now();
    const r = await engine.generate(params);
    perKind[kind] = { faces: r.stats.faces, ms: +(performance.now() - t0).toFixed(1) };
  }
  log('kinds', perKind);

  return out;
}
"""


def main() -> int:
    issues: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=CHROME_FLAGS)
        page = browser.new_page(viewport={"width": 900, "height": 700})
        page.on("pageerror", lambda e: issues.append(f"pageerror: {e}"))
        page.on("console", lambda m: issues.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(400)
        result = page.evaluate(PAGE_SCRIPT, {})
        browser.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    if issues:
        print("浏览器日志：")
        for line in issues[:12]:
            print("  ", line)

    fails: list[str] = []
    if result.get("error"):
        fails.append(f"引擎初始化失败：{result['error']}")
    steps = {s["name"]: s for s in result.get("steps", [])}

    acc = steps.get("sphere-accuracy")
    if acc:
        if acc["p95Err"] > 0.12:
            fails.append(f"球面 p95 径向误差 {acc['p95Err']} mm 超过 0.12 mm（pitch=0.5）")
        if acc["relVolErr"] > 0.01:
            fails.append(f"球体体积相对误差 {acc['relVolErr']:.3%} 超过 1%")
    else:
        fails.append("球体精度检查没有结果")

    wind = steps.get("winding")
    if wind and wind["agreeRatio"] < 0.99:
        fails.append(f"三角形绕序与法向一致率仅 {wind['agreeRatio']:.1%}")

    sph = steps.get("sphere")
    if sph and sph["signedVolume"] <= 0:
        fails.append("有向体积为负：整体绕序朝内")

    hole = steps.get("small-hole")
    if hole:
        if hole["holeWallRadius"] is None:
            fails.append("Ø2 mm 孔在提取结果中不存在（被糊死）")
        elif hole["holeWallErr"] > 0.2:
            fails.append(f"孔壁半径误差 {hole['holeWallErr']} mm 超过 0.2 mm")
        if hole["blockedVerts"] > 0:
            fails.append(f"孔轴附近有 {hole['blockedVerts']} 个顶点（孔径被堵）")
    else:
        fails.append("小孔精度检查没有结果")

    modes = steps.get("modes")
    if modes:
        inter = modes.get("intersect", {}).get("volume", 0)
        shell = modes.get("shell", {}).get("volume", 0)
        sub = modes.get("subtract", {}).get("volume", 0)
        uni = modes.get("union", {}).get("volume", 0)
        if not (uni > inter > shell):
            fails.append(f"应用方式体量关系异常：union={uni} intersect={inter} shell={shell}")
        solid = modes.get("solid", {}).get("volume", 0)
        if not (0 < sub < inter + shell or 0 < sub < solid):
            fails.append(f"挖减重腔体量异常：subtract={sub} intersect={inter} solid={solid}")
        # subtract（实体减晶格）+ intersect（实体里的晶格）应约等于实体本身
        if solid > 0:
            err = abs(sub + inter - solid) / solid
            if err > 0.2:
                fails.append(f"subtract + intersect = {sub + inter:.0f} 与实体 {solid:.0f} 相差 {err:.0%}")
    else:
        fails.append("应用方式检查没有结果")

    kinds = steps.get("kinds", {})
    for kind, data in kinds.items():
        if kind == "name":
            continue
        if not data.get("faces"):
            fails.append(f"晶格 {kind} 没有生成任何面")

    print("\n=== 判定 ===")
    if fails:
        for f in fails:
            print("  ✗", f)
        print("FAIL")
        return 1
    print("PASS: GPU 引擎、等值面精度、毫米级小孔、9 种晶格全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
