"""晶格隐式场（lattice implicit fields）—— echo-cad-toolbox 晶格化工具。

所有晶格以 ``<= 0 即材料内部`` 的有符号距离约定输出，单位与世界坐标一致（毫米）。
两类实现：

* **TPMS 型**（gyroid / schwarz_p / diamond）：三周期极小曲面。用归一化梯度
  ``s / |∇s|`` 把函数值换算成长度量纲，因此壁厚参数在任意 cell size 下都均匀、
  可直接以毫米指定。
* **杆型**（sc / bcc / fcc / octet / grid）：把点折叠到单胞的 1/8 象限，再求到
  少量代表线段的最短距离（立方对称性保证折叠无损），因此计算量是 O(1) 常数条
  线段，几百万体素也很快。

坐标约定：晶格以原点为单胞角点、沿各轴周期重复；调用方负责整体平移。
"""

from __future__ import annotations

import numpy as np

TAU = 2.0 * np.pi

#: 杆型晶格：单胞折叠后需要检查的代表线段（归一化 0~0.5，相对单胞尺寸）
_STRUT_SEGMENTS = {
    # 立方棱：三向正交杆
    "sc": (),
    # 体心立方：顶点 → 体心
    "bcc": (((0.0, 0.0, 0.0), (0.5, 0.5, 0.5)),),
    # 面心立方：顶点 → 三个面心（面对角线）
    "fcc": (
        ((0.0, 0.0, 0.0), (0.5, 0.5, 0.0)),
        ((0.0, 0.0, 0.0), (0.5, 0.0, 0.5)),
        ((0.0, 0.0, 0.0), (0.0, 0.5, 0.5)),
    ),
    # octet truss：FCC + 体心杆
    "octet": (
        ((0.0, 0.0, 0.0), (0.5, 0.5, 0.0)),
        ((0.0, 0.0, 0.0), (0.5, 0.0, 0.5)),
        ((0.0, 0.0, 0.0), (0.0, 0.5, 0.5)),
        ((0.0, 0.0, 0.0), (0.5, 0.5, 0.5)),
    ),
}

TPMS_KINDS = ("gyroid", "schwarz_p", "diamond", "iwp")
STRUT_KINDS = ("sc", "grid", "bcc", "fcc", "octet")

