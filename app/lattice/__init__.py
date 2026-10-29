"""晶格化工具：把 STL / STEP 实体转成晶格（点阵）结构并导出。

模块划分：

``implicit``
    晶格隐式场（TPMS 曲面 + 杆系桁架的 SDF 表达式）
``voxel``
    网格 → 体素 SDF → marching cubes 回网格
``meshio``
    几何文件读写、STEP 曲面离散（gmsh 子进程）、导出
``pipeline``
    参数校验 → 场组合 → 网格化 → 统计
``jobs``
    上传 / 体素缓存 / 后台生成 / 产物管理的作业层
``cycloid``
    与摆线针轮设计器衔接：2D 齿廓 → 3D 轮盘实体
``api``
    ``/api/lattice`` 路由
"""

from __future__ import annotations

from .implicit import KIND_INFO, LatticeField, estimate_relative_density

__all__ = ["KIND_INFO", "LatticeField", "estimate_relative_density"]
