"""晶格化工具（app.lattice）测试。

覆盖：摆线轮盘实体生成、体素化精度、9 种晶格、4 种应用方式、STEP 链路、
导出格式、参数裁剪，以及 ``/api/lattice`` 的 HTTP 接口。
"""

from __future__ import annotations

import io
import os
import tempfile
import time

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from app.geometry import CycloidParams
from app.lattice import meshio, pipeline, resources, voxel
from app.lattice.cycloid import build_disc_mesh
from app.main import app

client = TestClient(app)

LATTICE_KINDS = ["gyroid", "schwarz_p", "diamond", "iwp", "sc", "grid", "bcc", "fcc", "octet"]

_BOX = trimesh.creation.box(extents=(40.0, 30.0, 24.0))
_VOLUME_CACHE: dict[int, dict] = {}


def box_volume(resolution: int = 96) -> dict:
    if resolution not in _VOLUME_CACHE:
        _VOLUME_CACHE[resolution] = voxel.mesh_to_volume(_BOX, resolution=resolution)
    return _VOLUME_CACHE[resolution]


def wait_for_job(job_id: str, wanted: tuple[str, ...], timeout: float = 120.0) -> dict:
    """轮询作业直到进入 ``wanted`` 状态（生成是后台线程，必须留时间）。"""
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/api/lattice/jobs/{job_id}").json()
        if state.get("status") in wanted or state.get("status") == "error":
            return state
        time.sleep(0.25)
    raise AssertionError(f"作业等待超时：{state}")


def open_edges(mesh: trimesh.Trimesh) -> int:
    _uniq, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return int((counts == 1).sum())


# ---------------------------------------------------------------------------
# 几何链路
# ---------------------------------------------------------------------------
def test_voxel_volume_accuracy() -> None:
    """立方体体素化的体积误差应 < 1%。"""
    vol = box_volume(112)
    volume = float((vol["sdf"] <= 0).sum()) * vol["pitch"] ** 3
    expected = float(np.prod(_BOX.extents))
    assert abs(volume - expected) / expected < 0.01


@pytest.mark.parametrize("kind", LATTICE_KINDS)
def test_lattice_kinds_generate(kind: str) -> None:
    """每种晶格都能生成闭合、单连通的网格。"""
    vol = box_volume(96)
    result = pipeline.build(vol, {"lattice": kind, "cell_size": 8.0, "thickness": 1.0, "resolution": 96})
    assert open_edges(result.mesh) == 0, f"{kind} 网格有开口"
    assert len(result.mesh.split(only_watertight=False)) == 1, f"{kind} 不连通"
    assert 0.005 < result.stats["relative_density"] < 0.95


@pytest.mark.parametrize("mode", ["intersect", "shell", "subtract", "union"])
def test_apply_modes(mode: str) -> None:
    """四种应用方式都闭合（union 会顶满包围盒，专门验边界保护）。"""
    vol = box_volume(96)
    result = pipeline.build(
        vol, {"lattice": "gyroid", "cell_size": 9.0, "thickness": 1.1, "mode": mode, "resolution": 96}
    )
    assert open_edges(result.mesh) == 0, f"{mode} 网格有开口"


def test_warp_and_grad_stay_connected() -> None:
    """域扭曲 + Z 向渐变不能把场在分块边界处切断。"""
    vol = box_volume(96)
    result = pipeline.build(
        vol,
        {"lattice": "gyroid", "cell_size": 9.0, "thickness": 1.1, "warp": 0.25, "grad": 0.3, "resolution": 96},
    )
    assert open_edges(result.mesh) == 0
    assert len(result.mesh.split(only_watertight=False)) == 1


def test_memory_ceiling_is_detected() -> None:
    """分辨率上限必须能被探测出来，且落在参数允许范围内。"""
    ceiling, limit = resources.max_resolution()
    assert 48 <= ceiling <= resources.MAX_RESOLUTION_HARD_LIMIT
    assert limit is None or limit > 0
    info = resources.summary()
    assert info["max_resolution"] == ceiling
    assert info["workers"] >= 1


