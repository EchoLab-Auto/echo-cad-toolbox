"""体素场（``/api/lattice/jobs/{id}/volume``）接口测试。

这个接口是**浏览器 GPU 生成路径**唯一依赖的服务端能力：它把一次性体素化的
有符号距离场以 float32 原始字节返回，元数据放在 ``X-Volume-Meta`` 头里。
"""

from __future__ import annotations

import json
import struct

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _shape_of(response) -> tuple[dict, bytes]:
    meta = json.loads(response.headers["x-volume-meta"])
    return meta, response.content


@pytest.fixture(scope="module")
def cube_job() -> str:
    job = client.post("/api/lattice/samples/cube").json()
    yield job["id"]
    client.delete(f"/api/lattice/jobs/{job['id']}")


def test_volume_endpoint_shape_and_type(cube_job: str) -> None:
    r = client.get(f"/api/lattice/jobs/{cube_job}/volume", params={"resolution": 96})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/octet-stream"

    meta, body = _shape_of(r)
    nx, ny, nz = meta["shape"]
    assert len(body) == nx * ny * nz * 4
    assert meta["bytes"] == len(body)
    # 立方体 40×30×24 + 每边 3 层 padding
    assert meta["pitch"] == pytest.approx(40.0 / 96, rel=1e-9)
    assert nx > ny > nz

    values = struct.unpack(f"<{len(body) // 4}f", body)
    assert min(values) < 0 < max(values)

    # 立方体中心（半高 12 mm）的 SDF 应接近 -12，误差在一格体素内
    cx, cy, cz = nx // 2, ny // 2, nz // 2
    center = values[(cx * ny + cy) * nz + cz]
    assert center == pytest.approx(-12.0, abs=2.0)
    # 角落（体场边界之外）必须是外部
    assert values[0] > 0


def test_volume_resolution_is_clamped(cube_job: str) -> None:
    """服务端会把握手范围内的分辨率再按内存能力裁一次（不会把容器 OOM 掉）。"""
    r = client.get(f"/api/lattice/jobs/{cube_job}/volume", params={"resolution": 400})
    assert r.status_code == 200
    meta, body = _shape_of(r)
    # 请求 400，但服务端按内存能力裁到 320：最长边 = 320 + 两侧 padding（各 3 层）
    assert max(meta["shape"]) <= 320 + 2 * 3 + 1
    assert len(body) == meta["shape"][0] * meta["shape"][1] * meta["shape"][2] * 4


def test_volume_is_cached_and_stable(cube_job: str) -> None:
    """同一（作业, 分辨率）重复请求：字节完全一致（走 npz 缓存）。"""
    a = client.get(f"/api/lattice/jobs/{cube_job}/volume", params={"resolution": 112})
    b = client.get(f"/api/lattice/jobs/{cube_job}/volume", params={"resolution": 112})
    assert a.status_code == b.status_code == 200
    assert a.content == b.content
    assert a.headers["x-volume-meta"] == b.headers["x-volume-meta"]


def test_volume_unknown_job() -> None:
    r = client.get("/api/lattice/jobs/nope-does-not-exist/volume")
    assert r.status_code == 400
    assert "作业" in r.json()["detail"]


def test_volume_rejects_bad_resolution_param(cube_job: str) -> None:
    r = client.get(f"/api/lattice/jobs/{cube_job}/volume", params={"resolution": 10})
    assert r.status_code == 422  # FastAPI 校验：ge=48
