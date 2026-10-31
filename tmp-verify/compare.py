"""逐体素比对服务端（Python）与浏览器端（JS）的 SDF。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

OUT = Path("/tmp/fix")


def load(name: str, suffix: str) -> np.ndarray:
    return np.fromfile(OUT / f"{name}_{suffix}_sdf.bin", dtype=np.float32)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "box"
    srv = load(name, "srv")
    js = load(name, "js")
    srv_meta = json.loads((OUT / f"{name}_srv_meta.json").read_text())
    js_meta = json.loads((OUT / f"{name}_js_meta.json").read_text())

    print(f"=== {name} ===")
    print(f"  服务端 shape={srv_meta['shape']} pitch={srv_meta['pitch']:.4f} fill={srv_meta['fill_ratio']:.4f}")
    print(f"  浏览器 shape={js_meta['shape']} pitch={js_meta['pitch']:.4f} fill={js_meta['fill_ratio']:.4f} "
          f"耗时 {js_meta['ms']} ms")

    if list(srv.shape) != list(js.shape):
        print(f"  ✗ 形状不一致：{srv.shape} vs {js.shape}")
        return
    if abs(srv_meta["pitch"] - js_meta["pitch"]) > 1e-6:
        print(f"  ✗ pitch 不一致：{srv_meta['pitch']} vs {js_meta['pitch']}")
        return

    pitch = float(srv_meta["pitch"])
    # 1) 内外分类是否一致（这是最重要的：SDF 的符号决定形状）
    srv_in = srv < 0
    js_in = js < 0
    mismatch = int((srv_in != js_in).sum())
    total = srv_in.size
    print(f"  内外一致性：{total - mismatch}/{total} 一致，��一致 {mismatch} 个 "
          f"（{100.0 * mismatch / total:.4f}%）")

    # 2) 距离误差（只在符号一致处统计）
    same = srv_in == js_in
    diff = np.abs(srv[same] - js[same])
    if diff.size:
        print(f"  距离误差：平均 {diff.mean():.4f} mm  中位 {np.median(diff):.4f} mm  "
              f"p99 {np.percentile(diff, 99):.4f} mm  最大 {diff.max():.4f} mm  "
              f"（{diff.max() / pitch:.2f} 体素）")

    # 3) 表面对齐：零等值面附近（|srv| < 0.5 体素）的 JS 值应当也接近 0
    near = np.abs(srv) < 0.5 * pitch
    if near.any():
        print(f"  零等值面附近（{int(near.sum())} 体素）：JS |d| 平均 "
              f"{np.abs(js[near]).mean():.4f} mm，最大 {np.abs(js[near]).max():.4f} mm")

    ok = mismatch / total < 0.001 and (not diff.size or diff.mean() < 0.15 * pitch)
    print(f"  {'✓ 通过' if ok else '✗ 未达标'}")


if __name__ == "__main__":
    main()
