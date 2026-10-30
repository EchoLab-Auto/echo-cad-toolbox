"""工具箱三个页面的浏览器端验证（真实 Chromium + WebGPU）。

检查内容：

1. 首页：两张工具卡、在线状态、跳转正确；
2. 摆线针轮设计器（``/cycloid``）：参数面板、画布真的画了东西、**本地分析**
   （顶栏角标显示「本地 xx ms」，且不发 /api/analyze 请求）、改参数后指标变化、
   DXF 导出、以及「晶格化 →」把参数带到工作台；
3. 晶格化工作台（``/lattice``）：WebGPU 引擎探测、样例 → 一次性体素场 →
   **浏览器 GPU 生成**、三维场景里真的有网格、像素统计、视图/线框、导出按钮、
   精度诊断面板、摆线轮对话框；
4. 全程无 JS 报错、无 4xx/5xx 响应。

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

# WebGPU 需要显式打开；headless 下走 ANGLE/Vulkan
CHROME_FLAGS = [
    "--no-sandbox",
    "--enable-unsafe-webgpu",
    "--enable-features=Vulkan",
    "--use-angle=vulkan",
    "--enable-webgl",
]


def canvas_pixels(page, selector: str = "#canvas") -> dict:
    """对三维画布截图并统计像素。

    直接 ``gl.readPixels`` 读到的是已清空的绘制缓冲，所以走截图 —— 也就是用户
    真正看到的画面。
    """
    box = page.locator(selector).bounding_box()
    if not box or box["width"] < 2 or box["height"] < 2:
        return {"error": "canvas 尺寸异常"}
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
            if max(r, g, b) > 55:
                lit += 1
            if g > 80 and g > r * 1.15 and b > r * 1.1:
                teal += 1
            if max(r, g, b) < 20:
                black += 1
    return {
        "w": w, "h": h, "total": total,
        "lit": round(lit / total, 4), "teal": round(teal / total, 4), "dark": round(black / total, 4),
    }


def scene_meshes(page, group: str) -> dict:
    """读 three.js 场景图，确认某个 group 里真的有网格。"""
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
    """摆线设计器用 2D canvas，可以直接读 ImageData。"""
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

    js_errors: list[str] = []
    failed: list[str] = []
    bad_responses: list[str] = []
    api_calls: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=CHROME_FLAGS)
        # https 站点（含自签证书）要放行，否则检查脚本连门都进不去。
        # 注意这不影响 isSecureContext 的判定：scheme 是 https 就够了。
        page = browser.new_page(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=BASE.startswith("https://"),
        )

        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.on("requestfailed", lambda r: failed.append(f"{r.url} ({r.failure})"))
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        page.on("request", lambda r: api_calls.append(r.url) if "/api/analyze" in r.url else None)

        # ------------------------------------------------------------------ #
        print("[1] 工具箱首页")
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(600)

        if "CAD 工具箱" in page.title():
            ok(f"标题：{page.title()}")
        else:
            bad(f"标题异常：{page.title()}")

        cards = page.locator(".tool-card")
        if cards.count() == 2:
            names = [cards.nth(i).locator("h3").inner_text() for i in range(2)]
            hrefs = [cards.nth(i).get_attribute("href") for i in range(2)]
            ok(f"两张工具卡：{names[0]} → {hrefs[0]} / {names[1]} → {hrefs[1]}")
            if hrefs != ["/cycloid", "/lattice"]:
                bad(f"卡片跳转异常：{hrefs}")
        else:
            bad(f"工具卡数量异常：{cards.count()}")

        art = page.evaluate(
            "() => [document.querySelectorAll('#artCycloid svg *').length,"
            " document.querySelectorAll('#artLattice svg path').length]"
        )
        if art[0] > 10 and art[1] >= 3:
            ok(f"卡片插画已绘制（摆线 {art[0]} 个元素 / gyroid {art[1]} 条等值线）")
        else:
            bad(f"卡片插画缺失：{art}")

        status = page.locator("#statusPill").inner_text()
        if "在线" in status:
            ok(f"服务状态：{status}")
        else:
            bad(f"服务状态异常：{status}")

        # ------------------------------------------------------------------ #
        print("[2] 摆线针轮设计器")
        page.goto(f"{BASE}/cycloid", wait_until="networkidle")
        page.wait_for_timeout(900)

        if "摆线针轮" in page.title():
            ok(f"标题：{page.title()}")
        else:
            bad(f"标题异常：{page.title()}")

        if page.locator("#paramsPanel .group").count() >= 3:
            ok(f"参数面板已渲染（{page.locator('#paramsPanel .group').count()} 组）")
        else:
            bad("参数面板未渲染")

        px = canvas_2d_pixels(page)
        if px.get("nonBg", 0) > 0.01:
            ok(f"齿廓视图已绘制：非背景像素 {px['nonBg'] * 100:.1f}%（{px['w']}×{px['h']}）")
        else:
            bad(f"齿廓视图几乎空白：{px}")

        # 本地分析：角标 + 不发请求
        page.wait_for_function(
            "() => (document.querySelector('#perfChip')?.innerText || '').includes('本地')",
            timeout=15000,
        )
        chip = page.locator("#perfChip").inner_text().replace("\n", " ")
        ok(f"分析引擎角标：{chip}")
        if api_calls:
            bad(f"交互路径仍在请求 /api/analyze：{api_calls[:2]}")
        else:
            ok("派生量全部在浏览器本地算（无 /api/analyze 请求）")

        # 改参数 → 指标变化
        before = page.locator("#metricsPanel").inner_text()
        page.evaluate(
            """() => {
              const el = document.querySelector('#paramsPanel input[type=number]');
              el.value = '29';
              el.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(400)
        after = page.locator("#metricsPanel").inner_text()
        if before != after:
            ok("参数改动后指标即时刷新")
        else:
            bad("参数改动后指标没有变化")

        # DXF 导出
        try:
            with page.expect_download(timeout=30000) as dl:
                page.click("#btnDxf")
            ok(f"DXF 已导出：{dl.value.suggested_filename}")
        except Exception as exc:  # noqa: BLE001
            bad(f"DXF 导出失败：{exc}")

        # 「晶格化 →」带参数
        page.click("#btnLattice")
        page.wait_for_load_state("networkidle")
        note = page.locator("#cycloidFromUrl")
        if "/lattice" in page.url and note.is_visible():
            ok(f"参数已带到工作台：{note.inner_text()[:48]}…")
        else:
            bad(f"参数传递失败：url={page.url} note_visible={note.is_visible()}")

        # ------------------------------------------------------------------ #
        print("[3] 晶格化工作台")
        page.wait_for_timeout(500)
        engine = page.evaluate(
            """() => ({mode: window.__lattice?.state?.engineMode,
                       api: !!navigator.gpu,
                       secure: window.isSecureContext,
                       label: document.querySelector('#engineLabel')?.textContent})"""
        )
        ok(f"计算引擎：{engine['label']}（mode={engine['mode']}, navigator.gpu={engine['api']},"
           f" 安全上下文={engine['secure']}）")
        if engine["mode"] == "server" and not engine["secure"]:
            # WebGPU 只在安全上下文（HTTPS / localhost）可用；用 http://公网IP 访问时
            # 浏览器根本不暴露 navigator.gpu，应用会自动回退到服务端生成 —— 这是**预期**
            # 行为，不是缺陷。下面按服务端路径继续验证。
            ok("非安全上下文（http:// 公网地址）→ 按设计回退服务端生成")
        elif engine["api"] and engine["mode"] != "gpu":
            bad("浏览器支持 WebGPU 但没有切到 GPU 引擎")

        # 摆线轮对话框
        page.click("#btnCycloid")
        page.wait_for_timeout(300)
        if page.locator("#cycloidDialog").is_visible():
            kpi = page.locator("#cycloidKpi").inner_text().replace("\n", " ")
            ok(f"摆线轮对话框已打开：{kpi[:60]}")
            page.click(".modal-foot .btn:not(.primary)")
        else:
            bad("摆线轮对话框没有打开")

        # 样例 → 体素场 → 生成
        page.click("[data-sample='cylinder']")
        use_gpu = engine["mode"] == "gpu"

        if use_gpu:
            page.wait_for_function("() => window.__lattice?.state?.volume !== null", timeout=180000)
            vol = page.evaluate(
                """() => { const v = window.__lattice.state.volume;
                           return {shape: v.meta.shape, pitch: +v.meta.pitch.toFixed(3),
                                   mb: +(v.meta.bytes / 1048576).toFixed(1)}; }"""
            )
            ok(f"体素场已就绪：{vol['shape']} · {vol['pitch']} mm · {vol['mb']} MB")

        page.click("#btnGenerate")
        if use_gpu:
            page.wait_for_function("() => window.__lattice?.state?.mesh !== null", timeout=180000)
            page.wait_for_timeout(500)
            gen = page.evaluate(
                """() => { const s = window.__lattice.state;
                           return {faces: s.mesh.stats.faces, ms: Math.round(s.lastMs)}; }"""
            )
            ok(f"GPU 生成完成：{gen['faces']:,} 面，{gen['ms']} ms")
        else:
            # 服务端路径：等作业 done，再等 GLB 预览真正进场景（公网要几秒下载）
            page.wait_for_function(
                "() => window.__lattice?.state?.job?.status === 'done'", timeout=600000
            )
            job_stats = page.evaluate(
                """() => { const st = window.__lattice.state.job?.stats || {};
                           return {faces: st.faces || 0, seconds: st.seconds}; }"""
            )
            ok(f"服务端生成完成：{job_stats['faces']:,} 面，{job_stats['seconds']} s")
            page.wait_for_function(
                """() => { let n = 0;
                             window.__lattice?.groups.lattice.traverse((o) => { if (o.isMesh) n++; });
                             return n > 0; }""",
                timeout=180000,
            )
            gen = job_stats

        stats = page.locator("#statsKv").inner_text().replace("\n", " | ")
        if "相对密度" in stats:
            ok(f"统计：{stats[:100]}")
        else:
            bad(f"统计面板异常：{stats[:100]}")

        lat_scene = scene_meshes(page, "lattice")
        px = canvas_pixels(page)
        if lat_scene.get("meshes", 0) < 1:
            bad(f"晶格未加入场景：{lat_scene}")
        elif px.get("teal", 0) > 0.004:
            ok(f"晶格已渲染：{lat_scene['tris']:,} 三角形，青绿像素 {px['teal'] * 100:.2f}%")
        else:
            bad(f"晶格未渲染（teal={px.get('teal')}）：场景={lat_scene} 像素={px}")

        # 视图切换 / 线框
        for view in ("overlay", "original", "lattice"):
            page.click(f'[data-view="{view}"]')
            page.wait_for_timeout(200)
        ok("三视图切换正常")

        page.click("#wireBtn")
        page.wait_for_timeout(200)
        if "on" in (page.locator("#wireBtn").get_attribute("class") or ""):
            ok("线框模式已启用")
        else:
            bad("线框按钮状态异常")

        # 导出按钮
        if not page.locator("#btnStl").is_disabled():
            ok("STL/3MF/OBJ/PLY 导出按钮可用")
        else:
            bad("导出按钮仍禁用")

        if use_gpu:
            # 客户端导出（不经过服务器）
            sizes = page.evaluate(
                """async () => {
                  const mod = await import('/static/lattice/gpu/exporters.js');
                  const m = window.__lattice.state.mesh;
                  const out = {};
                  for (const fmt of ['stl', 'obj', 'ply', '3mf']) out[fmt] = mod.exportMesh(fmt, m, 'check').size;
                  return out;
                }"""
            )
            if all(v > 84 for v in sizes.values()):
                stl_kb, obj_kb, ply_kb, mf_kb = (round(v / 1024) for v in sizes.values())
                ok(f"浏览器端导出：STL {stl_kb} / OBJ {obj_kb} / PLY {ply_kb} / 3MF {mf_kb} KB")
            else:
                bad(f"浏览器端导出异常：{sizes}")
        else:
            # 服务端路径：验证下载接口真的能出 STL
            stl = page.evaluate(
                """async () => {
                  const id = window.__lattice.state.job.id;
                  const r = await fetch(`/api/lattice/jobs/${id}/download/stl`);
                  const b = await r.arrayBuffer();
                  return {status: r.status, mb: +(b.byteLength / 1048576).toFixed(1)};
                }"""
            )
            if stl["status"] == 200 and stl["mb"] > 0.01:
                ok(f"服务端 STL 下载：{stl['mb']} MB")
            else:
                bad(f"STL 下载异常：{stl}")

        # 精度面板
        if page.locator("#precisionBox").is_visible():
            prec = page.locator("#precisionBox").inner_text().replace("\n", " ")
            ok(f"精度诊断：{prec[:90]}")
        else:
            bad("精度诊断面板没有显示")

        # 调参重算：GPU 模式应立刻重算；服务端模式需手动点生成（预期差异）
        before_faces = gen["faces"]
        page.evaluate("""() => {
          const el = document.querySelector('#cellRange');
          el.value = '6'; el.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        if use_gpu:
            try:
                page.wait_for_function(
                    f"() => (window.__lattice?.state?.mesh?.stats?.faces || 0) !== {before_faces}",
                    timeout=60000,
                )
                ok("改单胞尺寸后 GPU 立刻重算（无需点按钮）")
            except Exception:  # noqa: BLE001
                bad("改参数后没有自动重算")
        else:
            page.wait_for_timeout(600)
            ok("服务端模式：改参数后需手动点「生成晶格」（预期行为）")

        page.screenshot(path="/tmp/lattice_ui.png", full_page=False)

        # ------------------------------------------------------------------ #
        print("[4] 报错汇总")
        if not js_errors:
            ok("三个页面均无 JS 报错")
        else:
            bad(f"JS 报错：{js_errors[:3]}")
        if not bad_responses:
            ok("所有请求都返回 2xx")
        else:
            bad(f"存在非 2xx 响应：{bad_responses[:4]}")
        real_failed = [u for u in dict.fromkeys(failed) if "/favicon" not in u and "ERR_ABORTED" not in u]
        if not real_failed:
            ok("没有失败的请求")
        else:
            bad(f"请求失败：{real_failed[:4]}")

        browser.close()

    print()
    for line in steps:
        print(line)

    print("\n=== 判定 ===")
    if issues:
        print(f"FAIL（{len(issues)} 项）")
        return 1
    print("PASS：首页、摆线设计器（本地分析）、晶格化工作台（GPU 生成 + 导出）全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
