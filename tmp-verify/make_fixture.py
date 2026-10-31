"""客户端体素化的交叉验证：同一网格，Python（服务端）与 JS（浏览器）各跑一次。

用法（在容器里，仓库挂到 /work）：

    python tmp-verify/make_fixture.py box      # 生成夹具 + 服务端 SDF
    node  tmp-verify/run_voxelize.mjs box      # 浏览器实现跑同一网格
    python tmp-verify/compare.py box           # 逐体素比对

三种夹具：box（解析解）、torus（曲率大）、cycloid（薄壁 + 小孔，真实零件）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, "/work")

from app.lattice import voxel  # noqa: E402

OUT = Path("/tmp/fix")
OUT.mkdir(parents=True, exist_ok=True)

RES = {"box": 96, "torus": 128, "cycloid": 128}


def build(name: str) -> trimesh.Trimesh:
    if name == "box":
        return trimesh.creation.box(extents=(40.0, 30.0, 24.0))
    if name == "torus":
        return trimesh.creation.torus(major_radius=25.0, minor_radius=8.0, major_segments=96,
                                      minor_segments=48)
    if name == "cycloid":
        from app.geometry import CycloidParams
        from app.lattice.cycloid import build_disc_mesh
        mesh, _info = build_disc_mesh(CycloidParams(), width=14.0)
        return mesh
    raise SystemExit(f"未知夹具 {name}")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "box"
    res = RES[name]
    mesh = build(name)

    np.ascontiguousarray(np.asarray(mesh.vertices, dtype=np.float32)).tofile(OUT / f"{name}_pos.bin")
    np.ascontiguousarray(np.asarray(mesh.faces, dtype=np.uint32)).tofile(OUT / f"{name}_idx.bin")
    (OUT / f"{name}_mesh.json").write_text(json.dumps({
        "name": name, "vertices": len(mesh.vertices), "faces": len(mesh.faces),
        "resolution": res, "watertight": bool(mesh.is_watertight),
    }, ensure_ascii=False))

    vol = voxel.mesh_to_volume(mesh, resolution=res)
    np.ascontiguousarray(vol["sdf"], dtype=np.float32).tofile(OUT / f"{name}_srv_sdf.bin")
    (OUT / f"{name}_srv_meta.json").write_text(json.dumps({
        "shape": list(vol["sdf"].shape),
        "pitch": float(vol["pitch"]),
        "origin": [float(v) for v in np.asarray(vol["origin"]).ravel()[:3]],
        "bounds": [[float(v) for v in row] for row in np.asarray(vol["bounds"])],
        "fill_ratio": float(vol["fill_ratio"]),
    }, ensure_ascii=False, indent=1))

    print(f"[{name}] {len(mesh.vertices)} 顶点 / {len(mesh.faces)} 面  res={res}")
    print(f"        服务端 SDF shape={vol['sdf'].shape} pitch={vol['pitch']:.4f} mm "
          f"fill={vol['fill_ratio']:.4f}")


if __name__ == "__main__":
    main()
