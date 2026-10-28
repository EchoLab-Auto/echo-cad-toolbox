"""DXF 导出测试：结构完整性、图层、单位与零件排布。"""

from __future__ import annotations

import io

import ezdxf
import numpy as np
import pytest
from ezdxf import bbox

from app.dxf_export import ALL_PARTS, build_document, dxf_bytes, suggested_filename
from app.geometry import CycloidParams

PARAMS = CycloidParams()







def test_units_are_millimetres() -> None:
    doc = build_document(PARAMS, ["cycloid_disc"])
    assert doc.units == ezdxf.units.MM
    assert doc.header["$INSUNITS"] == 4


def test_layers_present() -> None:
    doc = build_document(PARAMS, list(ALL_PARTS))
    names = {layer.dxf.name for layer in doc.layers}
    for expected in ("PROFILE", "THEORY", "PIN", "HOLE", "RING", "CENTER", "TEXT", "BORE"):
        assert expected in names


@pytest.mark.parametrize("parts", [["cycloid_disc"], ["pin_ring"], ["output_disc"], ["assembly"]])
def test_single_part_renders(parts) -> None:
    doc = build_document(PARAMS, parts)
    msp = doc.modelspace()
    assert len(list(msp)) > 0
    ext = bbox.extents(msp)
    assert ext.has_data
    # 所有内容应落在合理的毫米范围内
    assert ext.extmax.x < 1000 and ext.extmin.x > -1000


def test_profle_vertex_count() -> None:
    doc = build_document(PARAMS, ["cycloid_disc"], include_theoretical=False)
    polylines = [e for e in doc.modelspace().query("LWPOLYLINE")]
    assert len(polylines) == 1
    assert len(polylines[0]) == PARAMS.tooth_count * PARAMS.samples_per_tooth
    assert polylines[0].closed


def test_profile_geometry_matches_analysis() -> None:
    """DXF 中齿廓的半径范围应与解析分析一致。"""
    doc = build_document(PARAMS, ["cycloid_disc"], include_theoretical=False)
    pl = next(iter(doc.modelspace().query("LWPOLYLINE")))
    pts = np.array([(p[0], p[1]) for p in pl.get_points()])
    radii = np.hypot(pts[:, 0], pts[:, 1])
    assert 81.0 < radii.min() < 81.2
    assert 83.8 < radii.max() < 84.0


def test_separate_parts_do_not_overlap() -> None:
    """多个零件分件排布时必须互不重叠。"""
    doc = build_document(PARAMS, ["cycloid_disc", "pin_ring", "output_disc"])
    msp = doc.modelspace()
    xs = np.array([e.dxf.center.x for e in msp.query("CIRCLE")])
    # 三簇圆心应分居三个 x 区间
    assert xs.max() - xs.min() > 200


def test_theoretical_layer_toggle() -> None:
    with_theory = build_document(PARAMS, ["cycloid_disc"], include_theoretical=True)
    without = build_document(PARAMS, ["cycloid_disc"], include_theoretical=False)
    assert len(list(with_theory.modelspace().query('LWPOLYLINE[layer=="THEORY"]'))) == 1
    assert len(list(without.modelspace().query('LWPOLYLINE[layer=="THEORY"]'))) == 0


def test_centerlines_toggle() -> None:
    with_center = build_document(PARAMS, ["cycloid_disc"], include_centerlines=True)
    without = build_document(PARAMS, ["cycloid_disc"], include_centerlines=False)
    assert len(list(with_center.modelspace().query('LINE[layer=="CENTER"]'))) > 0
    assert len(list(without.modelspace().query('LINE[layer=="CENTER"]'))) == 0


def test_filenames() -> None:
    assert suggested_filename(PARAMS, ["assembly"]).startswith("cycloid_assembly_")
    assert "Zc39" in suggested_filename(PARAMS, ["cycloid_disc"])
    assert suggested_filename(PARAMS, ["cycloid_disc", "pin_ring"]).startswith("cycloid_parts_")


def test_metadata_stamped() -> None:
    doc = build_document(PARAMS, ["cycloid_disc"])
    values = {name: value for name, value in doc.header.custom_vars}
    assert "Zc=39" in str(values.get("PARAMS", ""))


def test_dxf_roundtrip_file(tmp_path) -> None:
    path = tmp_path / "disc.dxf"
    path.write_bytes(dxf_bytes(PARAMS, ["cycloid_disc"]))
    doc = ezdxf.readfile(str(path))
    assert doc.dxfversion == "AC1024"
    assert len(list(doc.modelspace())) > 0
