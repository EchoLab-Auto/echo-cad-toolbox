# echo-cad-toolbox

**摆线针轮设计器 + 晶格化工作台** —— 一个可部署在 Docker 中的 Web 工具箱，两个工具共用一套
后端与前端风格：

| 工具 | 路径 | 做什么 |
| --- | --- | --- |
| **摆线针轮设计器** | `/` | 交互式生成摆线针轮（RV / 摆线针轮减速器）齿廓、实时运动学仿真、啮合接触分析，导出**毫米级 DXF 图纸** |
| **晶格化工作台** | `/lattice` | 把 STL / STEP 实体转成**晶格结构**（gyroid / Schwarz P / Diamond 等 TPMS 与 BCC / FCC / Octet 桁架），实时三维预览并导出 STL / 3MF / OBJ / PLY |

两个工具是**打通**的：设计器顶栏的「晶格化 →」会把当前齿廓参数带过去，晶格化工作台直接按参数
生成 3D 摆线轮盘（齿廓拉伸 + 柱销孔 + 中心孔），再做成减重晶格 —— 从齿形设计到轻量化打印件
不需要中间导出建模。

![界面](docs/screenshot.png)

| 紧凑型 11/12（i=11:1） | 大速比 59/60（i=59:1，齿高更小） |
| --- | --- |
| ![紧凑型](docs/screenshot-compact.png) | ![大速比](docs/screenshot-highratio.png) |

```
┌────────────┬──────────────────────────────────┬────────────┐
│  参数面板   │        交互式 2D 视图             │  指标 / 诊断 │
│  基本参数   │  摆线轮 + 针轮 + 柱销 + 接触点     │  传动比     │
│  齿廓修形   │  滚轮缩放 / 拖拽平移 / 播放动画    │  齿顶齿根   │
│  输出机构   │                                  │  告警       │
└────────────┴──────────────────────────────────┴────────────┘
```

---

## 1. 快速开始

### 方式 A：Docker（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8080
```

或直接使用镜像：

```bash
docker build -t echo-cad-toolbox:1.0.0 .
docker run -d -p 8080:8080 --name cad-toolbox echo-cad-toolbox:1.0.0
```

国内网络构建较慢 / 拉不到官方镜像时，可同时指定 PyPI 与基础镜像加速源：

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
docker compose up -d --build
```

默认映射端口 `8080`，可用 `PORT=8090 docker compose up -d` 更换。

> 若宿主机 8080 已被占用，直接换端口即可（容器内始终是 8080）：
> `PORT=18080 docker compose up -d`，或
> `docker run -d --restart unless-stopped -p 18080:8080 --name echo-cad-toolbox echo-cad-toolbox:1.0.0`

### 服务器部署（纯 docker 一键脚本）

没有 `docker compose` 插件的主机（例如云服务器）用 `tools/deploy.sh`，它在上面 `docker run`
的基础上额外做了「同步源码 → 构建 → 替换容器 → 等健康检查 → 打印可访问地址」：

```bash
git clone --depth 1 https://github.com/EchoLab-Auto/echo-cad-toolbox.git /opt/echo-cad-toolbox
cd /opt/echo-cad-toolbox

# 指定端口（例：2228），PyPI 走云厂商内网镜像
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple ./tools/deploy.sh 2228

# 之后更新版本只需（PULL=1 先 git 同步到 origin/main）
PULL=1 PIP_INDEX_URL=... ./tools/deploy.sh 2228
```

> **构建加速的坑**：`PIP_INDEX_URL` 在 Dockerfile 里是 `ARG`，只能通过 `--build-arg`
> 传入。只写 `PIP_INDEX_URL=xxx docker build ...`（环境变量）**不会生效**，pip 仍走
> `pypi.org`，在国内主机上会卡在几 MB 的 wheel 上十几分钟。脚本已处理好这一点。

### 方式 B：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 <http://localhost:8080>（摆线针轮设计器）与 <http://localhost:8080/lattice>（晶格化工作台）；
API 文档在 <http://localhost:8080/docs>（晶格化接口在 `/api/lattice/*`）。

> 晶格化需要 `gmsh` 的 OCC 内核做 STEP 离散，`libgmsh.so` 在动态链接阶段依赖
> OpenGL / X11 / freetype。Linux 桌面版一般已具备；无桌面的服务器需要装：
> `apt install libgl1 libglu1-mesa libx11-6 libxft2 libfontconfig1`（Dockerfile 里已处理）。

### 已部署实例

