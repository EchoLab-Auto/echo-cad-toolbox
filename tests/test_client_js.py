"""浏览器端 JS（client/）的测试入口：转调 Node 脚本，没有 node 就跳过。

`app/static/lattice/client/` 下的体素化 / STL 解析 / Worker 契约都是纯 JS，
没法用 pytest 直接测，所以用 Node 跑 ``tools/test-client.mjs``（断言写在脚本里，
失败时非 0 退出）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test-client.mjs"


def _node() -> str | None:
    """找 node：环境变量 NODE 优先（容器里没装时可以指到宿主机上的），再查 PATH。"""
    return os.environ.get("NODE") or shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="没装 node，跳过浏览器端 JS 测试（可设 NODE=/path/to/node）")
def test_client_js() -> None:
    """STL 解析、体素化（解析解校验）、Worker 消息契约。"""
    result = subprocess.run(
        [_node() or "node", "--experimental-default-type=module", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, f"浏览器端 JS 测试失败：\n{output}"
    assert "全部通过" in output, output