#: 每种晶格可用的壁/杆模式
KIND_INFO = {
    "gyroid": {
        "label": "Gyroid（螺旋曲面）",
        "family": "tpms",
        "desc": "各向同性、高比强度的三周期极小曲面，最适合热交换/填充体。",
    },
    "schwarz_p": {
        "label": "Schwarz P（原始曲面）",
        "family": "tpms",
        "desc": "立方对称的简单 TPMS，通孔直、流动阻力小。",
    },
    "diamond": {
        "label": "Diamond（金刚石曲面）",
        "family": "tpms",
        "desc": "刚度最高的经典 TPMS，适合承力填充。",
    },
    "iwp": {
        "label": "IWP（W 型曲面）",
        "family": "tpms",
        "desc": "体积分数调节范围大，常用于鞋中底/缓冲。",
    },
    "sc": {
        "label": "SC 立方杆",
        "family": "strut",
        "desc": "三向正交杆，最易打印，适合做框架。",
    },
    "grid": {
        "label": "Grid 正交网格",
        "family": "strut",
        "desc": "各轴节距可独立设置的矩形网格（配重/加强筋）。",
    },
    "bcc": {
        "label": "BCC 体心立方",
        "family": "strut",
        "desc": "体对角线杆，抗压吸能好。",
    },
    "fcc": {
        "label": "FCC 面心立方",
        "family": "strut",
        "desc": "面对角线杆，剪切刚度高。",
    },
    "octet": {
        "label": "Octet 八面体桁架",
        "family": "strut",
        "desc": "FCC+BCC 复合桁架，比强度接近理论极限。",
    },
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _fold(u: np.ndarray, length: float) -> np.ndarray:
    """把坐标折叠到 [0, length/2]（利用立方对称性）。"""
    return np.minimum(u, length - u)


def _segment_distance(
    px: np.ndarray, py: np.ndarray, pz: np.ndarray,
    ax: float, ay: float, az: float, bx: float, by: float, bz: float,
) -> np.ndarray:
    """点到线段 ab 的距离（向量化）。"""
    dx, dy, dz = bx - ax, by - ay, bz - az
    wx, wy, wz = px - ax, py - ay, pz - az
    seg_len2 = dx * dx + dy * dy + dz * dz
    if seg_len2 <= 1e-12:  # 退化成点
        return np.sqrt(wx * wx + wy * wy + wz * wz)
    t = (wx * dx + wy * dy + wz * dz) / seg_len2
    t = np.clip(t, 0.0, 1.0)
    ex, ey, ez = wx - t * dx, wy - t * dy, wz - t * dz
    return np.sqrt(ex * ex + ey * ey + ez * ez)


# --------------------------------------------------------------------------- #
# TPMS
# --------------------------------------------------------------------------- #
_TPMS_TERMS = {
    # 名称: 四个系数 (a*b*c, a*B*C, A*b*C, A*B*c) —— 见下方 _tpms_value_grad
    "gyroid": "special",
    "schwarz_p": "special",
    "diamond": (1.0, 1.0, 1.0, 1.0),
    "iwp": (2.0, 1.0, 1.0, 1.0),
}


def _tpms_value_grad(kind: str, X, Y, Z, kx, ky, kz):
    """返回 (s, |∇s|)。s 无量纲，|∇s| 单位 1/mm。"""
    if kind == "schwarz_p":
        cx, cy, cz = np.cos(kx * X), np.cos(ky * Y), np.cos(kz * Z)
        sx, sy, sz = np.sin(kx * X), np.sin(ky * Y), np.sin(kz * Z)
        s = cx + cy + cz
        gx, gy, gz = -kx * sx, -ky * sy, -kz * sz
        return s, np.sqrt(gx * gx + gy * gy + gz * gz)

    A, B, C = np.cos(kx * X), np.cos(ky * Y), np.cos(kz * Z)
    a, b, c = np.sin(kx * X), np.sin(ky * Y), np.sin(kz * Z)

    if kind == "gyroid":
        s = a * B + b * C + c * A
        gx = kx * (A * B - c * a)
        gy = ky * (B * C - a * b)
        gz = kz * (C * A - b * c)
        return s, np.sqrt(gx * gx + gy * gy + gz * gz)

    # diamond / iwp：四正弦项组合，系数不同
    w1, w2, w3, w4 = _TPMS_TERMS[kind]
    s = w1 * (a * b * c) + w2 * (a * B * C) + w3 * (A * b * C) + w4 * (A * B * c)
    gx = kx * (
        w1 * (A * b * c) + w2 * (A * B * C) - w3 * (a * b * C) - w4 * (a * B * c)
    )
    gy = ky * (
        w1 * (a * B * c) - w2 * (a * b * C) + w3 * (A * B * C) - w4 * (A * b * c)
    )
    gz = kz * (
        w1 * (a * b * C) - w2 * (a * B * c) - w3 * (A * b * c) + w4 * (A * B * C)
    )
    return s, np.sqrt(gx * gx + gy * gy + gz * gz)


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class LatticeField:
    """晶格隐式场。

    Parameters
    ----------
    kind:
        晶格类型，见 :data:`KIND_INFO`。
    cell:
        单胞尺寸，单位 mm。长度 3 的元组或标量。
    thickness:
        TPMS 为壁厚 (mm)，杆型为杆直径 (mm)。
    wall_mode:
        ``"sheet"``（薄壁，默认）/ ``"solid"``（实体骨架，仅 TPMS）。
    warp:
        域扭曲幅度（0~1，相对单胞尺寸）。让晶格呈现自然的有机形态。
    grad:
        沿 Z 方向的晶格渐变率（0 = 均匀；0.5 表示顶部单胞比底部大 50%）。
    grad_height:
        渐变参考高度（mm，通常是模型 Z 尺寸）。**必须固定**：体场是按 Z 分块
        计算的，若用"当前批次的 z 范围"，各块映射不同会在块边界处把晶格切断。
    offset:
        晶格整体平移（mm），一般用于把单胞角点对齐到模型包围盒角点。
    """

    def __init__(
        self,
        kind: str,
        cell=8.0,
        thickness=1.2,
        wall_mode: str = "sheet",
        warp: float = 0.0,
        grad: float = 0.0,
        grad_height: float | None = None,
        offset=(0.0, 0.0, 0.0),
    ):
        if kind not in KIND_INFO:
            raise ValueError(f"未知晶格类型: {kind}")
        if np.isscalar(cell):
            cell = (float(cell),) * 3
        self.kind = kind
        self.cell = tuple(float(v) for v in cell)
        self.thickness = float(thickness)
        self.wall_mode = wall_mode if wall_mode in ("sheet", "solid") else "sheet"
        self.warp = float(max(0.0, warp))
        self.grad = float(grad)
        self.grad_height = float(grad_height) if grad_height else None
        self.offset = tuple(float(v) for v in offset)
        if min(self.cell) <= 0:
            raise ValueError("cell 尺寸必须为正")

    # -- 公开接口 ---------------------------------------------------------- #
    @property
    def is_tpms(self) -> bool:
        return KIND_INFO[self.kind]["family"] == "tpms"

    def evaluate(self, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
        """返回隐式场值，``<= 0`` 表示晶格材料内部。"""
        x = np.asarray(x, dtype=np.float64) - self.offset[0]
        y = np.asarray(y, dtype=np.float64) - self.offset[1]
        z = np.asarray(z, dtype=np.float64) - self.offset[2]

        if self.warp > 0.0:
            x, y, z = self._warp(x, y, z)
        if abs(self.grad) > 1e-9:
            z = self._graded_z(z)

        if self.is_tpms:
            return self._eval_tpms(x, y, z)
        return self._eval_strut(x, y, z)

    # -- 内部 -------------------------------------------------------------- #
    def _warp(self, x, y, z):
        """缓慢的正弦域扭曲：坐标耦合偏移，幅度按单胞尺寸缩放。"""
        Lx, Ly, Lz = self.cell
        amp = self.warp
        wx = amp * Lx * np.sin(TAU * z / (Lz * 2.0) + 0.7)
        wy = amp * Ly * np.sin(TAU * x / (Lx * 2.0) + 1.9)
        wz = amp * Lz * np.sin(TAU * y / (Ly * 2.0) + 3.1)
        return x + wx, y + wy, z + wz

    def _graded_z(self, z):
        """沿 Z 的非线性映射：单胞尺寸随高度线性变化（dz'/dz = 1 + g·z/h）。

        参考高度 h 必须是**固定的模型高度**，不能取当前批次的 min/max——
        否则分块计算时每块映射不同，场会在块边界处断裂。
        """
        h = self.grad_height if self.grad_height and self.grad_height > 0 else 1.0
        return z + 0.5 * self.grad * z * z / h

    def _eval_tpms(self, x, y, z):
        Lx, Ly, Lz = self.cell
        kx, ky, kz = TAU / Lx, TAU / Ly, TAU / Lz
        s, mag = _tpms_value_grad(self.kind, x, y, z, kx, ky, kz)
        # 归一化：s / |∇s| 近似到零等值面的有符号距离（长度量纲）
        mag = np.maximum(mag, 1e-9)
        dist = s / mag
        half_t = 0.5 * self.thickness
        if self.wall_mode == "solid":
            # 单侧实体骨架：保留零等值面的一侧，材料体积分数约 50%
            return dist - half_t
        # sheet：|s| 小的地方是实体壁（薄壁 TPMS）
        return np.abs(dist) - half_t

    def _eval_strut(self, x, y, z):
        Lx, Ly, Lz = self.cell
        ux, uy, uz = _fold(x % Lx, Lx), _fold(y % Ly, Ly), _fold(z % Lz, Lz)
        radius = 0.5 * self.thickness

        if self.kind in ("sc", "grid"):
            # 三向正交杆：到最近网格线（在另两个轴上）的距离
            d = np.minimum(
                np.minimum(np.hypot(uy, uz), np.hypot(uz, ux)),
                np.hypot(ux, uy),
            )
            return d - radius

        segs = _STRUT_SEGMENTS[self.kind]
        d = None
        for (a, b) in segs:
            seg_d = _segment_distance(
                ux, uy, uz,
                a[0] * Lx, a[1] * Ly, a[2] * Lz,
                b[0] * Lx, b[1] * Ly, b[2] * Lz,
            )
            d = seg_d if d is None else np.minimum(d, seg_d)
        return d - radius


# --------------------------------------------------------------------------- #
# 相对密度估算（解析近似，用于 UI 提示）
# --------------------------------------------------------------------------- #
def estimate_relative_density(kind: str, cell=8.0, thickness=1.2) -> float:
    """估算单胞材料体积分数（系数由体素实测校准，仅用于 UI 提示）。

    TPMS 用 ``1 - exp(-c·t/L)``：薄壁时线性（ρ≈c·t/L），厚壁时平滑趋近 1。
    杆型用比杆长 × 截面积 / 单胞体积，乘重叠折减系数。
    """
    if np.isscalar(cell):
        cell = (float(cell),) * 3
    Lx, Ly, Lz = (float(c) for c in cell)
    L = max((Lx * Ly * Lz) ** (1.0 / 3.0), 1e-6)
    t = float(thickness)
    ratio = t / L
    tpms_c = {"gyroid": 2.90, "schwarz_p": 2.30, "diamond": 3.45, "iwp": 3.55}
    if kind in tpms_c:
        # 薄壁区间近似线性（ρ ≈ c·t/L），系数由体素实测校准；厚壁裁剪到 0.95
        return float(np.clip(tpms_c[kind] * ratio, 0.005, 0.95))
    # 杆型：单位单胞内的杆总长 (mm) / 单胞体积 * 杆截面积
    strut_len_per_cell = {
        "sc": 3.0,
        "grid": 3.0,
        "bcc": 6.93,     # 8 条半体对角线
        "fcc": 8.49,     # 12 条半面对角线
        "octet": 15.42,  # FCC + BCC
    }
    overlap = {"sc": 0.96, "grid": 0.96, "bcc": 0.87, "fcc": 0.88, "octet": 0.90}
    if kind in strut_len_per_cell:
        r = t / 2.0
        rho = strut_len_per_cell[kind] * np.pi * r * r / (L * L) * overlap[kind]
        return float(np.clip(rho, 0.002, 0.97))

    return 0.2