| 位置 | 地址 | 说明 |
| --- | --- | --- |
| cloud-server-2（腾讯云，广州） | <http://114.132.241.46:2228/> | 公网可达，`restart=unless-stopped`，内存上限 512 MiB |
| cloud-server-2（子域名入口） | `http://cad.<你的域名>:2229/` | 域名前缀分流由 **echo-server-core** 的进程内插件 `esc.edge-router` 提供（见该仓 `docs/EDGE-ROUTER.md`）；本仓不再包含该反代 |
| 4090 工作站（内网） | <http://192.168.31.35:18080/> | 局域网可达，见下节 |

容器常驻参数（与 `tools/deploy.sh` 一致）：

```bash
docker run -d --name echo-cad-toolbox --restart unless-stopped \
  -p 2228:8080 --memory 512m echo-cad-toolbox:1.0.0
```

> ⚠️ 本服务**没有鉴权**，端口对公网开放意味着任何人都能访问与下载 DXF。
> 仅供内网 / 临时演示；若需长期公网暴露，请在前面加一层 Nginx + Basic Auth 或
> 只允许固定 IP（云安全组）访问。

### 局域网 / 内网访问

服务默认监听 `0.0.0.0`，容器端口也发布到所有网卡，因此同网段的其他电脑、手机、平板
可直接用宿主机 IP 打开，无需额外配置：

```bash
# 自动列出本机所有网卡地址并逐个探测（输出即别人能打开的地址）
python3 tools/lan_check.py            # 默认探测 8080 / 18080
python3 tools/lan_check.py 18080      # 指定端口

# 典型输出
#   ✅ http://192.168.31.35:18080/  [enp5s0]  echo-cad-toolbox 1.0.0
#   ✅ http://192.168.31.148:18080/ [wlo1]    echo-cad-toolbox 1.0.0
```

- 手机/平板浏览器直接访问该地址即可，界面在窄屏自动切为「参数 → 画布 → 指标」纵向布局，
  触摸可拖拽平移、双指缩放（或双击复位）。
- 页面上的「分享」按钮复制出的链接带完整主机地址，可直接发到同一内网的其他设备打开。
- 打不开时依次检查：① 服务是否存活（`docker ps` 应显示 `healthy`）；
  ② 宿主机防火墙是否放行端口（Debian/Ubuntu：`sudo ufw allow 18080/tcp`）；
  ③ 路由器是否开启了 AP 隔离 / 访客网络隔离。
- 公网暴露请自行加反向代理与访问控制，本服务无鉴权。

### 命令行批量出图

```bash
python -m app.cli --zc 39 --e 1.43 --rz 88 --rp 5.5 --parts cycloid_disc -o disc.dxf --print-params
python -m app.cli --parts cycloid_disc,pin_ring,output_disc -o parts.dxf   # 三件分件排布
python -m app.cli --parts assembly -o asm.dxf                            # 装配图
```

---

## 2. 功能

| 功能 | 说明 |
| --- | --- |
| **解析齿廓生成** | 短幅外摆线 + 等距偏移，直接给出可加工的闭合轮廓（非折线拟合近似） |
| **三种修形** | 等距修形 Δrp、移距修形 ΔRz、转角修形 —— 用于形成啮合侧隙 |
| **实时运动仿真** | 转臂输入 → 摆线轮低速反向输出，可播放/暂停/单步/拖拽转角 |
| **啮合可视化** | 逐个针齿的接触点实时标出（接触律 τᵢ = ψᵢ + φ/Zc） |
| **输出机构** | 柱销孔 / 柱销套 / 中心孔自动布局，带壁厚校验 |
| **几何诊断** | 根切判据、齿廓自交（星形性）检测、孔壁厚、曲率 |
| **DXF 导出** | 毫米单位 1:1，分层（PROFILE/THEORY/PIN/HOLE/…），单零件或分件排布 |
| **SVG / PNG / JSON** | 矢量预览、界面截图、参数与派生量存档 |
| **分享链接** | 参数编码进 URL，一键复制 |
| **一键晶格化** | 把当前齿廓参数带到晶格化工作台，直接生成减重轮盘 |

### 晶格化工作台（`/lattice`）

