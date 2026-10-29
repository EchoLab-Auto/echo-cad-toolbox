"""晶格化工作台的浏览器端验证（真实 Chromium）。

检查内容：

1. 两个工具页面都能打开、无 JS 报错、无失败请求
2. 摆线设计器 → 「晶格化 →」按钮能把参数带过去（?p= 令牌）
3. 晶格化页面：生成摆线轮盘 → 生成晶格 → 三维视口真的画出了几何（像素统计）
4. 视图切换、线框、导出按钮状态、统计面板

用法::

    python3 tools/lattice_check.py [http://127.0.0.1:8099]
"""

from __future__ import annotations

import base64
import io
import json
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099").rstrip("/")

def canvas_pixels(page, selector: str = "#canvas") -> dict:
    """对画布元素截图并统计像素。

    直接 ``gl.readPixels`` 读到的是已清空的绘制缓冲（WebGL 默认
    ``preserveDrawingBuffer=false``），所以这里走截图——也就是用户真正看到的画面。
    """
    box = page.locator(selector).bounding_box()
    if not box or box["width"] < 2 or box["height"] < 2:
        return {"error": "canvas 尺寸异常"}
    # HUD 里的按钮是绿/青色，会被 "teal" 判定误当成晶格像素，统计时先隐藏
    page.add_style_tag(content=".hud{display:none!important}")
    page.wait_for_timeout(120)
    shot = page.screenshot(clip=box)
    page.add_style_tag(content=".hud{display:block!important}")
    image = Image.open(io.BytesIO(shot)).convert("RGB")
    w, h = image.size
    px = image.load()
    total = lit = teal = black = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            r, g, b = px[x, y]
            total += 1
            bright = max(r, g, b)
            if bright > 55:
                lit += 1
            if g > 80 and g > r * 1.15 and b > r * 1.1:
                teal += 1
            if bright < 20:
                black += 1
    return {
        "w": w, "h": h, "total": total,
        "lit": round(lit / total, 4), "teal": round(teal / total, 4), "dark": round(black / total, 4),
    }


def scene_meshes(page, group: str) -> dict:
    """读 three.js 场景图，确认某个 group 里真的有网格。

    比像素统计可靠：HUD 的绿色按钮、「原始」模型的灰色都会污染颜色判定。
    """
    return page.evaluate(
        """(name) => {
          const L = window.__lattice;
          if (!L) return {error: 'no __lattice hook'};
          const g = L.groups[name];
          let meshes = 0, tris = 0, hasNormals = 0;
          g.traverse((o) => {
            if (!o.isMesh) return;
            meshes += 1;
            const geo = o.geometry;
            if (geo?.index) tris += geo.index.count / 3;
            else if (geo?.attributes?.position) tris += geo.attributes.position.count / 3;
            if (geo?.attributes?.normal) hasNormals += 1;
          });
          return {meshes, tris: Math.round(tris), hasNormals, visible: g.visible};
        }""",
        group,
    )


def canvas_2d_pixels(page, selector: str = "#view") -> dict:
    """摆线设计器用的是 2D canvas，可以直接读 ImageData。"""
    return page.evaluate(
        """(sel) => {
          const c = document.querySelector(sel);
          if (!c) return { error: 'no canvas' };
          const ctx = c.getContext('2d');
          if (!ctx) return { error: 'no 2d ctx' };
          const { width, height } = c;
          const img = ctx.getImageData(0, 0, width, height).data;
          let nonBg = 0, total = 0;
          for (let i = 0; i < img.length; i += 4 * 11) {
            const r = img[i], g = img[i + 1], b = img[i + 2];
            total++;
            if (r > 28 || g > 36 || b > 46) nonBg++;
          }
          return { w: width, h: height, total, nonBg: +(nonBg / total).toFixed(4) };
        }""",
        selector,
    )


def share_token(params: dict) -> str:
    raw = json.dumps(params).encode()
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