def test_resolution_is_clamped_to_memory_ceiling() -> None:
    """超过本机内存承受范围的分辨率要被裁到上限（否则容器会被 OOM）。"""
    job = client.post("/api/lattice/samples/cube").json()
    job_id = job["id"]
    wait_for_job(job_id, ("ready",))

    ceiling = resources.max_resolution()[0]
    accepted = client.post(
        f"/api/lattice/jobs/{job_id}/generate",
        json={"lattice": "bcc", "cell_size": 12, "thickness": 2.0, "resolution": 320},
    )
    assert accepted.status_code == 200
    assert accepted.json()["params"]["resolution"] <= ceiling, "分辨率没有被裁到内存上限"

    state = wait_for_job(job_id, ("done",), timeout=300.0)
    assert state["status"] == "done", state
    client.delete(f"/api/lattice/jobs/{job_id}")


def test_decimate_never_breaks_watertight() -> None:
    """抽稀只能在不新增坏边的前提下生效；抽坏了宁可不抽，并告知用户。

    gyroid 的薄壁能抽（60% 仍闭合），bcc 的细杆抽到 60% 就会把杆两侧缝成非流形
    边 —— 那就退回不抽，并在 warnings 里说明。
    """
    vol = box_volume(96)
    gyroid = {"lattice": "gyroid", "cell_size": 8.0, "thickness": 1.0,
              "resolution": 96, "smooth": 0}
    raw = pipeline.build(vol, {**gyroid, "decimate": 0.0})
    cut = pipeline.build(vol, {**gyroid, "decimate": 0.6})
    assert cut.stats["faces"] < raw.stats["faces"], "gyroid 上抽稀应当生效"
    assert cut.stats["boundary_edges"] == 0, "抽稀抽出了洞"
    assert cut.stats["nonmanifold_edges"] == 0, "抽稀缝出了非流形边"

    rod = {"lattice": "bcc", "cell_size": 8.0, "thickness": 1.0,
           "resolution": 96, "smooth": 0}
    raw_rod = pipeline.build(vol, {**rod, "decimate": 0.0})
    cut_rod = pipeline.build(vol, {**rod, "decimate": 0.6})
    assert cut_rod.stats["nonmanifold_edges"] == 0, "抽稀把细杆缝成了非流形边"
    if cut_rod.stats["faces"] >= raw_rod.stats["faces"]:
        assert any("抽稀已跳过" in w for w in cut_rod.stats["warnings"]), "跳过抽稀要说一声"


def test_param_normalization() -> None:
    """越界参数被裁剪，非法值回落默认。"""
    p = pipeline.normalize_params(
        {"lattice": "nope", "cell_size": -5, "thickness": 999, "resolution": 9999, "mode": "??"}
    )
    assert p["lattice"] == "gyroid"
    assert 1.0 <= p["cell_size"] <= 200.0
    assert p["thickness"] <= 0.8 * p["cell_size"]
    assert p["resolution"] <= 320
    assert p["mode"] == "intersect"


def test_thin_wall_is_thickened_automatically() -> None:
    """壁厚不到 1.5 个体素时自动加厚 —— 否则等值面只会画出一张刺球。"""
    vol = box_volume(96)
    pitch = float(vol["pitch"])
    wall = 1.2 * pitch                       # 只有 1.2 个体素，画不出连续的壁
    result = pipeline.build(
        vol, {"lattice": "gyroid", "cell_size": 8.0, "thickness": wall,
              "resolution": 96, "smooth": 0, "decimate": 0.0}
    )
    stats = result.stats
    assert stats["thin_wall_fixed"] is True
    assert stats["thickness_requested"] == pytest.approx(wall)
    assert stats["thickness_used"] == pytest.approx(
        pipeline.MIN_RENDER_WALL_VOXELS * pitch, rel=1e-6
    )
    assert stats["wall_voxels"] == pytest.approx(pipeline.MIN_RENDER_WALL_VOXELS, rel=1e-6)
    assert result.params["thickness"] == pytest.approx(stats["thickness_used"])
    assert stats["warnings"] and stats["warnings"][0].startswith("ℹ"), "兜底必须告知用户"
    assert open_edges(result.mesh) == 0, "加厚后的网格应当是闭合实体"


def test_thin_wall_off_keeps_requested_thickness() -> None:
    """thin_wall=off 时按原值硬画，并把"会出尖刺"写进警告。"""
    vol = box_volume(96)
    pitch = float(vol["pitch"])
    wall = 1.2 * pitch
    result = pipeline.build(
        vol, {"lattice": "gyroid", "cell_size": 8.0, "thickness": wall,
              "resolution": 96, "smooth": 0, "decimate": 0.0, "thin_wall": "off"}
    )
    stats = result.stats
    assert stats["thin_wall_fixed"] is False
    assert stats["thickness_used"] == pytest.approx(wall)
    assert stats["wall_voxels"] == pytest.approx(1.2)
    assert any(w.startswith("⚠") and "体素" in w for w in stats["warnings"])