| 功能 | 说明 |
| --- | --- |
| **CAD 原生输入** | STEP / STP / IGES 用 gmsh 的 OCC 内核离散（子进程隔离）；STL / OBJ / PLY / 3MF / GLB 直接读；载入后自动去重、修法向、补洞 |
| **摆线轮盘直出** | 只填 4 个摆线参数即可得到带柱销孔与中心孔的 3D 轮盘，省掉「导出 DXF → 建模 → 导出 STL」 |
| **9 种晶格** | gyroid / Schwarz P / Diamond / IWP（TPMS 薄壁或实体骨架）+ SC / Grid / BCC / FCC / Octet（杆系桁架） |
| **4 种应用方式** | 内部填充、仅表层壳内填充、挖减重腔、外扩融合；可叠加「保留原始外表皮」 |
| **毫米级厚度** | TPMS 用归一化梯度 `s/\|∇s\|` 换算真实距离场，壁厚/杆径按 mm 直接给，任意单胞尺寸都均匀 |
| **有机形态** | 域扭曲让晶格走向自然；Z 向渐变让单胞尺寸沿高度连续变化 |
| **参数可控** | 体素分辨率 48 ~ 320、平滑迭代、抽稀比例，面数与精度自己权衡 |
| **三维预览** | 原始 / 晶格 / 叠加三视图、线框、自动旋转、适配视角 |
| **导出** | STL / 3MF / OBJ / PLY，单位毫米，直接进切片软件或 CAE 前处理 |

---

## 3. 数学原理

### 3.1 记号

| 符号 | 含义 |
| --- | --- |
| `Zc` | 摆线轮齿数 |
| `Zp = Zc + 1` | 针齿数 |
| `e` | 偏心距 |
| `Rz` | 针齿中心圆半径 |
| `rp` | 针齿（套）半径 |
| `Δrp` / `ΔRz` | 等距 / 移距修形量 |
| `K1 = e·Zp / Rz` | 短幅系数（**必须 < 1**，工程常用 0.5 ~ 0.75） |

### 3.2 齿廓

理论齿廓是**短幅外摆线**（即针齿中心在摆线轮坐标系中划出的轨迹）：

$$
\begin{aligned}
P(\tau) &= \bigl(R_z'\cos\tau - e\cos(Z_p\tau),\quad R_z'\sin\tau - e\sin(Z_p\tau)\bigr)\\
S(\tau) &= \sqrt{R_z'^2 + (eZ_p)^2 - 2R_z'eZ_p\cos(Z_c\tau)}\\
N(\tau) &= \frac{1}{S}\bigl(R_z'\cos\tau - eZ_p\cos(Z_p\tau),\ R_z'\sin\tau - eZ_p\sin(Z_p\tau)\bigr)
\end{aligned}
$$

实际齿廓为理论齿廓的**内等距曲线**（`N` 为单位外法线）：

$$
Q(\tau) = P(\tau) - (r_p + \Delta r_p)\,N(\tau),\qquad R_z' = R_z - \Delta R_z
$$

### 3.3 运动学

针轮固定、转臂输入时：

$$
C(\varphi) = e\,(\cos\varphi,\ \sin\varphi),\qquad
\theta_c(\varphi) = -\frac{\varphi}{Z_c},\qquad
i = \frac{\omega_{in}}{\omega_{out}} = Z_c
$$

### 3.4 接触律

第 `i` 个针齿（`ψᵢ = 2πi/Zp`）与齿廓的接触点位于

$$
Q_i = Q\!\left(\psi_i + \frac{\varphi}{Z_c}\right)
$$

> 该关系经数值包络校验：任意转角下，齿廓到最近针齿中心的距离恒 `≥ rp`，
> 接触处**恰好等于** `rp`，且零穿透（见 `tests/test_geometry.py`）。

### 3.5 不根切条件

理论齿廓出现尖点（`S → 0`）的条件为 `Rz' = e·Zp`，即

$$
K_1 = \frac{e\,Z_p}{R_z} < 1
$$

---

## 4. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/presets` | 参数预设 |
| `POST` | `/api/analyze` | 轮廓 + 派生量 + 诊断（+ 可选接触表） |
| `POST` | `/api/export/dxf` | 导出 DXF（JSON body） |
| `GET` | `/api/export/dxf` | 导出 DXF（查询参数，便于 `wget`） |
| `POST` | `/api/export/svg` | 导出 SVG |

示例：

```bash
# 分析
curl -s -X POST localhost:8080/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"params":{"tooth_count":39,"eccentricity":1.43,"pin_circle_radius":88,"pin_radius":5.5}}' | jq '.derived'

# 直接下载 DXF
curl -OJ "localhost:8080/api/export/dxf?tooth_count=39&eccentricity=1.43&pin_circle_radius=88&pin_radius=5.5&parts=cycloid_disc"
```

`parts` 取值：`cycloid_disc`（摆线轮）、`pin_ring`（针齿壳）、`output_disc`（输出盘）、
`assembly`（装配图）。同时给出多个非 `assembly` 零件时按 2.6×外径 间距**分件排布**。

---

