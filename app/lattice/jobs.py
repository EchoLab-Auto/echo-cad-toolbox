"""作业管理：上传 → 网格修复 → 体素化 → 生成晶格 → 导出产物。

每个作业一个目录（默认 ``data/lattice_jobs/<id>/``，可用环境变量
``LATTICE_DATA_DIR`` 覆盖）：:

    input.<ext>      用户上传的原始文件（或由摆线轮参数生成的临时文件）
    mesh.stl         规范化后的三角网格（所有后续计算都用它）
    original.glb     原模型预览
    volume.npz       体素 SDF 缓存（按分辨率缓存）
    lattice.glb      晶格预览
    lattice.stl      晶格 STL（导出母本）
    job.json         元数据
"""

from __future__ import annotations

import ctypes
import gc
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import trimesh

from . import meshio as io_mesh
from . import pipeline, resources
from . import voxel as fld

ROOT = Path(__file__).resolve().parents[2]
#: 作业产物目录。容器里由 Dockerfile / compose 提供可写的 /app/data 卷。
JOBS_DIR = Path(os.environ.get("LATTICE_DATA_DIR", ROOT / "data" / "lattice_jobs"))

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
PREVIEW_FACE_BUDGET = 400_000     # 预览网格的面数上限（超出则抽稀）
JOB_TTL_SECONDS = 24 * 3600


class JobError(RuntimeError):
    """作业层错误（可直接展示给用户）。"""


