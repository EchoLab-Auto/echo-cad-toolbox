"""几何文件载入与导出（晶格化工具的 IO 层）。

支持：
* 网格格式：STL / OBJ / PLY / OFF / 3MF / GLB / GLTF（trimesh 直接读取）
* CAD 格式：STEP / STP / IGES（用 gmsh 的 OCC 内核在**子进程**里曲面离散化，
  避免 OCC 崩溃影响 Web 服务）

载入后统一做：合并、去重、法向修复、闭合修补，输出单一 ``trimesh.Trimesh``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import trimesh

STEP_EXTS = {".step", ".stp", ".iges", ".igs"}
MESH_EXTS = {".stl", ".obj", ".ply", ".off", ".3mf", ".glb", ".gltf"}
SUPPORTED_EXTS = STEP_EXTS | MESH_EXTS
EXPORT_FORMATS = {"stl": ".stl", "obj": ".obj", "ply": ".ply", "3mf": ".3mf"}


class GeometryError(RuntimeError):
    """几何处理失败（对用户可见的错误信息）。"""


# --------------------------------------------------------------------------- #
# STEP / IGES → 三角网格（gmsh，子进程隔离）
# --------------------------------------------------------------------------- #
_GMSH_SCRIPT = textwrap.dedent(
    """
    import json, sys
    import gmsh

    src, dst, max_edge, angle = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setNumber("General.Verbosity", 0)
    gmsh.option.setNumber("Geometry.OCCImportLabels", 0)
    gmsh.model.add("cad")
    try:
        gmsh.model.occ.importShapes(src)
        gmsh.model.occ.synchronize()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": "OCC 导入失败: %s" % exc}))
        sys.exit(0)

    xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
    diag = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5
    if max_edge <= 0:
        max_edge = max(diag / 160.0, 1e-3)

    gmsh.option.setNumber("Mesh.MeshSizeMax", max_edge)
    gmsh.option.setNumber("Mesh.MeshSizeMin", max_edge * 0.2)
    gmsh.option.setNumber("Mesh.CharacteristicLengthFromCurvature", 0)
    gmsh.option.setNumber("Mesh.Smoothing", 5)
    gmsh.option.setNumber("Mesh.HighOrderOptimize", 0)
    gmsh.option.setNumber("Mesh.Binary", 1)
    gmsh.option.setNumber("Mesh.Algorithm", 6)          # Frontal-Delaunay (2D)
    gmsh.option.setNumber("Geometry.Tolerance", 1e-8)

    # 先做面网格（足够做体素化）；失败时退回 OCC 曲面直接离散
    try:
        gmsh.model.mesh.generate(2)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": "曲面网格化失败: %s" % exc}))
        sys.exit(0)

    gmsh.write(dst)
    n2 = len(gmsh.model.mesh.getElements(2)[1])
    info = {
        "ok": True,
        "bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
        "max_edge": max_edge,
        "surfaces": n2,
        "version": gmsh.__version__ if hasattr(gmsh, "__version__") else "",
    }
    gmsh.finalize()
    print(json.dumps(info))
    """
).strip()


def _step_to_mesh(path: Path, max_edge: float | None = None, timeout: int = 240) -> trimesh.Trimesh:
    """调用子进程用 gmsh 把 STEP/IGES 离散成 STL，再读入。"""
    with tempfile.TemporaryDirectory(prefix="lattice_step_") as tmp:
        dst = os.path.join(tmp, "tess.stl")
        cmd = [
            sys.executable, "-c", _GMSH_SCRIPT, str(path), dst,
            str(max_edge or 0.0), "0.35",
        ]
        env = dict(os.environ)
        libs = str(Path(__file__).resolve().parent.parent / ".pylibs")
        env["PYTHONPATH"] = libs + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("OMP_NUM_THREADS", "4")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise GeometryError(f"STEP 离散超时（>{timeout}s），请先简化模型或降低尺寸") from exc

        meta: dict = {}
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    meta = json.loads(line)
                except json.JSONDecodeError:
                    continue
                break
        if not meta.get("ok"):
            err = meta.get("error") or (proc.stderr or "").strip().splitlines()[-3:]
            raise GeometryError(f"STEP 解析失败：{err}")
        if not os.path.exists(dst) or os.path.getsize(dst) < 128:
            raise GeometryError("STEP 离散没有产生有效网格，请检查文件是否为实体（solid）")

        mesh = _load_trimesh_scene(Path(dst))
        mesh.metadata["cad"] = {
            "max_edge": meta.get("max_edge"),
            "surfaces": meta.get("surfaces"),
            "bbox": meta.get("bbox"),
        }
        return mesh


# --------------------------------------------------------------------------- #
# 网格载入
# --------------------------------------------------------------------------- #
def _load_trimesh_scene(path: Path) -> trimesh.Trimesh:
    try:
        obj = trimesh.load(str(path), force="mesh", process=False)
    except Exception as exc:
        raise GeometryError(f"网格读取失败：{exc}") from exc
    if isinstance(obj, trimesh.Scene):
        geoms = [g for g in obj.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise GeometryError("文件中没有三角网格")
        obj = trimesh.util.concatenate(geoms)
    if not isinstance(obj, trimesh.Trimesh) or len(obj.faces) == 0:
        raise GeometryError("文件中没有三角网格")
    return obj


def load_mesh(path: str | Path, max_edge: float | None = None) -> tuple[trimesh.Trimesh, dict]:
    """载入几何文件并做修复，返回 (mesh, info)。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise GeometryError(f"不支持的格式 {ext}，可用：{', '.join(sorted(SUPPORTED_EXTS))}")

    info: dict = {"format": ext.lstrip("."), "tessellated_from_cad": ext in STEP_EXTS}
    if ext in STEP_EXTS:
        mesh = _step_to_mesh(path, max_edge=max_edge)
        info.update(mesh.metadata.get("cad", {}))
        info["tessellated_from_cad"] = True
    else:
        mesh = _load_trimesh_scene(path)

    mesh = repair_mesh(mesh)
    info.update(describe_mesh(mesh))
    return mesh, info