### 晶格化接口（`/api/lattice/*`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/lattice/health` | 健康检查 |
| GET | `/api/lattice/config` | 晶格类型、应用方式、默认参数与区间、支持格式、摆线轮盘默认值 |
| POST | `/api/lattice/jobs` | `multipart/form-data` 上传 `file`（STL/STEP/IGES/OBJ/PLY/3MF/GLB） |
| POST | `/api/lattice/jobs/from-cycloid` | 传摆线参数直接生成 3D 轮盘并建作业 |
| POST | `/api/lattice/samples/{name}` | 内置样例：cube / sphere / cylinder / bracket / tube |
| GET | `/api/lattice/jobs/{id}` | 作业状态（`status` / `progress` / `stage` / `stats`） |
| POST | `/api/lattice/jobs/{id}/generate` | 传晶格参数，后台生成 |
| GET | `/api/lattice/jobs/{id}/preview/{original,lattice}` | GLB 预览 |
| GET | `/api/lattice/jobs/{id}/download/{stl,3mf,obj,ply}` | 下载产物 |
| DELETE | `/api/lattice/jobs/{id}` | 删除作业 |

作业状态机：`new → preparing（体素化）→ ready → running（生成）→ done | error`。

```bash
# 由摆线参数直接生成轮盘并建作业
curl -s -X POST http://localhost:8080/api/lattice/jobs/from-cycloid \
  -H 'Content-Type: application/json' \
  -d '{"params":{"tooth_count":39,"eccentricity":1.43,"pin_circle_radius":88,"pin_radius":5.5},"width":14}'

# 生成 gyroid 填充（返回的 id 换成上一步的）
curl -s -X POST http://localhost:8080/api/lattice/jobs/<id>/generate \
  -H 'Content-Type: application/json' \
  -d '{"lattice":"gyroid","cell_size":10,"thickness":1.2,"mode":"shell","shell_thickness":3}'
```

---

## 5. DXF 图层

| 图层 | 内容 | 颜色 |
| --- | --- | --- |
| `PROFILE` | 摆线轮实际齿廓（闭合多段线，加工用） | 白 |
| `THEORY` | 理论齿廓（短幅外摆线，参考） | 灰虚线 |
| `BORE` / `HOLE` / `SLEEVE` | 中心孔 / 柱销孔 / 柱销 | 蓝 / 青 / 洋红 |
| `PIN` / `PIN_PITCH` | 针齿圆 / 针齿中心圆 | 绿 / 灰虚线 |
| `RING` | 针齿壳内外圆 | 黄 |
| `CENTER` | 中心线 | 红点划线 |
| `TEXT` | 标题与参数标注 | 白 |

单位：`$INSUNITS = 4`（毫米），比例 1:1。文件头 `custom_vars` 中留档全部参数。

---

## 6. 项目结构

```
echo-cad-toolbox/
├── app/
│   ├── geometry.py     # 摆线几何核心：参数、齿廓解析式、运动学、接触、诊断
│   ├── dxf_export.py   # DXF 构建（ezdxf，多图层 / 分件排布）
│   ├── svg_export.py   # SVG 预览图
│   ├── api.py          # 摆线针轮的 FastAPI 路由与校验模型
│   ├── main.py         # 应用装配：挂载两套路由与静态资源
│   ├── cli.py          # 命令行出图
│   ├── lattice/        # ★ 晶格化工作台（后端）
│   │   ├── implicit.py     #   晶格隐式场：TPMS 解析梯度 / 杆系立方对称折叠
│   │   ├── voxel.py        #   射线体素化 + EDT-SDF + marching cubes
│   │   ├── meshio.py       #   STL/STEP 读写、gmsh 子进程离散、导出
│   │   ├── pipeline.py     #   参数校验 → 场组合 → 网格化 → 统计
│   │   ├── jobs.py         #   作业层：上传 / 体素缓存 / 后台生成 / 产物
│   │   ├── cycloid.py      #   摆线齿廓 → 3D 轮盘实体（两工具衔接点）
│   │   └── api.py          #   /api/lattice 路由
│   └── static/         # 前端（原生 JS，无构建步骤）
│       ├── index.html / app.js / style.css    # 摆线针轮设计器（2D Canvas）
│       └── lattice/                            # 晶格化工作台（three.js）
│           ├── index.html / app.js / style.css
│           └── vendor/three/                   # three.js 本地副本（离线可用）
├── tests/              # pytest：几何 / API / DXF / 晶格化
├── tools/
│   ├── browser_check.py    # 摆线设计器的无头浏览器检查（JS 错误 + 画布像素）
│   ├── lattice_check.py    # 晶格化工作台的无头浏览器检查（含真实生成流程）
│   ├── lan_check.py        # 局域网可访问性自检（列出可分享地址）
│   ├── deploy.sh           # 服务器一键部署（纯 docker，无 compose 也能跑）
│   └── shoot.py            # 生成 README 配图
├── Dockerfile          # 多阶段构建，非 root 运行，内置 HEALTHCHECK
├── docker-compose.yml
└── data/lattice_jobs/  # 晶格化作业产物（容器里挂 data 卷；已在 .gitignore）
```