def release_memory() -> None:
    """把已释放的堆内存还给操作系统。

    CPython 释放大数组后，glibc 往往只把内存留在 arena 里不归还 OS，导致
    进程 RSS 单调上涨直到撞上容器内存上限被 OOM 杀掉（重启会丢掉所有作业）。
    晶格化一次要分配几百 MB，所以每完成一个任务就主动 ``malloc_trim`` 一次。
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # 非 glibc 平台（musl / macOS）没有这个符号，忽略


class Job:
    def __init__(self, job_id: str, directory: Path):
        self.id = job_id
        self.dir = directory
        self.lock = threading.Lock()
        self.filename = ""
        self.info: dict = {}
        self.status = "new"          # new|preparing|ready|running|done|error
        self.progress = 0.0
        self.stage = ""
        self.error: str | None = None
        self.params: dict | None = None
        self.stats: dict | None = None
        self.version = 0
        self.updated_at = time.time()
        self.created_at = time.time()
        self.resolution = 0

    # -- 路径 ------------------------------------------------------------- #
    @property
    def mesh_path(self) -> Path:
        return self.dir / "mesh.stl"

    @property
    def volume_path(self) -> Path:
        return self.dir / "volume.npz"

    @property
    def lattice_stl(self) -> Path:
        return self.dir / "lattice.stl"

    @property
    def lattice_glb(self) -> Path:
        return self.dir / "lattice.glb"

    @property
    def original_glb(self) -> Path:
        return self.dir / "original.glb"

    @property
    def meta_path(self) -> Path:
        return self.dir / "job.json"

    # -- 状态 ------------------------------------------------------------- #
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "filename": self.filename,
                "status": self.status,
                "progress": round(self.progress, 3),
                "stage": self.stage,
                "error": self.error,
                "info": self.info,
                "params": self.params,
                "stats": self.stats,
                "version": self.version,
                "resolution": self.resolution,
                "has_original": self.original_glb.exists(),
                "has_lattice": self.lattice_glb.exists() and self.version > 0,
            }

    def set_state(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)
            self.updated_at = time.time()

    def save_meta(self) -> None:
        payload = {
            "id": self.id,
            "filename": self.filename,
            "info": self.info,
            "params": self.params,
            "stats": self.stats,
            "version": self.version,
            "resolution": self.resolution,
            "created_at": self.created_at,
        }
        try:
            self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        except OSError:
            pass


class JobStore:
    def __init__(self, workers: int | None = None):
        if workers is None:
            workers = int(os.environ.get("LATTICE_WORKERS", "2"))
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lattice")
        # 内置样例是内存里现算的，只有作业目录需要落盘
        JOBS_DIR.mkdir(parents=True, exist_ok=True)

    # -- 基础 ------------------------------------------------------------- #
    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobError("作业不存在或已过期")
        return job

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.snapshot() for j in sorted(jobs, key=lambda x: -x.created_at)]

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job.dir, ignore_errors=True)

    def gc(self) -> None:
        """清理超过 TTL 的作业。"""
        now = time.time()
        with self._lock:
            stale = [jid for jid, job in self._jobs.items() if now - job.updated_at > JOB_TTL_SECONDS]
        for jid in stale:
            self.delete(jid)

    # -- 上传 ------------------------------------------------------------- #
    def create_from_bytes(self, data: bytes, filename: str, max_edge: float | None = None) -> Job:
        if not data:
            raise JobError("文件为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise JobError(f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")

        suffix = Path(filename).suffix.lower()
        if suffix not in io_mesh.SUPPORTED_EXTS:
            raise JobError(
                f"不支持的格式 {suffix or '(无扩展名)'}；"
                f"支持 {', '.join(sorted(io_mesh.SUPPORTED_EXTS))}"
            )

        job_id = uuid.uuid4().hex[:12]
        directory = JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(job_id, directory)
        job.filename = Path(filename).name
        with self._lock:
            self._jobs[job_id] = job

        raw_path = directory / f"input{suffix}"
        raw_path.write_bytes(data)

        try:
            mesh, info = io_mesh.load_mesh(raw_path, max_edge=max_edge)
            self._finalize_upload(job, mesh, info)
        except Exception as exc:
            job.set_state(status="error", error=str(exc), stage="上传失败")
            self.delete(job_id)  # 解析失败就别留残骸
            raise JobError(str(exc)) from exc
        return job

    def create_from_mesh(self, mesh: trimesh.Trimesh, filename: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        directory = JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(job_id, directory)
        job.filename = filename
        with self._lock:
            self._jobs[job_id] = job
        mesh = io_mesh.repair_mesh(mesh)
        self._finalize_upload(job, mesh, io_mesh.describe_mesh(mesh))
        return job

    def _finalize_upload(self, job: Job, mesh: trimesh.Trimesh, info: dict) -> None:
        job.set_state(status="preparing", stage="保存网格", progress=0.02)
        # 统一存成 STL，后续所有计算都基于它（STEP 只离散一次）
        io_mesh.export_mesh(mesh, job.mesh_path)
        job.info = dict(info)
        job.info["filename"] = job.filename
        job.info.setdefault("format", Path(job.filename).suffix.lstrip(".").lower())

        # 原模型预览：面数过多时抽稀，保证浏览器流畅
        preview = mesh
        if len(mesh.faces) > PREVIEW_FACE_BUDGET:
            try:
                preview = mesh.simplify_quadric_decimation(face_count=PREVIEW_FACE_BUDGET)
            except Exception:  # noqa: BLE001
                preview = mesh
        io_mesh.preview_glb(preview, job.original_glb)
        job.save_meta()

        # 体素化放后台，前端立即可见模型
        self._pool.submit(self._prepare_volume, job)

    def _prepare_volume(self, job: Job, resolution: int = 128) -> dict:
        job.set_state(status="preparing", stage="体素化", progress=0.15)
        try:
            mesh = io_mesh._load_trimesh_scene(job.mesh_path)
            volume = fld.mesh_to_volume(mesh, resolution=resolution)
            fld.save_volume(volume, job.volume_path)
            job.set_state(
                status="ready", progress=1.0, stage="就绪", resolution=resolution
            )
            job.info["voxel"] = {
                "resolution": resolution,
                "pitch": volume["pitch"],
                "shape": list(volume["shape"]),
            }
            job.save_meta()
            return volume
        except Exception as exc:  # noqa: BLE001
            job.set_state(status="error", error=f"体素化失败：{exc}", stage="体素化失败")
            return {}

    # -- 生成 ------------------------------------------------------------- #
    def generate(self, job_id: str, raw_params: dict) -> dict:
        job = self.get(job_id)
        with job.lock:
            if job.status in ("running", "preparing"):
                raise JobError("已有任务在执行，请稍候")
        params = pipeline.normalize_params(raw_params)
        # 体素分辨率不能超过本机内存能承受的上限，否则会把容器 OOM 掉
        ceiling = resources.max_resolution()[0]
        params["resolution"] = min(params["resolution"], ceiling)
        job.set_state(
            status="running", progress=0.0, stage="排队", error=None, params=params
        )
        job.save_meta()
        self._pool.submit(self._run_generate, job, params)
        return {"job_id": job.id, "params": params}

    def _run_generate(self, job: Job, params: dict) -> None:
        try:
            volume = self._ensure_volume(job, int(params["resolution"]))
            result = pipeline.build(
                volume,
                params,
                progress=lambda frac, stage: job.set_state(progress=frac, stage=stage),
            )
            job.set_state(progress=0.95, stage="导出产物")
            io_mesh.export_mesh(result.mesh, job.lattice_stl)

            preview = result.mesh
            if len(preview.faces) > PREVIEW_FACE_BUDGET:
                try:
                    preview = preview.simplify_quadric_decimation(face_count=PREVIEW_FACE_BUDGET)
                except Exception:  # noqa: BLE001
                    preview = result.mesh
            io_mesh.preview_glb(preview, job.lattice_glb)

            with job.lock:
                job.version += 1
            job.set_state(
                status="done",
                progress=1.0,
                stage="完成",
                stats=result.stats,
                params=result.params,
            )
            job.save_meta()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            job.set_state(status="error", error=str(exc), stage="生成失败", progress=0.0)
        finally:
            release_memory()

    def _ensure_volume(self, job: Job, resolution: int) -> dict:
        if job.volume_path.exists() and job.resolution == resolution:
            try:
                return fld.load_volume(job.volume_path)
            except Exception:  # noqa: BLE001
                pass
        job.set_state(status="preparing", stage="体素化", progress=0.1)
        mesh = io_mesh._load_trimesh_scene(job.mesh_path)
        volume = fld.mesh_to_volume(mesh, resolution=resolution)
        fld.save_volume(volume, job.volume_path)
        job.set_state(resolution=resolution)
        job.info["voxel"] = {
            "resolution": resolution,
            "pitch": volume["pitch"],
            "shape": list(volume["shape"]),
        }
        job.save_meta()
        return volume

    # -- 导出 ------------------------------------------------------------- #
    def export_path(self, job_id: str, fmt: str) -> tuple[Path, str]:
        job = self.get(job_id)
        fmt = fmt.lower().lstrip(".")
        if fmt not in io_mesh.EXPORT_FORMATS:
            raise JobError(f"不支持的导出格式 {fmt}")
        if not job.lattice_stl.exists():
            raise JobError("尚未生成晶格模型")

        stem = Path(job.filename).stem or "lattice"
        target = job.dir / f"{stem}_lattice.{fmt}"
        if not target.exists():
            mesh = io_mesh._load_trimesh_scene(job.lattice_stl)
            io_mesh.export_mesh(mesh, target)
        return target, f"{stem}_lattice.{fmt}"


# --------------------------------------------------------------------------- #
# 内置样例（用于无文件时快速体验）
# --------------------------------------------------------------------------- #
def sample_meshes() -> dict[str, callable]:
    def cube():
        return trimesh.creation.box(extents=(40.0, 30.0, 24.0))

    def sphere():
        return trimesh.creation.icosphere(subdivisions=4, radius=22.0)

    def cylinder():
        return trimesh.creation.cylinder(radius=18.0, height=40.0, sections=96)

    def bracket():
        plate = trimesh.creation.box(extents=(60.0, 40.0, 6.0))
        plate.apply_translation((0, 0, 3.0))
        wall = trimesh.creation.box(extents=(6.0, 40.0, 30.0))
        wall.apply_translation((-27.0, 0, 15.0))
        return trimesh.util.concatenate([plate, wall])

    def tube():
        outer = trimesh.creation.cylinder(radius=20.0, height=40.0, sections=96)
        inner = trimesh.creation.cylinder(radius=14.0, height=44.0, sections=96)
        try:
            cut = trimesh.boolean.difference([outer, inner])
            if cut is not None and len(cut.faces):
                return cut
        except Exception:  # noqa: BLE001
            pass
        return outer

    return {
        "cube": cube,
        "sphere": sphere,
        "cylinder": cylinder,
        "bracket": bracket,
        "tube": tube,
    }
