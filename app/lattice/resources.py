"""运行环境能力探测：容器/主机的内存上限 → 可安全使用的体素分辨率。

晶格化的内存开销几乎全在「体素化 → marching cubes → 抽稀」这条链上，峰值由
**面数**主导（等值面越大，顶点/法向/抽稀的中间数组越大）。

实测（cylinder ⌀36×40 样例、gyroid 内部填充、含 30% 抽稀；`ru_maxrss` 进程高水位）：

===========  ============  ===============  ============
分辨率        体素量级      等值面面数        峰值内存
===========  ============  ===============  ============
96            0.9 M         312 k            ~0.5 GB
128           2.2 M         565 k            ~0.8 GB
160           4.2 M         892 k            ~1.2 GB
===========  ============  ===============  ============

> 早先的版本量到的是「只用摆线设计器 + 小面数模型」的更低水位；上面这组是
> 生成阶段的真实峰值，1 GiB 容器跑 160 会直接在 cgroup 里 OOM。

因此按容器内存上限给出一个保守的可用分辨率，避免用户在内存吃紧的机器上
选了高分辨率、结果把容器 OOM 掉（重启会丢掉全部作业，体验很差）。每个档位
都留了约 30%~50% 的余量给 uvicorn 常驻、GLB 导出与分配器碎片。
"""

from __future__ import annotations

import os
from pathlib import Path

#: (内存上限字节, 允许的最高分辨率)。数值来自上表的实测峰值 + 余量：
#: 1 GiB → 96（峰值 0.5 GB）、2 GiB → 128（0.8 GB）、3 GiB → 160（1.2 GB）。
_LADDER = (
    (1024 * 1024**2, 96),
    (2 * 1024**3, 128),
    (3 * 1024**3, 160),
    (6 * 1024**3, 200),
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
