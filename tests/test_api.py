"""HTTP API 测试。"""

from __future__ import annotations

import io

import ezdxf
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_presets_are_valid() -> None:
    r = client.get("/api/presets")
    assert r.status_code == 200
    presets = r.json()["presets"]
    assert len(presets) >= 3
    for preset in presets:
        res = client.post("/api/analyze", json={"params": preset["params"], "contact_steps": 0})
        assert res.status_code == 200, preset["id"]
        errors = [w for w in res.json()["warnings"] if w["level"] == "error"]
        assert errors == [], f"预设 {preset['id']} 存在几何错误：{errors}"


def test_analyze_default() -> None:
    r = client.post("/api/analyze", json={"params": {}, "contact_steps": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["derived"]["pin_count"] == data["derived"]["ratio"] + 1
    assert len(data["profile"]["actual"]) == 39 * 64
    assert len(data["pin_ring"]["pin_centers"]) == 40
    assert len(data["output"]["hole_centers"]) == data["output"]["count"]
    assert data["derived"]["star_shaped"] is True
    # 所有点必须是有限数
    for x, y in data["profile"]["actual"][:100]:
        assert abs(x) < 1e6 and abs(y) < 1e6


def test_analyze_with_contacts() -> None:
    r = client.post("/api/analyze", json={"params": {"tooth_count": 11}, "contact_steps": 24})
    assert r.status_code == 200
    contacts = r.json()["contacts"]
    assert contacts["steps"] == 24
    assert len(contacts["pin_indices"]) == 24
    assert all(i >= 0 for row in contacts["pin_indices"] for i in row)


@pytest.mark.parametrize(
    "params",
    [
        {"tooth_count": 2000},
        {"eccentricity": -1},
        {"pin_circle_radius": 0},
    ],
)
def test_analyze_rejects_invalid(params: dict) -> None:
    r = client.post("/api/analyze", json={"params": params})
    assert r.status_code == 422  # pydantic 校验


def test_undercut_reported_not_crash() -> None:
    """K1 > 1（根切）时应报告错误告警而不是 500。"""
    r = client.post("/api/analyze", json={"params": {"eccentricity": 3.0}, "contact_steps": 0})
    assert r.status_code == 200
    codes = {w["code"] for w in r.json()["warnings"]}
    assert "undercut" in codes


def test_export_dxf_post() -> None:
    r = client.post("/api/export/dxf", json={"params": {}, "parts": ["cycloid_disc"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/dxf"
    assert "attachment" in r.headers["content-disposition"]
    doc = ezdxf.read(io.StringIO(r.content.decode("utf-8")))
    msp = doc.modelspace()
    assert len(list(msp.query("LWPOLYLINE"))) >= 1


def test_export_dxf_get_query() -> None:
    r = client.get(
        "/api/export/dxf",
        params={"tooth_count": 20, "eccentricity": 1.2, "pin_circle_radius": 50, "pin_radius": 3},
    )
    assert r.status_code == 200
    assert len(r.content) > 5000


def test_export_svg() -> None:
    r = client.post("/api/export/svg", json={"params": {}, "parts": ["assembly"]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


def test_index_served() -> None:
    """首页是工具箱启动台，设计器在 /cycloid。"""
    home = client.get("/")
    assert home.status_code == 200
    assert "CAD 工具箱" in home.text
    assert "/cycloid" in home.text and "/lattice" in home.text

    r = client.get("/cycloid")
    assert r.status_code == 200
    assert "摆线针轮" in r.text
    assert "/static/cycloid/app.js" in r.text


def test_static_assets() -> None:
    """三个页面的 CSS / JS / 着色器 / three.js 都能取到。"""
    for path in (
        "/static/favicon.svg",
        "/static/base.css",
        "/static/home/home.js",
        "/static/home/home.css",
        "/static/cycloid/app.js",
        "/static/cycloid/style.css",
        "/static/cycloid/analysis.js",
        "/static/lattice/app.js",
        "/static/lattice/style.css",
        "/static/lattice/gpu/engine.js",
        "/static/lattice/gpu/exporters.js",
        "/static/lattice/gpu/lattice.wgsl",
        "/static/vendor/three/build/three.module.js",
    ):
        assert client.get(path).status_code == 200, path
