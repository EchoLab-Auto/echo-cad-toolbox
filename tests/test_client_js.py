"""浏览器端 JS（client/）的测试入口：转调 Node 脚本，没有 node 就跳过。

`app/static/lattice/client/` 下的体素化 / STL 解析 / Worker 契约都是纯 JS，
没法用 pytest 直接测，所以用 Node 跑 ``tools/test-client.mjs``（断言写在脚本里，
失败时非 0 退出）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test-client.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没装 node，跳过浏览器端 JS 测试")
def test_client_js() -> None:
    """STL 解析、体素化（解析解校验）、Worker 消息契约。"""
    result = subprocess.run(
        ["node", "--experimental-default-type=module", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, f"浏览器端 JS 测试失败：\n{output}"
    assert "全部通过" in output, output
