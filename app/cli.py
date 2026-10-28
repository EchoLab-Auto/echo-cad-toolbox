"""命令行入口：无需 Web 界面即可批量生成 DXF 图纸。

示例::

    # 默认参数，导出摆线轮零件图
    python -m app.cli -o disc.dxf

    # 自定义参数 + 装配图
    python -m app.cli --zc 39 --e 1.43 --rz 88 --rp 5.5 --parts assembly -o asm.dxf

    # 三个零件分件排布，并打印派生量
    python -m app.cli --parts cycloid_disc,pin_ring,output_disc -o parts.dxf --print-params
"""

from __future__ import annotations

import argparse
import sys

from .dxf_export import ALL_PARTS, dxf_bytes, suggested_filename
from .geometry import CycloidParams, analyze


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="摆线针轮齿廓生成器 —— 生成可加工 DXF 图纸",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-o", "--out", help="输出文件路径（默认按参数自动命名）")
    p.add_argument("--zc", type=int, default=39, help="摆线轮齿数 Zc")
    p.add_argument("--e", "--eccentricity", dest="eccentricity", type=float, default=1.43, help="偏心距 e (mm)")
    p.add_argument("--rz", "--pin-circle-radius", dest="pin_circle_radius", type=float, default=88.0,
                   help="针齿中心圆半径 Rz (mm)")
    p.add_argument("--rp", "--pin-radius", dest="pin_radius", type=float, default=5.5, help="针齿半径 rp (mm)")
    p.add_argument("--d-rp", dest="equidistant_offset", type=float, default=0.02, help="等距修形 Δrp (mm)")
    p.add_argument("--d-rz", dest="radial_offset", type=float, default=0.0, help="移距修形 ΔRz (mm)")
    p.add_argument("--rot", dest="rotary_offset_deg", type=float, default=0.0, help="转角修形 (deg)")
    p.add_argument("--wall", dest="ring_wall", type=float, default=12.0, help="针齿壳壁厚 (mm)")
    p.add_argument("--holes", dest="pin_hole_count", type=int, default=0, help="柱销数（0=自动）")
    p.add_argument("--samples", dest="samples_per_tooth", type=int, default=64, help="每齿采样点数")
    p.add_argument(
        "--parts",
        default="cycloid_disc",
        help=f"零件，逗号分隔，可选 {','.join(ALL_PARTS)}",
    )
    p.add_argument("--no-theory", action="store_true", help="不绘制理论齿廓")
    p.add_argument("--no-labels", action="store_true", help="不绘制文字标注")
    p.add_argument("--print-params", action="store_true", help="打印派生量与诊断")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = CycloidParams(
        tooth_count=args.zc,
        eccentricity=args.eccentricity,
        pin_circle_radius=args.pin_circle_radius,
        pin_radius=args.pin_radius,
        equidistant_offset=args.equidistant_offset,
        radial_offset=args.radial_offset,
        rotary_offset_deg=args.rotary_offset_deg,
        ring_wall=args.ring_wall,
        pin_hole_count=args.pin_hole_count,
        samples_per_tooth=args.samples_per_tooth,
    )

    parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    unknown = [p for p in parts if p not in ALL_PARTS]
    if unknown:
        print(f"未知零件：{', '.join(unknown)}（可选：{', '.join(ALL_PARTS)}）", file=sys.stderr)
        return 2

    if args.print_params:
        res = analyze(params)
        d = res["derived"]
        print(f"齿数 Zc/Zp        : {d['pin_count'] - 1} / {d['pin_count']}")
        print(f"传动比 i          : {d['ratio']:g} : 1")
        print(f"短幅系数 K1       : {d['short_amplitude_coefficient']:.4f}")
        print(f"齿顶圆 / 齿根圆   : {d['tip_radius']:.3f} / {d['root_radius']:.3f} mm")
        print(f"齿高              : {d['tooth_height']:.3f} mm")
        print(f"齿廓周长          : {d['profile_length']:.2f} mm")
        print(f"柱销数 / 分布圆   : {d['output']['count']} / R{d['output']['circle_radius']:.2f} mm")
        print(f"理论法向侧隙      : {d['theoretical_backlash']:.4f} mm")
        if res["warnings"]:
            print("诊断：")
            for w in res["warnings"]:
                print(f"  [{w['level']}] {w['message']}")
        else:
            print("诊断              : 几何参数合法")

    data = dxf_bytes(
        params,
        parts=parts,
        include_theoretical=not args.no_theory,
        label=not args.no_labels,
    )
    out = args.out or suggested_filename(params, parts)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"已写入 {out}（{len(data) / 1024:.0f} KB，{len(parts)} 个视图）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