---

## 7. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q                     # 66 项单元 / 接口测试（含 27 项晶格化）

# 端到端（需先启动服务；会捕获 JS 错误并做画布像素统计）
playwright install chromium
python tools/browser_check.py http://127.0.0.1:8080/      # 摆线设计器
python tools/lattice_check.py http://127.0.0.1:8080/      # 晶格化工作台

# 重新生成 README 配图
python tools/shoot.py http://127.0.0.1:8080/
```

`tests/test_geometry.py` 中最关键的一项验证：把齿廓按运动学变换到世界坐标系后，
齿廓上**任意**一点到最近针齿中心的距离必须 `≥ rp`（零穿透），且存在距离**恰好等于**
`rp` 的接触点；同时齿廓径向极值个数必须等于 `Zc`，极角必须单调（星形性，即无自交）。

---

## 8. 使用提示

- **短幅系数 K1 建议 0.5 ~ 0.75**：过小齿廓接近圆弧、承载差；接近 1 会根切。
- **齿数 Zc 越大**，齿高越小（≈ `2e`）、传动比越大。
- **修形**：先给 `Δrp = 0.01 ~ 0.03 mm` 形成侧隙；若需整体减小外径用 `ΔRz`。
- 导出的 `PROFILE` 图层为闭合多段线，可直接用于 2.5D 轮廓加工；若需 G2/G3 圆弧，
  建议在 CAM 中做「拟合圆弧」。
- 摆线轮与针齿理论上是**多齿同时啮合**，视图中全部接触点都会显示。

### 晶格化怎么调

| 参数 | 建议 |
| --- | --- |
| 单胞尺寸 | 先取零件最小特征尺寸的 1/5 ~ 1/3；偏大撑不住，偏小打印不出来 |
| 壁厚 / 杆径 | ≥ 2 × 体素尺寸；FDM 打印别低于 0.8 mm |
| 分辨率 | 先用 128 ~ 160 排版试错，定稿再上 200 ~ 256（256 约 1 GB 内存） |
| 应用方式 | 承力件用「仅表层壳内填充」保留实心齿面；纯减重用「挖减重腔」 |
| 抽稀 | 默认 30%；只做外观可以拉到 60%，要送 CAE 就设 0 |
| 保留外表皮 | 想让零件「看起来还是原壳、里面是晶格」时打开 |

性能参考（i7 级 CPU，单件）：

| 场景 | 体素化 | 生成 | 面数 |
| --- | --- | --- | --- |
| 立方体 40×30×24 + gyroid（128） | 0.3 s | 2.0 s | 330 k |
| L 支架 + gyroid（144） | 0.5 s | 1.5 s | 200 k |
| 摆线轮盘 Zc=39 + gyroid 壳内填充（128） | 0.6 s | 1.1 s | 98 k |
| STEP 带孔件 + gyroid（160） | 1.0 s | 4.8 s | 640 k |

### 晶格化算法要点

1. **体素化**：对每个 (x, y) 栅格列沿 −Z 发一条射线，收集与全部三角面的交点，排序后按
   **奇偶规则**填充成占据栅格。比逐体素做包含性测试快一个量级，也不依赖拾取引擎的包围体实现。
2. **SDF**：占据栅格做欧氏距离变换（EDT）得到内外距离，带 `0.5·pitch` 的体素中心修正，
   使零等值面落在真实表面位置；再做边界保护（最外 1.6 层强制为外部），保证等值面在体场内闭合。
3. **晶格场**：TPMS 用 `s/\|∇s\|`（解析梯度）把函数值换算成**真实距离**，所以壁厚是毫米直给；
   杆系利用立方对称性把坐标折叠到单胞 1/8 象限，最近杆族收缩成 1~4 条代表线段求距离 —— 计算量
   与单胞尺寸无关，且天然无断口。
4. **组合**：按应用方式对实体 SDF 与晶格场取 `max` / `min`（隐式布尔），比网格布尔稳定得多。
5. **网格化**：marching cubes → Taubin 平滑 → 二次误差抽稀 → 连通分量清理 → 补洞。

---

## 9. 许可

MIT
