"""浏览器端 analysis.js 与后端 ``POST /api/analyze`` 的逐字段对拍。

背景：摆线针轮设计器正在把交互式分析（每次拖滑块都 POST /api/analyze）搬到
浏览器端，前端实现是 ``app/static/cycloid/analysis.js``。本脚本用真实 Chromium
打开设计器页面（``/cycloid``；旧版 ``/`` 或静态入口兜底），对同一组参数分别：

1. ``fetch('/api/analyze', ...)`` 取服务端结果
2. ``import('/static/cycloid/analysis.js')`` 后调用 ``analyze(params)`` 取前端结果
3. 在页面里递归比较两者的每个字段（数值用「绝对 + 相对」双阈值）

判定标准（与工具的用途匹配：交互式预览可以容忍 4 位小数压缩带来的量化误差，
但派生量必须严格一致）：

* 坐标类数组（``profile.actual`` / ``profile.theoretical`` / ``pin_ring.pin_centers``
  / ``output.hole_centers`` / ``contacts.center``）后端已 ``round(x, 4)``，
  容差 ``abs <= 1.5e-4``（允许 1 个末位量化差）
* ``contacts.gaps`` 后端 ``round(x, 6)``，容差 ``abs <= 1.5e-6``
* 其余数值（派生量、布局半径、接触表转角等）``rel <= 1e-6``（另有 1e-9 绝对下限）
* 字符串（告警文案）/ 布尔 / 字段集合必须完全一致；``null`` 与 NaN/Infinity 视为相等
  （Python 的 json 把非有限浮点写成 null）

用法::

    timeout 300 python3 tools/analysis_parity.py
    python3 tools/analysis_parity.py http://127.0.0.1:8099 -v

dev 服务不通时会打印起服务的命令并以退出码 2 结束。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

DEFAULT_URL = "http://127.0.0.1:8099"

DOCKER_HINT = (
    "docker run -d --rm --name cad-dev --user 0:0 -p 8099:8080 "
    '-v "$PWD/app:/app/app" -v /tmp/cad-dev-data:/app/data '
    "echo-cad-toolbox:1.1.0 uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload"
)

# ---------------------------------------------------------------------------
# 用例：覆盖常规参数与各类边界（Zc 两端、K1 附近、负修形、柱销/中心孔自动与显式、
# 采样密度两端、接触表开关……）
# ---------------------------------------------------------------------------

CASES: list[dict] = [
    {
        "name": "default_39x40",
        "params": {},
        "steps": 72,
        "note": "默认参数 + 接触表",
    },
    {
        "name": "zc6_min_spt8",
        "params": {
            "tooth_count": 6,
            "eccentricity": 1.0,
            "pin_circle_radius": 20.0,
            "pin_radius": 2.0,
            "samples_per_tooth": 8,
        },
        "steps": 0,
        "note": "最小齿数 Zc=6 / 最少采样 8 / 无接触表",
    },
    {
        "name": "zc120_max_spt256",
        "params": {
            "tooth_count": 120,
            "eccentricity": 1.0,
            "pin_circle_radius": 150.0,
            "pin_radius": 4.0,
            "samples_per_tooth": 256,
        },
        "steps": 0,
        "note": "最大齿数 Zc=120 / 最密采样 256",
    },
    {
        "name": "k1_0.99_near_cut",
        "params": {"eccentricity": 2.178, "samples_per_tooth": 64},
        "steps": 36,
        "note": "K1 = 0.99（undercut_near + 自交）",
    },
    {
        "name": "k1_1.36_undercut",
        "params": {"eccentricity": 3.0, "samples_per_tooth": 32},
        "steps": 0,
        "note": "K1 > 1，根切 error",
    },
    {
        "name": "k1_exactly_1_cusp",
        "params": {"eccentricity": 2.2, "samples_per_tooth": 48},
        "steps": 0,
        "note": "K1 = 1 精确尖点：cusp + S=0 + 曲率 NaN（JSON null）",
    },
    {
        "name": "negative_modification",
        "params": {
            "equidistant_offset": -0.2,
            "radial_offset": -0.2,
            "rotary_offset_deg": -0.4,
            "samples_per_tooth": 96,
        },
        "steps": 0,
        "note": "三种修形全为负值（齿廓外扩）",
    },
    {
        "name": "positive_modification",
        "params": {
            "equidistant_offset": 0.35,
            "radial_offset": 0.4,
            "rotary_offset_deg": 0.4,
            "samples_per_tooth": 64,
        },
        "steps": 24,
        "note": "三种修形全为正值",
    },
    {
        "name": "low_k1_0.09",
        "params": {"eccentricity": 0.2},
        "steps": 0,
        "note": "K1 < 0.3，low_k1 info",
    },
    {
        "name": "large_pin_selfcut",
        "params": {"pin_radius": 40.0, "samples_per_tooth": 40},
        "steps": 8,
        "note": "大针齿 rp>0.25Rz：large_pin info + 自交",
    },
    {
        "name": "zw_auto_bore_auto",
        "params": {"pin_hole_count": 0, "center_bore_radius": 0.0},
        "steps": 0,
        "note": "柱销数与中心孔全自动",
    },
    {
        "name": "zw_explicit_8",
        "params": {
            "pin_hole_count": 8,
            "pin_hole_circle_radius": 50.0,
            "pin_sleeve_radius": 6.0,
            "clearance": 0.1,
            "center_bore_radius": 20.0,
        },
        "steps": 0,
        "note": "柱销数 / 分布圆 / 柱销套 / 中心孔全显式",
    },
    {
        "name": "zw_2mod4_zc18",
        "params": {
            "tooth_count": 18,
            "eccentricity": 1.0,
            "pin_circle_radius": 50.0,
            "pin_radius": 3.2,
            "pin_hole_count": 0,
            "pin_hole_circle_radius": 40.0,
            "pin_sleeve_radius": 5.0,
        },
        "steps": 0,
        "note": "Zc=18：round(Zc/4)=4.5 检验银行家舍入",
    },
    {
        "name": "zw_2mod4_zc10",
        "params": {
            "tooth_count": 10,
            "eccentricity": 1.2,
            "pin_circle_radius": 30.0,
            "pin_radius": 2.5,
            "pin_hole_count": 0,
        },
        "steps": 0,
        "note": "Zc=10：round(2.5)=2 检验银行家舍入",
    },
    {
        "name": "bore_too_big_thin_rim",
        "params": {"center_bore_radius": 79.0, "samples_per_tooth": 32},
        "steps": 0,
        "note": "中心孔过大：thin_rim / hole_overrun / hole_bore_overlap",
    },
    {
        "name": "zw32_hole_clamp",
        "params": {
            "pin_hole_count": 32,
            "pin_hole_circle_radius": 70.0,
            "pin_sleeve_radius": 12.0,
            "samples_per_tooth": 24,
        },
        "steps": 0,
        "note": "柱销数偏多 → hole_radius 被 hole_max 夹住",
    },
    {
        "name": "ring_wall_small",
        "params": {"ring_wall": 2.0, "samples_per_tooth": 8},
        "steps": 0,
        "note": "壳体壁厚下限 / 采样下限",
    },
    {
        "name": "contact_steps_1440",
        "params": {
            "tooth_count": 11,
            "eccentricity": 1.86,
            "pin_circle_radius": 42.0,
            "pin_radius": 4.5,
            "samples_per_tooth": 24,
        },
        "steps": 1440,
        "note": "接触表步数上限 1440（紧凑型 11/12）",
    },
]

# ---------------------------------------------------------------------------
# 页面内脚本：取服务端结果 + 前端结果 + 递归比较
# ---------------------------------------------------------------------------

PAGE_JS = r"""
async (payload) => {
  const COORD_FIELDS = [
    'profile.actual',
    'profile.theoretical',
    'pin_ring.pin_centers',
    'output.hole_centers',
    'contacts.center',
  ];
  const QUANT6_FIELDS = ['contacts.gaps'];

  function fieldOf(path) {
    return path.replace(/\[\d+\]/g, '');
  }

  function policy(path) {
    const f = fieldOf(path);
    if (COORD_FIELDS.indexOf(f) >= 0) return { atol: 1.5e-4, rtol: 1e-6, kind: 'coord' };
    if (QUANT6_FIELDS.indexOf(f) >= 0) return { atol: 1.5e-6, rtol: 1e-9, kind: 'quant6' };
    return { atol: 1e-9, rtol: 1e-6, kind: 'strict' };
  }

  function kindOf(v) {
    if (v === null) return 'null';
    if (Array.isArray(v)) return 'array';
    if (typeof v === 'number') return 'number';
    if (typeof v === 'string') return 'string';
    if (typeof v === 'boolean') return 'boolean';
    if (typeof v === 'object') return 'object';
    return typeof v;
  }

  function compare(server, client) {
    // 接触表的 pin_indices 是「间隙最小的 floor(Zp/2) 个针齿」这一秩选择。
    // 纯等距修形时全部针齿的法向间隙在数学上完全相等（都等于 Δrp），此时
    // 后端 np.argsort（quicksort，不稳定）挑哪几个针齿取决于浮点末位噪声，
    // 无法逐位复现。因此这两个数组按「所选间隙的多重集」比较（语义等价），
    // 其余字段一律严格比较。
    const sGaps = server.contacts && server.contacts.gaps ? server.contacts.gaps : null;
    const cGaps = client.contacts && client.contacts.gaps ? client.contacts.gaps : null;
    let tieSwaps = 0;

    const st = {
      leaves: 0,
      fields: new Set(),
      maxRel: 0,
      maxRelPath: '',
      maxAbs: 0,
      maxAbsPath: '',
      quantAbs: 0,
      quantPath: '',
      mismatches: [],
      mismatchCount: 0,
    };

    function bad(msg) {
      st.mismatchCount += 1;
      if (st.mismatches.length < 8) st.mismatches.push(msg);
    }

    function numCompare(a, b, path) {
      const pol = policy(path);
      const finiteA = Number.isFinite(a);
      const finiteB = Number.isFinite(b);
      if (!finiteA || !finiteB) {
        const same = (Number.isNaN(a) && Number.isNaN(b)) || a === b;
        if (!same) bad(`${path}: 非有限值不同 server=${a} client=${b}`);
        return;
      }
      const abs = Math.abs(a - b);
      const scale = Math.max(Math.abs(a), Math.abs(b));
      const rel = scale > 0 ? abs / scale : 0;
      if (pol.kind === 'strict') {
        if (rel > st.maxRel) {
          st.maxRel = rel;
          st.maxRelPath = path;
        }
        if (abs > pol.atol + pol.rtol * scale) {
          bad(`${path}: server=${a} client=${b} abs=${abs.toExponential(3)} rel=${rel.toExponential(3)}`);
        }
      } else {
        if (abs > st.quantAbs) {
          st.quantAbs = abs;
          st.quantPath = path;
        }
        if (abs > pol.atol + pol.rtol * scale) {
          bad(`${path}: server=${a} client=${b} abs=${abs.toExponential(3)}（量化容差 ${pol.atol}）`);
        }
      }
      if (abs > st.maxAbs) {
        st.maxAbs = abs;
        st.maxAbsPath = path;
      }
    }

    function sameIntArray(a, b) {
      if (a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
      return true;
    }

    function checkPinIndices(a, b, path) {
      if (a.length !== b.length) {
        bad(`${path}: 步数不同 server=${a.length} client=${b.length}`);
        return;
      }
      for (let k = 0; k < a.length; k += 1) {
        const sa = a[k];
        const sb = b[k];
        if (sa.length !== sb.length) {
          bad(`${path}[${k}]: 选中针齿数不同 server=${sa.length} client=${sb.length}`);
          continue;
        }
        if (sameIntArray(sa, sb)) continue;
        tieSwaps += 1;
        if (!sGaps || !cGaps) {
          bad(`${path}[${k}]: 针齿集合不同（缺少 gaps 无法判定并列）`);
          continue;
        }
        const gs = sa.map((i) => sGaps[k][i]).sort((x, y) => x - y);
        const gc = sb.map((i) => cGaps[k][i]).sort((x, y) => x - y);
        for (let j = 0; j < gs.length; j += 1) {
          const abs = Math.abs(gs[j] - gc[j]);
          const scale = Math.max(Math.abs(gs[j]), Math.abs(gc[j]));
          if (abs > 1.5e-6 + 1e-9 * scale) {
            bad(
              `${path}[${k}]: 所选间隙多重集不同（第 ${j} 小 gap: server=${gs[j]} client=${gc[j]}）`,
            );
            break;
          }
        }
      }
    }

    function walk(a, b, path) {
      st.leaves += 1;
      if (path) st.fields.add(fieldOf(path));
      if (path === 'contacts.pin_indices') {
        if (Array.isArray(a) && Array.isArray(b)) checkPinIndices(a, b, path);
        else bad(`${path}: 类型不同`);
        return;
      }

      // JSON 里非有限浮点写成 null：null ↔ NaN/±Infinity 视为相等
      if (a === null && typeof b === 'number' && !Number.isFinite(b)) return;
      if (b === null && typeof a === 'number' && !Number.isFinite(a)) return;
      if (a === null || b === null) {
        if (a === b) return;
        bad(`${path || '<root>'}: 类型不同 server=${kindOf(a)} client=${kindOf(b)}`);
        return;
      }

      const ta = kindOf(a);
      const tb = kindOf(b);
      if (ta === 'number' && (tb === 'number' || tb === 'boolean')) {
        if (tb !== 'number') {
          bad(`${path || '<root>'}: 类型不同 server=number client=${tb}`);
          return;
        }
        numCompare(a, b, path);
        return;
      }
      if (ta === 'boolean' || tb === 'boolean') {
        if (a !== b) bad(`${path || '<root>'}: 布尔不同 server=${a} client=${b}`);
        return;
      }
      if (ta === 'string' || tb === 'string') {
        if (a !== b) bad(`${path || '<root>'}: 字符串不同 server=${JSON.stringify(a)} client=${JSON.stringify(b)}`);
        return;
      }
      if (ta === 'array' && tb === 'array') {
        if (a.length !== b.length) {
          bad(`${path || '<root>'}: 数组长度不同 server=${a.length} client=${b.length}`);
          return;
        }
        for (let i = 0; i < a.length; i += 1) walk(a[i], b[i], `${path}[${i}]`);
        return;
      }
      if (ta === 'object' && tb === 'object') {
        const ka = Object.keys(a);
        const kb = Object.keys(b);
        const missing = ka.filter((k) => kb.indexOf(k) < 0);
        const extra = kb.filter((k) => ka.indexOf(k) < 0);
        if (missing.length) bad(`${path || '<root>'}: 缺少字段 ${missing.join(',')}`);
        if (extra.length) bad(`${path || '<root>'}: 多出字段 ${extra.join(',')}`);
        for (const k of ka) {
          if (kb.indexOf(k) >= 0) walk(a[k], b[k], path ? `${path}.${k}` : k);
        }
        return;
      }
      bad(`${path || '<root>'}: 类型不同 server=${ta} client=${tb}`);
    }

    walk(server, client, '');
    return {
      ok: st.mismatchCount === 0,
      leaves: st.leaves,
      fields: Array.from(st.fields).sort(),
      maxRel: st.maxRel,
      maxRelPath: st.maxRelPath,
      maxAbs: st.maxAbs,
      maxAbsPath: st.maxAbsPath,
      quantAbs: st.quantAbs,
      quantPath: st.quantPath,
      mismatchCount: st.mismatchCount,
      mismatches: st.mismatches,
      tieSwaps,
    };
  }

  const tImport = performance.now();
  const mod = await import(payload.moduleUrl);
  const importMs = performance.now() - tImport;

  const results = [];
  for (const c of payload.cases) {
    const rec = { name: c.name, steps: c.steps };
    let server;
    const tServer = performance.now();
    try {
      const resp = await fetch(payload.analyzeUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ params: c.params, contact_steps: c.steps }),
      });
      if (!resp.ok) {
        rec.error = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 300)}`;
        results.push(rec);
        continue;
      }
      server = await resp.json();
    } catch (e) {
      rec.error = 'fetch 失败: ' + (e && e.message ? e.message : String(e));
      results.push(rec);
      continue;
    }
    rec.serverMs = performance.now() - tServer;

    let client;
    const tClient = performance.now();
    try {
      client = mod.analyze(c.params, { contactSteps: c.steps });
    } catch (e) {
      rec.error = 'analyze 抛错: ' + (e && e.stack ? e.stack : String(e));
      results.push(rec);
      continue;
    }
    rec.clientMs = performance.now() - tClient;
    rec.cmp = compare(server, client);
    results.push(rec);
  }

  return { importMs, moduleUrl: payload.moduleUrl, results };
}
"""


# ---------------------------------------------------------------------------
# Python 侧：探活 / 打印
# ---------------------------------------------------------------------------


def healthz(base: str) -> bool:
    """dev 服务是否可用（/api/health 返回 200）。"""
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def fmt(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.1e}"


TABLE_HEAD = (
    f"{'case':<26}{'steps':>6}{'values':>9}{'max_rel':>11}  {'max_rel field':<30}"
    f"{'coord|d|':>10}{'bad':>5}{'srv_ms':>8}{'js_ms':>8}"
)


def build_row(rec: dict) -> dict:
    """把页面返回的对比结果压成一行表格数据。"""
    row = {
        "name": rec["name"],
        "steps": rec.get("steps", 0),
        "ok": False,
        "values": 0,
        "max_rel": "-",
        "field": "-",
        "coord": "-",
        "bad": 0,
        "srv_ms": "-",
        "js_ms": "-",
        "error": rec.get("error"),
        "details": [],
    }
    if rec.get("error"):
        return row

    cmp = rec["cmp"]
    row.update(
        ok=bool(cmp["ok"]),
        values=cmp["leaves"],
        max_rel=fmt(cmp["maxRel"]),
        field=cmp["maxRelPath"] or "-",
        coord=fmt(cmp["quantAbs"]),
        bad=cmp["mismatchCount"],
        srv_ms=f"{rec.get('serverMs', 0.0):.0f}",
        js_ms=f"{rec.get('clientMs', 0.0):.1f}",
        details=list(cmp["mismatches"]),
        tie_swaps=cmp.get("tieSwaps", 0),
    )
    return row


def print_table(rows: list[dict]) -> None:
    print(TABLE_HEAD)
    print("-" * len(TABLE_HEAD))
    for row in rows:
        if row["error"]:
            print(f"{'FAIL':<6}{row['name']:<20}{row['steps']:>6}   错误：{row['error'][:80]}")
            continue
        flag = "PASS" if row["ok"] else "FAIL"
        print(
            f"{flag:<6}{row['name']:<20}{row['steps']:>6}{row['values']:>9}{row['max_rel']:>11}  "
            f"{row['field'][:30]:<30}{row['coord']:>10}{row['bad']:>5}{row['srv_ms']:>8}{row['js_ms']:>8}"
        )


def main(argv: list[str]) -> int:
    base = DEFAULT_URL
    verbose = False
    for arg in argv[1:]:
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg.startswith("http"):
            base = arg
    base = base.rstrip("/")

    if not healthz(base):
        print(f"× dev 服务 {base} 不可用。先起一个：\n  {DOCKER_HINT}")
        return 2

    payload = {
        "analyzeUrl": f"{base}/api/analyze",
        "moduleUrl": f"{base}/static/cycloid/analysis.js",
        "cases": [{"name": c["name"], "params": c["params"], "steps": c["steps"]} for c in CASES],
    }

    page_errors: list[str] = []
    page_url = base
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        try:
            # 页面只是拿来做同源 fetch 与 import 的宿主：优先打开设计器页（顺便验证
            # 真实页面里能正常 import 本模块），旧版路由 / 与静态入口作为兜底。
            for url in (f"{base}/cycloid", f"{base}/", f"{base}/static/cycloid/index.html"):
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:  # noqa: BLE001 - 单个候选打不开就试下一个
                    print(f"  （{url} 打开失败：{type(exc).__name__}，试下一个入口）")
                    continue
                if resp is not None and resp.status < 400:
                    break
            page_url = page.url
            page.wait_for_timeout(400)
            result = page.evaluate(PAGE_JS, payload)
        finally:
            browser.close()

    print(f"\n页面 {page_url}\n对照 {result['moduleUrl']}\n  ↔  {payload['analyzeUrl']}")
    print(f"模块加载 {result['importMs']:.1f} ms；用例 {len(CASES)} 组\n")

    rows = [build_row(rec) for rec in result["results"]]
    print_table(rows)
    print(
        "\n列说明：max_rel/max_rel field = 严格字段（派生量、输出布局、接触表转角）的最大相对误差及其路径；\n"
        "          coord|d| = 量化数组（齿廓点 / 针齿中心 / 柱销孔中心 / 接触 center4 位、gaps6 位）的最大绝对误差；\n"
        "          bad = 超差条目数；srv_ms/js_ms = 服务端 fetch 与浏览器端 analyze 的耗时。"
    )

    tie_total = sum(r.get("tie_swaps", 0) for r in rows if not r["error"])
    if tie_total:
        print(
            f"注：接触表 pin_indices 有 {tie_total} 步出现「间隙完全相等」的并列选择（纯等距修形时全部针齿"
            "间隙都等于 Δrp），已按所选间隙的多重集比较（详见脚本内注释）。"
        )

    failures = [r for r in rows if r["error"] or not r["ok"]]
    if failures:
        print("\n=== 超差详情 ===")
        for row in failures:
            if row["error"]:
                print(f" {row['name']}: {row['error']}")
                continue
            print(f" {row['name']}（{row['bad']} 处）：")
            for msg in row["details"]:
                print(f"   - {msg}")

    if verbose:
        print("\n=== 用例说明 ===")
        for case in CASES:
            print(f" {case['name']:<24} {case.get('note', '')}")

    if page_errors:
        print(f"\n（页面自身 JS 报错 {len(page_errors)} 条，不影响对拍：{page_errors[0][:120]}）")

    passed = len(rows) - len(failures)
    if not failures:
        print(f"\nPASS: {passed}/{len(rows)} 组参数下 analysis.js 与 /api/analyze 完全一致")
        return 0

    print(f"\nFAIL: {len(failures)}/{len(rows)} 组超差")
    return 1


def run() -> int:
    """包一层：异常（比如浏览器起不来）也要给出可读的中文提示。"""
    try:
        return main(sys.argv)
    except Exception as exc:  # noqa: BLE001
        print(f"\n💥 对拍脚本中断：{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
