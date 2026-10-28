"""局域网可访问性自检：列出本机 IPv4 地址，逐个探测服务健康接口。

用法：
    python3 tools/lan_check.py              # 探测常用端口 8080 / 18080
    python3 tools/lan_check.py 18080        # 只探测指定端口
    python3 tools/lan_check.py 18080 8090

输出即为可分享给他人的地址，例如 http://192.168.31.35:18080/
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_PORTS = (8080, 18080)


def local_ipv4() -> list[tuple[str, str]]:
    """返回 [(网卡名, IPv4)]，跳过 docker 网桥与回环。"""
    out: list[tuple[str, str]] = []
    try:
        raw = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface, cidr = parts[1], parts[3]
            ip = cidr.split("/")[0]
            if iface.startswith(("docker", "br-", "veth")):
                continue
            out.append((iface, ip))
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # 退化方案：解析主机名
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    out.append(("?", ip))
        except OSError:
            pass
    return out


def probe(ip: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    url = f"http://{ip}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            try:
                data = json.loads(resp.read().decode())
            except (ValueError, UnicodeDecodeError):
                return True, "200 (非 JSON)"
            return True, f"{data.get('service', '?')} {data.get('version', '')}".strip()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))


def main(argv: list[str]) -> int:
    ports = [int(a) for a in argv[1:] if a.isdigit()] or list(DEFAULT_PORTS)
    addrs = local_ipv4()
    if not addrs:
        print("未找到任何局域网 IPv4 地址（网线/Wi-Fi 是否连接？）")
        return 1

    print("本机地址：")
    for iface, ip in addrs:
        print(f"  {iface:10s} {ip}")
    print()

    any_ok = False
    for port in ports:
        print(f"端口 {port}:")
        for iface, ip in addrs:
            ok, info = probe(ip, port)
            mark = "✅" if ok else "❌"
            print(f"  {mark} http://{ip}:{port}/  [{iface}]  {info}")
            any_ok = any_ok or ok
        print()

    if not any_ok:
        print("没有端口可达。检查：服务是否监听 0.0.0.0、容器端口映射、防火墙（ufw/firewalld）。")
        return 1
    print("以上 ✅ 地址可直接在局域网其他设备（电脑/手机/平板）浏览器打开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