def repair_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """常用修复：去重复面/顶点、统一法向、补洞、去除退化面。"""
    mesh = mesh.copy()
    try:
        mesh.remove_infinite_values()
    except Exception:  # noqa: BLE001
        pass
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()

    # 尝试补洞（对非闭合网格）
    if not mesh.is_watertight:
        try:
            trimesh.repair.fill_holes(mesh)
        except Exception:  # noqa: BLE001
            pass
    try:
        trimesh.repair.fix_normals(mesh, multibody=True)
    except Exception:  # noqa: BLE001
        try:
            trimesh.repair.fix_winding(mesh)
        except Exception:  # noqa: BLE001
            pass
    # 体积为负说明整体法向朝内
    try:
        if mesh.is_watertight and mesh.volume < 0:
            mesh.invert()
    except Exception:  # noqa: BLE001
        pass
    return mesh


def describe_mesh(mesh: trimesh.Trimesh) -> dict:
    extents = mesh.extents
    return {
        "vertices": len(mesh.vertices),
        "faces": len(mesh.faces),
        "watertight": bool(mesh.is_watertight),
        "volume": float(mesh.volume) if mesh.is_watertight else None,
        "area": float(mesh.area),
        "extents": [float(v) for v in extents],
        "bounds": [[float(v) for v in row] for row in mesh.bounds],
    }


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export_mesh(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    path = Path(path)
    ext = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if ext == ".stl":
            mesh.export(str(path), file_type="stl")
        elif ext == ".obj":
            mesh.export(str(path), file_type="obj")
        elif ext == ".ply":
            mesh.export(str(path), file_type="ply", encoding="binary")
        elif ext == ".3mf":
            mesh.export(str(path), file_type="3mf")
        else:
            raise GeometryError(f"不支持的导出格式：{ext}")
    except GeometryError:
        raise
    except Exception as exc:
        raise GeometryError(f"导出 {ext} 失败：{exc}") from exc
    return path


def preview_glb(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """导出 GLB 用于前端预览（比 STL 小且带法向，three.js 直接加载）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(geometry={"part": mesh})
    path.write_bytes(scene.export(file_type="glb"))
    return path
