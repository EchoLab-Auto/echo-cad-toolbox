# edge-router —— 用「域名前缀」把不同子域名分流到本机不同端口

同一台云主机（cloud-server-2 / `114.132.241.46`）上跑着多个服务。用端口号区分太难记，
改成 **子域名前缀**：

```
http://cad.你的域名:2229/      → 127.0.0.1:2228   echo-cad-toolbox（摆线针轮工具）
http://panel.你的域名:2229/    → 127.0.0.1:8080   EchoAgent panel
http://report.你的域名:2229/   → 127.0.0.1:9000   任意新服务，加一条路由即可
```

**只看 Host 的第一段**，所以 base domain 完全不影响路由 —— 换域名、加域名都不用改配置：

| 访问地址 | 提取的前缀 | 后端端口 |
| --- | --- | --- |
| `cad.echo-tech.com:2229` | `cad` | 2228 |
| `cad.whatever.cn:2229` | `cad` | 2228 |
| `cad.114.132.241.46.nip.io:2229` | `cad` | 2228 |
| `CAD.Example.COM:2229` | `cad`（nginx 已转小写） | 2228 |
| `114.132.241.46:2229` | `114`（IP 没有前缀） | → 421 |

## 组成

| 项 | 位置 |
| --- | --- |
| 容器 | `edge-router`（`nginx:alpine`，`--network host`，`restart=unless-stopped`） |
| 主配置 | `/etc/esc/edge/nginx.conf` |
| 路由数据 | `/etc/esc/edge/routes.tsv`（`前缀<TAB>端口<TAB>备注`） |
| 生成配置 | `/etc/esc/edge/routes.conf`（nginx `map`，**勿手改**） |
| HTTPS 层 | `/etc/esc/edge/tls.conf`（默认空文件，机制已预留） |
| 证书目录 | `/etc/ssl/edge/`（容器已挂载） |
| 管理命令 | `/usr/local/bin/edge-route` |

用 host 网络的原因：要直接够到宿主机上**所有**端口 —— 既有 docker 发布的（2228），
也有裸进程监听的（8080 panel、3132 core）。

## 安装

```bash
sudo sh tools/edge-router/install.sh          # 首次安装 / 更新配置
sudo sh tools/edge-router/install.sh --reload # 只重装配置并热加载
```

## 日常操作

```bash
edge-route list                    # 列出路由，并真的探一次后端状态码
edge-route add cad 2228 工具箱      # 新增（同前缀覆盖；同端口可以并存多个前缀）
edge-route add panel 8080 panel
edge-route rm panel                # 删除
edge-route logs 50                 # 最近访问日志（含 host / 命中的前缀 / 后端端口）
edge-route reload                  # 手改 routes.tsv 后重新生成并热加载
```

`add` / `rm` 的执行顺序：① 重新生成 `routes.conf`；② 用**一次性容器**跑 `nginx -t`
校验（校验不过就完全不碰运行中的实例）；③ 通过才 `nginx -s reload` 平滑生效，
失败则重建容器。

`list` 输出示例：

```
前缀             端口   监听     探测   备注
cad              2228   是       200    echo-cad-toolbox
```

## 监听端口与「备案拦截」（实测，2026-10-29）

先说结论：**拦截是看 Host 的，不是看端口的**。同一个未备案域名，走 `:80` 和走
`:2229` 的结果一样 —— 都会被 302 到 `https://dnspod.qcloud.com/static/webblock.html?d=…`。
只有 **HTTPS**（Host 在 TLS 里，中间设备看不到）才不触发。

从 4090 打公网实测矩阵：

| Host | `:80` | `:2229` | 说明 |
| --- | --- | --- | --- |
| `cad.example.com` | 302 → dnspod | 302 → dnspod | 已注册、无备案 → 拦 |
| `cad.114.132.241.46.nip.io` | 302 → dnspod | 302 → dnspod | 同上 |
| `cad.baidu.com` | 200 | 200 | 有备案 → 放行 |
| `cad.echo-demo.com` | 200 | 200 | **域名不解析**，不在备案库 → 放行 |
| `cad.nonexistent-zzz.top` | 200 | 200 | 同上 |

对照实验（TLS 里带未备案 Host，走的是 `esc-tls` 的 2226）：

```bash
curl -sk -D - -o /dev/null -H 'Host: cad.example.com' https://114.132.241.46:2226/
# → HTTP/1.1 401 Unauthorized（Server: nginx）—— 请求到达了本机，没有被拦
```

