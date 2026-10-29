"""运行环境能力探测：容器/主机的内存上限 → 可安全使用的体素分辨率。

晶格化的内存开销几乎全在「体素化 → marching cubes → 抽稀」这条链上，且与
体素数近似线性。实测（本项目，200 分辨率、190 万面网格、含 30% 抽稀）：

===========  ============  ==========================
分辨率        体素量级      峰值内存（进程 RSS）
===========  ============  ==========================
128           1.0 M         ~250 MB
160           1.6 M         ~380 MB
200           3.0 M         ~830 MB
256           6.2 M         1.4 GB 以上
===========  ============  ==========================

因此按容器内存上限给出一个保守的可用分辨率，避免用户在内存吃紧的机器上
选了高分辨率、结果把容器 OOM 掉（重启会丢掉全部作业，体验很差）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: (内存上限字节, 允许的最高分辨率)
_LADDER = (
    (768 * 1024**2, 128),
    (1536 * 1024**2, 160),
    (3 * 1024**3, 200),
)

#: 内存充足时的上限（与 pipeline.PARAM_LIMITS 一致）
MAX_RESOLUTION_HARD_LIMIT = 320


def _cgroup_memory_limit() -> int | None:
    """读 cgroup 的内存上限（字节）。v2 / v1 都试，取最小值。"""
    limits: list[int] = []
    # cgroup v2
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.high"):
        try:
            raw = Path(p).read_text().strip()
            if raw and raw != "max":
                limits.append(int(raw))
        except (OSError, ValueError):
            pass
    # cgroup v1
    for p in (
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        "/sys/fs/cgroup/memory/memory.soft_limit_in_bytes",
    ):
        try:
            value = int(Path(p).read_text().strip())
            # v1 里"无限制"是一个巨大的数
            if 0 < value < (1 << 60):
                limits.append(value)
        except (OSError, ValueError):
            pass
    if not limits:
        return None
    return min(limits)


def _physical_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def memory_limit() -> int | None:
    """可用内存上限（字节），未探测到返回 None。"""
    limit = _cgroup_memory_limit()
    total = _physical_memory()
    if limit and total:
        return min(limit, total)
    return limit or total


def max_resolution() -> tuple[int, int | None]:
    """返回 (允许的最高分辨率, 探测到的内存上限字节)。"""
    limit = memory_limit()
    if limit is None:
        # 探测失败时按最保守的一档处理，避免把机器压垮
        return 128, None
    for ceiling, resolution in _LADDER:
        if limit <= ceiling:
            return resolution, limit
    return MAX_RESOLUTION_HARD_LIMIT, limit


def default_workers() -> int:
    """并行生成任务的线程数。

    单个任务的峰值可达内存上限的 60~80%（实测 160 分辨率最坏 777 MB / 981 MB），
    两个大任务叠加必然把容器撑爆，所以内存小的机器只跑一个。
    """
    env = os.environ.get("LATTICE_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    limit = memory_limit()
    if limit and limit <= 1536 * 1024**2:
        return 1
    return 2


def summary() -> dict:
    """给前端用的能力说明。"""
    resolution, limit = max_resolution()
    return {
        "memory_limit_mb": round(limit / 1024**2) if limit else None,
        "max_resolution": resolution,
        "workers": default_workers(),
    }
