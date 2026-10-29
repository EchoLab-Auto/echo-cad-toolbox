# echo-cad-toolbox

**摆线针轮轮廓生成与可视化仿真器** —— 一个可部署在 Docker 中的 Web 工具：交互式生成摆线针轮
（RV / 摆线针轮减速器）齿廓、实时运动学仿真、啮合接触分析，并导出**毫米级 DXF 图纸**用于
CNC / 线切割 / 激光加工。

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

访问 <http://localhost:8080>；API 文档在 <http://localhost:8080/docs>。

### 已部署实例

| 位置 | 地址 | 说明 |
| --- | --- | --- |
| cloud-server-2（腾讯云，广州） | <http://114.132.241.46:2228/> | 公网可达，`restart=unless-stopped`，内存上限 512 MiB |
| cloud-server-2（子域名入口） | `http://cad.<你的域名>:2229/` | 由 edge-router 按域名前缀分流，见 [docs/edge-router.md](docs/edge-router.md) |
| 4090 工作站（内网） | <http://192.168.31.35:18080/> | 局域网可达，见下节 |

容器常驻参数（与 `tools/deploy.sh` 一致）：

```bash
docker run -d --name echo-cad-toolbox --restart unless-stopped \
  -p 2228:8080 --memory 512m echo-cad-toolbox:1.0.0
```

> ⚠️ 本服务**没有鉴权**，端口对公网开放意味着任何人都能访问与下载 DXF。
> 仅供内网 / 临时演示；若需长期公网暴露，请在前面加一层 Nginx + Basic Auth 或
> 只允许固定 IP（云安全组）访问。

### 用子域名访问（多服务同机）

同一台机器上服务多了以后，端口号不好记。仓库自带一个轻量反代 `edge-router`，
按 Host 的**第一段**分流，base domain 随意：

```
http://cad.你的域名:2229/    → 127.0.0.1:2228   echo-cad-toolbox
http://panel.你的域名:2229/  → 127.0.0.1:8080   其他服务，加一条路由即可
```

```bash
sudo sh tools/edge-router/install.sh      # 安装/更新反代（nginx 容器，host 网络）
edge-route add cad 2228 工具箱             # 新增一条路由，热加载生效
edge-route list                          # 看路由 + 探测后端状态码
```

DNS 只需加一条解析（`cad` → 这台机器 IP；或用 `*.你的域名` 泛解析一次搞定）。

> ⚠️ **备案**：拦截是按 Host 判定的，与端口无关 —— 未备案的真实域名在 `:80` 和
> `:2229` 上都会被 302 到 `dnspod.qcloud.com/webblock`，只有 HTTPS 不触发（Host 在
> TLS 里）。域名已备案直接用 HTTP；未备案就走 HTTPS（放行 443 + 放证书）。
> 完整实测矩阵与踩坑记录见 **[docs/edge-router.md](docs/edge-router.md)**。

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
│   ├── geometry.py     # 几何核心：参数、齿廓解析式、运动学、接触、诊断
│   ├── dxf_export.py   # DXF 构建（ezdxf，多图层 / 分件排布）
│   ├── svg_export.py   # SVG 预览图
│   ├── api.py          # FastAPI 路由与校验模型
│   ├── main.py         # 应用装配、静态资源、GZip
│   ├── cli.py          # 命令行出图
│   └── static/         # 前端（原生 JS + Canvas，无构建步骤）
├── tests/              # pytest：几何 / API / DXF
├── tools/
│   ├── browser_check.py    # 无头浏览器端到端检查（JS 错误 + 画布像素）
│   ├── lan_check.py        # 局域网可访问性自检（列出可分享地址）
│   ├── deploy.sh           # 服务器一键部署（纯 docker，无 compose 也能跑）
│   └── edge-router/        # 域名前缀 → 端口 的分流反代（nginx + edge-route）
│   └── shoot.py            # 生成 README 配图
├── Dockerfile          # 多阶段构建，非 root 运行，内置 HEALTHCHECK
└── docker-compose.yml
```

---

## 7. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q                     # 39 项单元 / 接口测试

# 端到端（需先启动服务；会捕获 JS 错误并做画布像素统计）
playwright install chromium
python tools/browser_check.py http://127.0.0.1:8080/

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

---

## 9. 许可

MIT