所以要注意：**用不存在的假域名自测会得到"看起来能用"的假阳性**
（`cad.echo-demo.com` 就是 200）。真实域名一旦能解析、又没备案，就会被拦。

### 那到底怎么用？

| 你的情况 | 建议 |
| --- | --- |
| 域名已 **ICP 备案** 到这台机器 | `http://cad.你的域名/` 直接可用（80/2229 都行） |
| 域名**未备案** | 走 HTTPS：安全组放行 443 → 放证书 → 启用 `tls.conf`，用 `https://cad.你的域名/`（实测 HTTPS 不触发拦截） |
| 只是自己/内网用 | 直接 `http://114.132.241.46:2228/`，或局域网地址，不碰域名 |

### 安全组实测

| 端口 | 结果 |
| --- | --- |
| 80 | 可达 |
| 443 | **未放行**（连接超时，需在腾讯云控制台加规则） |
| 2223 / 2224 / 2226 | 可达（esc-tls） |
| 2228 / 2229 / 8080 / 11010 | 可达 |
| 3333 / 8081 / 8443 / 18080 / 20080 / 2443 / 9010 | 未放行 |

## HTTPS（机制已就绪，未启用）

443 未在安全组放行，所以 `tls.conf` 目前是空文件（nginx 允许空 include）。
对**未备案域名**来说这是唯一能用的访问方式（见上一节实测）。开启步骤：

1. 腾讯云控制台 → 安全组 → 入站放行 **TCP 443**；
2. 证书放到 `/etc/ssl/edge/fullchain.pem` + `/etc/ssl/edge/privkey.pem`
   （通配符证书 `*.你的域名` 最省事，一张覆盖所有前缀）；
3. 取消 `/etc/esc/edge/tls.conf` 里注释掉的 `server { listen 443 ssl; … }`；
4. `edge-route reload`。

> 自签证书只能自测：浏览器会警告，分享出去的链接在别人机器上打不开。

## 几个踩过的坑（改之前先读）

1. **单文件 bind mount + `mv` 替换 = 配置不生效。**
   `edge-route` 用 `tmp → mv` 原子替换 `routes.conf`，但容器若按「单文件」挂载，
   仍指向旧 inode，读到的永远是旧内容（现象：`add` 说成功，访问却 421）。
   所以现在挂的是**目录** `-v /etc/esc/edge:/etc/nginx/edge:ro`，并用
   `-c /etc/nginx/edge/nginx.conf` 指定主配置。
2. **`nginx -s reload` 必须带 `-c`。**
   不带 `-c` 会去读默认的 `/etc/nginx/nginx.conf`，找不到同一个 pid 文件就报错，
   进而误走「重建容器」分支 —— 那一瞬间端口会短暂中断（表现为偶发 000）。
3. **`location` 不能写在 `http {}` 顶层。**
   `error_page 421 = @named` 的命名 location 必须放进 `server {}`，否则
   `"location" directive is not allowed here`。
4. **`proxy_set_header Host $host` 会丢掉端口。**
   用 `$http_host` 才能保留 `:2229`，否则后端生成的绝对链接会指错端口。
5. **别拿"不存在的域名"验证备案拦截。**
   未注册/不解析的 Host 不会被拦，会让人误以为「换个端口就绕过去了」。真实域名
   一解析就拦，且 `:80` 与 `:2229` 行为一致。
6. **`sh` 没有 `local`。**
   函数里 `while read -r pfx port` 会污染调用方的同名变量（曾导致 `add` 成功后
   提示信息打成空串）。循环变量统一加下划线前缀。

## 与 esc-tls 的关系

`esc-tls`（2223 / 2224 / 2226）是 EchoAgent 网关的 TLS 终结层，只服务 `esc-gateway`；
`edge-router` 是通用反代，服务任意本机端口。两者完全独立，各自重启互不影响。

## 排障

```bash
docker ps --filter name=edge-router            # 是否在跑
docker logs --tail 50 edge-router             # 启动/错误日志（已重定向到 stdout）
edge-route list                              # 端口在听吗、后端回什么状态码
curl -s -H 'Host: cad.你的域名' http://127.0.0.1:2229/api/health   # 绕开公网自测
```

| 现象 | 原因 |
| --- | --- |
| `421 no_route` | 前缀没配，或改了 `routes.tsv` 没 `edge-route reload` |
| `502` | 后端端口没在听（看 `list` 的「监听」列）；或客户端侧代理干扰 |
| 公网超时 | 安全组没放行该端口 |
| 302 到 dnspod | 云厂商未备案域名拦截，改用 `:2229` |
