"""生成文档截图（README 配图）。需先启动服务。

用法：python3 tools/shoot.py http://127.0.0.1:8080/

产物（docs/）：

| 文件 | 内容 |
| --- | --- |
| ``screenshot-home.png``        | 工具箱首页（两张工具卡） |
| ``screenshot.png``             | 摆线针轮设计器 · 默认 39/40，动画进行中 |
| ``screenshot-compact.png``     | 摆线针轮设计器 · 紧凑型 11/12 |
| ``screenshot-highratio.png``   | 摆线针轮设计器 · 大速比 59/60 |
| ``screenshot-lattice.png``     | 晶格化工作台 · 浏览器 GPU 生成的 gyroid 减重盘体 |
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/").rstrip("/")
OUT = Path(__file__).resolve().parent.parent / "docs"
OUT.mkdir(exist_ok=True)

CHROME_FLAGS = [
    "--no-sandbox",
    "--enable-unsafe-webgpu",
    "--enable-features=Vulkan",
    "--use-angle=vulkan",
]


def share_token(params: dict) -> str:
    raw = json.dumps(params).encode()
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


CYCLOID_SHOTS = [
    {
        "file": "screenshot.png",
        "params": None,
        "play_ms": 1100,
        "note": "默认 39/40，动画进行中",
    },
    {
        "file": "screenshot-compact.png",
        "params": {"tooth_count": 11, "eccentricity": 1.86, "pin_circle_radius": 42, "pin_radius": 4.5},
        "play_ms": 700,
        "note": "紧凑型 11/12",
    },
    {
        "file": "screenshot-highratio.png",
        "params": {"tooth_count": 59, "eccentricity": 1.4, "pin_circle_radius": 120, "pin_radius": 6.0},
        "play_ms": 500,
        "note": "大速比 59/60（齿高更小）",
    },
]


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=CHROME_FLAGS)
        page = browser.new_page(viewport={"width": 1680, "height": 950}, device_scale_factor=1.5)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---------------- 首页 ----------------
        page.goto(BASE + "/", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "screenshot-home.png"), full_page=True)
        print(f"{'screenshot-home.png':26s} 工具箱首页")

        # ---------------- 摆线针轮设计器 ----------------
        for shot in CYCLOID_SHOTS:
            url = BASE + "/cycloid"
            if shot["params"]:
                url += "?p=" + share_token(shot["params"])
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(900)
            page.click("#btnPlay")
            page.wait_for_timeout(shot["play_ms"])
            page.click("#btnPlay")  # 暂停在中间相位
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / shot["file"]))
            hud = page.inner_text("#hud").replace("\n", " | ")
            print(f"{shot['file']:26s} {shot['note']:24s} {hud}")

        # ---------------- 晶格化工作台 ----------------
        page.goto(BASE + "/lattice", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        # 用摆线轮盘做减重晶格：这既是两个工具的衔接，也是量具最完整的展示
        page.click("#btnCycloid")
        page.wait_for_timeout(400)
        page.click("#btnBuildCycloid")
        try:
            page.wait_for_function("() => window.__lattice?.state?.volume !== null", timeout=90000)
            page.click("#btnGenerate")
            page.wait_for_function("() => window.__lattice?.state?.mesh !== null", timeout=90000)
            page.wait_for_timeout(700)
            page.click("#rotateBtn")
            page.wait_for_timeout(500)
            page.click("#rotateBtn")
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / "screenshot-lattice.png"))
            stats = page.inner_text("#statsKv").replace("\n", " | ")
            print(f"{'screenshot-lattice.png':26s} 晶格化工作台 · GPU   {stats[:90]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"晶格化截图失败：{exc}")

        browser.close()

    if errors:
        print("JS errors:", errors)
        return 1
    print("done →", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
