"""浏览器端验证：真实 Chromium 渲染 + JS 错误捕获 + 像素统计。

用途：在没有人工查看的情况下确认前端确实渲染出内容、交互不报错。
运行：python3 tools/browser_check.py [url]（默认摆线针轮设计器 /cycloid）
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099/cycloid"

PIXEL_PROBE = """
() => {
  const c = document.querySelector('#view');
  if (!c) return { error: 'no canvas' };
  const ctx = c.getContext('2d');
  const { width, height } = c;
  const img = ctx.getImageData(0, 0, width, height).data;
  let nonBg = 0, green = 0, blue = 0, amber = 0, total = 0;
  for (let i = 0; i < img.length; i += 4 * 7) {
    const r = img[i], g = img[i + 1], b = img[i + 2];
    total++;
    if (r > 24 || g > 32 || b > 42) nonBg++;
    if (g > 90 && g > r * 1.5 && g > b * 1.3) green++;
    if (b > 110 && b > r * 1.3) blue++;
    if (r > 120 && g > 80 && b < 90) amber++;
  }
  return {
    width, height, total,
    nonBg: +(nonBg / total).toFixed(4),
    green: +(green / total).toFixed(4),
    blue: +(blue / total).toFixed(4),
    amber: +(amber / total).toFixed(4),
  };
}
"""


def main() -> int:
    issues: list[str] = []
    logs: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1680, "height": 950}, device_scale_factor=1)

        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: issues.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: issues.append(f"requestfailed: {r.url} {r.failure}"))

        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        stats_idle = page.evaluate(PIXEL_PROBE)
        page.screenshot(path="/tmp/cadtool/shot_idle.png")

        hud = page.inner_text("#hud")
        metrics = page.inner_text("#metricsPanel")
        params_count = page.evaluate("document.querySelectorAll('#paramsPanel .ctrl').length")
        toggles = page.evaluate("document.querySelectorAll('#toggles input').length")

        # --- 交互：播放动画 ---
        page.click("#btnPlay")
        page.wait_for_timeout(900)
        stats_anim = page.evaluate(PIXEL_PROBE)
        page.screenshot(path="/tmp/cadtool/shot_anim.png")
        phi_txt = page.inner_text("#phiVal")

        # --- 交互：改参数（偏心距）---
        page.evaluate(
            """() => {
              const rows = [...document.querySelectorAll('#paramsPanel .ctrl')];
              const row = rows.find(r => r.textContent.includes('偏心距'));
              const num = row.querySelector('input[type=number]');
              num.value = '2.2';
              num.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(1200)
        metrics2 = page.inner_text("#metricsPanel")
        stats_mod = page.evaluate(PIXEL_PROBE)

        # --- 导出 DXF（检查下载）---
        download_ok = False
        try:
            with page.expect_download(timeout=15000) as dl:
                page.click("#btnDxf")
            download = dl.value
            download.save_as("/tmp/cadtool/browser_download.dxf")
            download_ok = True
            filename = download.suggested_filename
        except Exception as exc:  # noqa: BLE001
            filename = f"<failed: {exc}>"

        browser.close()

    report = {
        "url": URL,
        "pixel_idle": stats_idle,
        "pixel_anim": stats_anim,
        "pixel_modified": stats_mod,
        "hud": hud.strip(),
        "params_controls": params_count,
        "toggles": toggles,
        "phi_after_anim": phi_txt,
        "metrics_has_ratio": "39 : 1" in metrics,
        "metrics_after_mod_changed": metrics != metrics2,
        "dxf_download_ok": download_ok,
        "dxf_filename": filename,
        "js_issues": issues,
        "console": logs[:15],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # --- 判定 ---
    fails: list[str] = []
    if issues:
        fails.append(f"JS/网络错误 {issues}")
    if stats_idle.get("nonBg", 0) < 0.02:
        fails.append(f"画布几乎空白 nonBg={stats_idle.get('nonBg')}")
    if stats_idle.get("green", 0) < 0.002:
        fails.append(f"未检测到绿色齿廓像素 green={stats_idle.get('green')}")
    if stats_idle.get("blue", 0) < 0.001:
        fails.append(f"未检测到针齿（蓝色）像素 blue={stats_idle.get('blue')}")
    if params_count < 10:
        fails.append(f"参数控件数量异常 {params_count}")
    if "39 : 1" not in metrics:
        fails.append("指标面板未显示传动比")
    if not download_ok:
        fails.append("DXF 下载失败")

    print("\n=== 判定 ===")
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS: 渲染、交互、导出均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