def test_thin_wall_is_left_alone_when_thick_enough() -> None:
    """2.4 个体素的壁能画出来，就不该被动过。"""
    vol = box_volume(96)
    pitch = float(vol["pitch"])
    wall = 2.4 * pitch
    result = pipeline.build(
        vol, {"lattice": "gyroid", "cell_size": 8.0, "thickness": wall,
              "resolution": 96, "smooth": 0, "decimate": 0.0}
    )
    stats = result.stats
    assert stats["thin_wall_fixed"] is False
    assert stats["thickness_used"] == pytest.approx(wall)
    assert not any(w.startswith("ℹ") for w in stats["warnings"])


def test_export_formats() -> None:
    """STL / OBJ / PLY / 3MF 都能导出且非空。"""
    vol = box_volume(80)
    result = pipeline.build(vol, {"lattice": "bcc", "cell_size": 10.0, "thickness": 1.4, "resolution": 80})
    with tempfile.TemporaryDirectory() as tmp:
        for fmt in ("stl", "obj", "ply", "3mf"):
            path = meshio.export_mesh(result.mesh, os.path.join(tmp, f"out.{fmt}"))
            assert os.path.getsize(path) > 1024, f"{fmt} 导出文件过小"


def test_step_pipeline() -> None:
    """STEP → 网格 → 体素 → 晶格（用 gmsh 现造一个带孔实体）。"""
    gmsh = pytest.importorskip("gmsh")
    with tempfile.TemporaryDirectory() as tmp:
        step_path = os.path.join(tmp, "part.step")
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.model.add("t")
            gmsh.model.occ.addBox(0, 0, 0, 30, 24, 18)
            gmsh.model.occ.addCylinder(15, 12, -1, 0, 0, 20, 5)
            gmsh.model.occ.synchronize()
            gmsh.model.occ.cut([(3, 1)], [(3, 2)])
            gmsh.model.occ.synchronize()
            gmsh.write(step_path)
        finally:
            gmsh.finalize()

        _mesh, info = meshio.load_mesh(step_path)
        assert info["faces"] > 500
        assert info["watertight"]
        expected = 30 * 24 * 18 - np.pi * 25 * 18
        assert abs(info["volume"] - expected) / expected < 0.02


# ---------------------------------------------------------------------------
# 摆线轮盘 → 3D 实体（两个工具的衔接点）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "params,width",
    [
        ({"tooth_count": 39, "eccentricity": 1.43, "pin_circle_radius": 88.0, "pin_radius": 5.5}, 14.0),
        ({"tooth_count": 11, "eccentricity": 1.86, "pin_circle_radius": 42.0, "pin_radius": 4.5}, 10.0),
        ({"tooth_count": 59, "eccentricity": 1.4, "pin_circle_radius": 120.0, "pin_radius": 6.0}, 20.0),
    ],
)
def test_cycloid_disc_solid(params: dict, width: float) -> None:
    """齿廓拉伸出的实体必须闭合、有孔、尺寸合理。"""
    p = CycloidParams(**params)
    mesh, info = build_disc_mesh(p, width=width)
    assert mesh.is_watertight, "轮盘实体不闭合"
    assert mesh.volume > 0
    assert info["hole_count"] >= 3
    assert abs(mesh.extents[2] - width) < 1e-6, "盘厚与设定不符"
    assert mesh.extents[0] < 2 * params["pin_circle_radius"], "外径异常放大"


def test_cycloid_disc_lattice_end_to_end() -> None:
    """摆线轮盘直接晶格化：能出闭合的减重结构且体积下降。"""
    p = CycloidParams(tooth_count=39, eccentricity=1.43, pin_circle_radius=88.0, pin_radius=5.5)
    mesh, info = build_disc_mesh(p, width=14.0)
    vol = voxel.mesh_to_volume(mesh, resolution=110)
    result = pipeline.build(
        vol,
        {"lattice": "gyroid", "cell_size": 10.0, "thickness": 1.2, "mode": "shell",
         "shell_thickness": 3.0, "resolution": 110},
    )
    assert open_edges(result.mesh) == 0
    assert result.stats["volume_mm3"] < info["volume"], "晶格化后体积没有下降"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------