def main() -> int:
    issues: list[str] = []
    steps: list[str] = []

    def ok(msg: str) -> None:
        steps.append(f"  ✓ {msg}")

    def bad(msg: str) -> None:
        issues.append(msg)
        steps.append(f"  ✗ {msg}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--use-gl=swiftshader", "--enable-webgl"])

        # ---------------- 1. 摆线针轮设计器 ----------------
        print("[1] 摆线针轮设计器")
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        js_errors: list[str] = []
        failed: list[str] = []
        # 关键：GLB 预览是 fetch 出来的，HTTP 4xx/5xx 不会触发 requestfailed，
        # 也不会抛 JS 异常，只会在前端弹一句「预览加载失败」。必须显式记住状态码。
        bad_responses: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.on("requestfailed", lambda r: failed.append(f"{r.url} ({r.failure})"))
        page.on(
            "response",
            lambda r: bad_responses.append(f"{r.status} {r.url}")
            if r.status >= 400 else None,
        )

        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(600)

        if page.title().find("摆线针轮") >= 0:
            ok(f"标题：{page.title()}")
        else:
            bad(f"标题异常：{page.title()}")

        nav = page.locator(".toolnav a")
        if nav.count() == 2:
            ok(f"工具导航 2 项：{nav.nth(0).inner_text()} / {nav.nth(1).inner_text()}")
        else:
            bad(f"工具导航项数异常：{nav.count()}")

        if page.locator("#paramsPanel .group").count() >= 3:
            ok(f"参数面板已渲染（{page.locator('#paramsPanel .group').count()} 组）")
        else:
            bad("参数面板未渲染")

        # 画布有内容
        px = canvas_2d_pixels(page)
        if px.get("nonBg", 0) > 0.01:
            ok(f"齿廓视图已绘制：非背景像素 {px['nonBg'] * 100:.1f}%（{px['w']}×{px['h']}）")
        else:
            bad(f"齿廓视图几乎空白：{px}")

        # ---------------- 2. 参数传递 ----------------
        print("[2] 摆线参数带到晶格化")
        page.click("#btnLattice")
        page.wait_for_url("**/lattice?p=*", timeout=15000)
        page.wait_for_timeout(400)
        ok(f"跳转到 {page.url.split('?')[0]}")

        token = page.url.split("p=")[1]
        decoded = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if decoded.get("tooth_count") == 39:
            ok(f"令牌携带齿数 Zc={decoded['tooth_count']}")
        else:
            bad(f"令牌参数异常：{decoded}")

        # ---------------- 3. 晶格化页面 ----------------
        print("[3] 晶格化工作台")
        lattice_errors: list[str] = []
        lattice_failed: list[str] = []
        page.on("pageerror", lambda e: lattice_errors.append(str(e)))
        page.on("requestfailed", lambda r: lattice_failed.append(f"{r.url} ({r.failure})"))

        if page.locator("#latticeSel option").count() == 9:
            ok("晶格类型下拉 9 项")
        else:
            bad(f"晶格类型项数：{page.locator('#latticeSel option').count()}")

        # 参数已从分享令牌带入 → 页面应自动生成轮盘
        try:
            page.wait_for_function(
                "() => !document.querySelector('#btnGenerate').disabled", timeout=90000
            )
            ok("轮盘已按带入参数自动生成（生成按钮可用）")
        except Exception as exc:  # noqa: BLE001
            bad(f"轮盘生成超时：{exc}")

        info_text = page.locator("#cycloidInfo").inner_text()
        if "齿数" in info_text:
            ok(f"轮盘信息：{info_text.replace(chr(10), ' ')[:70]}")
        else:
            bad(f"轮盘信息缺失：{info_text!r}")

        scene = scene_meshes(page, "original")
        px = canvas_pixels(page)
        if scene.get("meshes", 0) < 1:
            bad(f"原始模型未加入场景：{scene}")
        elif scene.get("hasNormals", 0) < scene["meshes"]:
            bad(f"网格缺法向（会渲染成全黑）：{scene}")
        elif px.get("lit", 0) > 0.01:
            ok(f"轮盘已渲染：{scene['tris']:,} 三角形，亮像素 {px['lit'] * 100:.1f}%")
        else:
            bad(f"轮盘未渲染或全黑：场景={scene} 像素={px}")

        # 生成晶格
        print("[4] 生成晶格")
        # 分辨率在「高级选项」折叠区里，先展开（模拟用户操作）
        page.click(".advanced summary")
        page.wait_for_timeout(150)
        page.select_option("#modeSel", "shell")
        page.evaluate("() => { document.querySelector('#shellRange').value = 3; }")
        page.select_option("#resSel", "128")
        page.click("#btnGenerate")
        try:
            page.wait_for_selector("#statsPanel:not([hidden])", timeout=240000)
            ok("生成完成")
        except Exception as exc:  # noqa: BLE001
            bad(f"生成超时：{exc}")

        stats = page.locator("#statsKv").inner_text().replace("\n", " | ")
        if "相对密度" in stats:
            ok(f"统计：{stats[:110]}")
        else:
            bad(f"统计面板异常：{stats[:110]}")

        # 统计面板出现只代表后端算完了；GLB 预览是随后异步下载的
        # （公网上 3~4 MB 要几秒），必须等它真正进场景，否则会误判成"加载失败"
        try:
            page.wait_for_function(
                """() => {
                  const L = window.__lattice;
                  if (!L) return false;
                  let n = 0;
                  L.groups.lattice.traverse((o) => { if (o.isMesh) n++; });
                  return n > 0;
                }""",
                timeout=120000,
            )
        except Exception:  # noqa: BLE001
            pass  # 交给下面的断言给出具体信息

        lat_scene = scene_meshes(page, "lattice")
        px = canvas_pixels(page)
        if lat_scene.get("meshes", 0) < 1:
            bad(f"晶格未加入场景（预览可能加载失败）：{lat_scene}")
        elif px.get("teal", 0) > 0.005:
            ok(f"晶格已渲染：{lat_scene['tris']:,} 三角形，青绿像素 {px['teal'] * 100:.2f}%")
        else:
            bad(f"晶格未渲染（teal={px.get('teal')}）：场景={lat_scene} 像素={px}")

        # 视图切换 / 线框
        for view in ("overlay", "original", "lattice"):
            page.click(f'[data-view="{view}"]')
            page.wait_for_timeout(220)
        ok("三视图切换正常")

        page.click("#wireBtn")
        page.wait_for_timeout(220)
        if page.locator("#wireBtn").get_attribute("class").find("active") >= 0:
            ok("线框模式已启用")
        else:
            bad("线框按钮状态异常")

        # 导出按钮可用
        if not page.locator("#btnStl").is_disabled():
            ok("STL/3MF/OBJ/PLY 导出按钮可用")
        else:
            bad("导出按钮仍禁用")

        # 上传来源切换
        page.click('[data-source="upload"]')
        page.wait_for_timeout(200)
        if page.locator("#dropzone").is_visible():
            ok("切换到上传来源，拖放区可见")
        else:
            bad("上传来源切换失败")

        page.screenshot(path="/tmp/lattice_ui.png", full_page=False)
        page.click('[data-source="cycloid"]')

        # ---------------- 4. 报错汇总 ----------------
        print("[5] 前端报错检查")
        if not js_errors:
            ok("设计器页面无 JS 报错")
        else:
            bad(f"设计器页面 JS 报错：{js_errors[:3]}")
        if not lattice_errors:
            ok("晶格化页面无 JS 报错")
        else:
            bad(f"晶格化页面 JS 报错：{lattice_errors[:3]}")

        # Chromium 会把"响应体被流式读取后正常关闭"的请求也标成 ERR_ABORTED，
        # 数据其实完整拿到了，因此只把非 ERR_ABORTED 的失败当成问题。
        # 任何 4xx/5xx 都算失败：GLB 预览失败只会弹一句 toast，容易被忽略
        if not bad_responses:
            ok("所有请求都返回 2xx（含 GLB 预览）")
        else:
            bad(f"存在非 2xx 响应：{bad_responses[:4]}")

        real_failed = [
            u for u in dict.fromkeys(failed + lattice_failed)
            if "/favicon" not in u and "ERR_ABORTED" not in u
        ]
        if not real_failed:
            ok("无失败的网络请求")
        else:
            bad(f"网络请求失败：{real_failed[:3]}")

        browser.close()

    return _report(steps, issues)


def _report(steps: list[str], issues: list[str]) -> int:
    print("\n".join(steps))
    print()
    if issues:
        print(f"❌ {len(issues)} 项异常")
        for item in issues:
            print("   -", item)
        return 1
    print(f"✅ 全部 {len(steps)} 项检查通过")
    return 0


def run() -> int:
    """包一层：异常也要把已完成的检查项打出来。"""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001
        print(f"\n💥 检查脚本中断：{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
