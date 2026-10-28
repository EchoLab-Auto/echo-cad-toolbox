"""生成文档截图（README 配图）。需先启动服务。

用法：python3 tools/shoot.py http://127.0.0.1:8080/
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


def share_token(params: dict) -> str:
    raw = json.dumps(params).encode()
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


def main() -> int:
    shots = [
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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1680, "height": 950}, device_scale_factor=1.5)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        for shot in shots:
            url = BASE + "/"
            if shot["params"]:
                url += "?p=" + share_token(shot["params"])
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(900)
            page.click("#btnPlay")
            page.wait_for_timeout(shot["play_ms"])
            page.click("#btnPlay")  # 暂停在中间相位
            page.wait_for_timeout(200)
            path = OUT / shot["file"]
            page.screenshot(path=str(path))
            hud = page.inner_text("#hud").replace("\n", " | ")
            print(f"{shot['file']:26s} {shot['note']:24s} {hud}")

        browser.close()

    if errors:
        print("JS errors:", errors)
        return 1
    print("done →", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