def test_lattice_health_and_config() -> None:
    r = client.get("/api/lattice/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    cfg = client.get("/api/lattice/config").json()
    assert set(cfg["lattices"]) == set(LATTICE_KINDS)
    assert list(cfg["modes"]) == ["intersect", "shell", "subtract", "union"]
    assert "stl" in cfg["export_formats"]
    assert cfg["cycloid"]["default_width"] > 0


def test_pages_are_served() -> None:
    """首页 + 两个独立工具页都能打开（两个工具各自一套静态资源）。"""
    home = client.get("/")
    assert home.status_code == 200
    assert "CAD 工具箱" in home.text

    cycloid = client.get("/cycloid")
    assert cycloid.status_code == 200
    assert "摆线针轮" in cycloid.text
    assert "/static/cycloid/app.js" in cycloid.text

    lattice = client.get("/lattice")
    assert lattice.status_code == 200
    assert "晶格化" in lattice.text
    assert "/static/lattice/app.js" in lattice.text
    # GPU 引擎与着色器是这个页面的核心依赖
    assert "/static/lattice/style.css" in lattice.text


def test_lattice_api_flow() -> None:
    """上传样例 → 生成 → 预览 → 下载 的完整链路。"""
    created = client.post("/api/lattice/samples/bracket")
    assert created.status_code == 200, created.text
    job = created.json()
    job_id = job["id"]

    state = wait_for_job(job_id, ("ready",))
    assert state["status"] == "ready", state

    gen = client.post(
        f"/api/lattice/jobs/{job_id}/generate",
        json={"lattice": "gyroid", "cell_size": 9.0, "thickness": 1.1, "resolution": 96},
    )
    assert gen.status_code == 200, gen.text

    state = wait_for_job(job_id, ("done",), timeout=180.0)
    assert state["status"] == "done", state
    assert state["stats"]["faces"] > 1000

    preview = client.get(f"/api/lattice/jobs/{job_id}/preview/lattice")
    assert preview.status_code == 200
    assert preview.content[:4] == b"glTF"

    dl = client.get(f"/api/lattice/jobs/{job_id}/download/stl")
    assert dl.status_code == 200
    assert len(dl.content) > 1024

    assert client.delete(f"/api/lattice/jobs/{job_id}").status_code == 200


def test_lattice_from_cycloid_api() -> None:
    """用摆线轮参数直接建作业，info 里应带回齿数与质量估算。"""
    r = client.post(
        "/api/lattice/jobs/from-cycloid",
        json={
            "params": {"tooth_count": 19, "eccentricity": 1.6, "pin_circle_radius": 50.0,
                       "pin_radius": 3.2},
            "width": 12,
        },
    )
    assert r.status_code == 200, r.text
    job = r.json()
    cy = job["info"]["cycloid"]
    assert cy["tooth_count"] == 19
    assert cy["width"] == 12
    assert cy["mass_kg_steel"] > 0
    assert job["info"]["watertight"] is True
    client.delete(f"/api/lattice/jobs/{job['id']}")


def test_preview_accepts_any_version_token() -> None:
    """回归：`v` 是缓存穿透标记，前端传的是复合值（如 ``3-1793264757956``）。

    早期把它声明成 ``int``，FastAPI 对这类值直接返回 422，用户在界面上只会看到
    「预览加载失败」——而 422 不抛 JS 异常、也不触发 requestfailed，很难被发现。
    """
    created = client.post("/api/lattice/samples/cube")
    job_id = created.json()["id"]
    wait_for_job(job_id, ("ready",))

    for token in ("0", "3", "3-1793264757956", "1.5", "abc"):
        r = client.get(f"/api/lattice/jobs/{job_id}/preview/original?v={token}")
        assert r.status_code == 200, f"v={token!r} 返回 {r.status_code}：{r.text[:200]}"
        assert r.content[:4] == b"glTF", f"v={token!r} 响应不是 GLB"

    client.delete(f"/api/lattice/jobs/{job_id}")


def test_lattice_rejects_bad_upload() -> None:
    """非法格式与空文件都要给中文错误。"""
    bad = client.post(
        "/api/lattice/jobs",
        files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert bad.status_code == 400
    assert "不支持" in bad.json()["detail"]

    empty = client.post(
        "/api/lattice/jobs",
        files={"file": ("empty.stl", io.BytesIO(b""), "model/stl")},
    )
    assert empty.status_code == 400
