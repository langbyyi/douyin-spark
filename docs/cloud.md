# Linux 部署指南

## 服务模型

`spark gui` 提供 Web 控制面板及每日调度服务。计划默认禁用，按配置时区解释执行时间，默认时区为 `Asia/Shanghai`。服务停止时终止调度并取消所属任务、登录服务和浏览器进程；页面关闭不影响服务生命周期。

项目不注册宿主机定时任务，不提供独立 cron 调度服务。

需要自动检查版本与面板一键升级时，使用 [程序更新管理器](updates.md)。
它在宿主机管理原生服务或 Docker 容器的更新、重启和失败恢复；Docker 宿主机需 Python 3.11+。

## 获取源码

```bash
git clone https://github.com/langbyyi/douyin-spark.git
cd douyin-spark
```

后续命令均在项目根目录执行。

## Docker 部署

### 快速部署与访问

Linux 宿主机安装 Docker Engine 和 Docker Compose 后，在项目根目录执行：

```bash
bash deploy/docker/start.sh
```

脚本生成缺失的 `.env` 和 `config/config.toml`，初始化 `config/`、`data/` 权限，构建镜像并启动 `local` 服务。已有配置内容不被覆盖。初始化容器以 root 设置目录及配置文件归属，常驻应用仍以 UID 1000 运行；仅调整挂载目录与配置文件，不递归修改已有账号数据。

`local` 使用 Linux host 网络，GUI 监听宿主回环地址 `127.0.0.1:8765`。同机浏览器访问 `http://127.0.0.1:8765`；初始密码通过 `docker compose logs local` 查询。服务器访问在操作端执行：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

保持 SSH 会话运行，通过操作端浏览器访问 `http://127.0.0.1:8765`。默认面板端口为 8765，SSH 示例采用服务器默认 SSH 端口。服务管理命令：

```bash
docker compose logs -f local
docker compose stop local
docker compose up -d local
```

快速入口使用控制面板同源二维码及短信登录，禁用 noVNC。需要 noVNC 时采用下方 HTTPS bridge 部署。`local` 与 `spark` 共用配置、数据、服务锁及端口，切换部署方式前停止当前服务，不同时启动。

### HTTPS 部署初始化

宿主机要求 Docker Engine 和 Docker Compose。初始化配置和数据目录：

```bash
mkdir -p config data
test -e config/config.toml || cp config.example.toml config/config.toml
test -e .env || cp .env.example .env
sudo chown 1000:1000 data config config/config.toml
sudo chmod 700 data config
sudo chmod 600 .env config/config.toml
docker compose config --quiet
docker compose build
docker compose run --rm spark doctor --feature gui
```

容器运行身份为 UID 1000，持久化挂载为 `./data:/data` 和 `./config:/config`。配置目录需可写，以支持原子文件替换；已有目录内的文件权限亦须与容器运行身份一致。数据目录不可写时，容器入口返回退出码 73。

原生部署的根目录 `config.toml` 应在确认内容后迁移至 `config/config.toml`，保留已存在的目标文件。Docker GUI 和 MCP 使用相同配置；原生 CLI 混合运行时应显式统一配置与数据路径。

单文件绑定挂载出现 `EBUSY` 时，配置写入可降级为原位更新，失去原子替换保证；标准部署应使用目录挂载。

镜像包含 GUI、MCP、Chrome 和 Xvfb。`deploy/docker/browser-policy.json` 使用 `URLBlocklist` 禁止 `bitbrowser:` 外部协议，防止外部应用确认对话框阻断浏览器输入。Dockerfile 中的三处策略复制应保留。

## HTTPS 反向代理

本节适用于 `spark` 服务的 bridge 网络部署。容器控制面板绑定 `0.0.0.0` 并要求 HTTPS 请求。Compose 将 8765、8789 和 6080 绑定于宿主机 `127.0.0.1`；公网入口由 HTTPS 反向代理提供。

| 端口 | 作用 |
| --- | --- |
| 8765 | 控制面板 |
| 8789 | 独立 CLI/MCP 临时登录服务 |
| 6080 | 临时 noVNC 服务 |

GUI 登录使用控制面板同源地址及内部随机端口，不依赖固定 8789。以下 Nginx 配置中的域名和证书路径应替换为实际值：

```nginx
server {
    listen 443 ssl;
    server_name spark.example.com;
    ssl_certificate /etc/letsencrypt/live/spark.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spark.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Port $server_port;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 70s;
    }
}
```

### 转发头与来源校验

代理须覆盖协议、Host、外部端口及客户端 IP 转发头，不透传客户端提供的值。应用仅接受可信原始连接来源的转发信息。

- `Host $host` 与 `X-Forwarded-Port $server_port` 联用，保留非默认外部端口信息。
- `Host $http_host` 可直接保留请求 Host 中的端口；Host 已包含端口时，以该端口为准。
- Host 应使用外部访问地址，不使用上游 `$proxy_host`。
- Origin 校验依据协议、主机名和有效端口；默认端口的显式与省略形式等价。

配置管理工具生成的代理规则亦须满足上述条件。Host 变量语义参见 [Nginx 代理文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header)。Origin 不得通过删除或重写规避校验。

来源不匹配时，日志记录 `origin_rejected`、规范化的 `expected` 和 `received`；无效来源记为 `<invalid>`。该诊断记录不包含密码、Cookie、请求体或 URL 认证信息。

### 可信代理地址

`SPARK_GUI_TRUSTED_PROXIES` 通过 Compose 注入 `FORWARDED_ALLOW_IPS`。应依据代理到容器的实际连接来源配置，不使用通配值 `*`。宿主代理通过 Docker bridge 访问时，可查询网络网关作为诊断依据：

```bash
docker compose create spark
container_id=$(docker compose ps -aq spark)
network_name=$(docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$container_id")
docker network inspect --format '{{range .IPAM.Config}}{{println .Gateway}}{{end}}' "$network_name"
```

默认可信地址为 `127.0.0.1`。实际来源不匹配时，HTTPS 转发头不生效，非回环监听请求返回 HTTP 426。独立容器代理应使用固定且已确认的来源地址；网络重建后重新核对来源。

公网仅开放配置的 HTTPS 端口，后端限制为代理可访问。GUI 使用单进程运行；认证会话与限流状态不在多 worker 或副本之间共享。

## 服务启动与停止

完成 TLS 和可信代理配置后启动 HTTPS 部署服务；切换自快速部署时先执行 `docker compose stop local`：

```bash
docker compose up -d spark
docker compose logs -f spark
```

停止服务：

```bash
docker compose stop spark
```

未配置初始密码时，首次启动日志输出一次随机密码；指定密码使用 `.env` 中的 `SPARK_GUI_PASSWORD`。认证哈希保存在数据目录，后续启动沿用。认证规则参见 [面板认证](usage.md#面板认证)。

账号注册、对象和模板配置完成后，可保存执行时间并启用计划。Compose 使用 `restart: "no"`；容器停止或宿主机重启后，使用 `docker compose up -d spark` 恢复服务，已保存调度配置保持有效。

如需容器退出后自动恢复，请自行为所用面板服务（`local` 或 `spark`）设置重启规则，例如 `unless-stopped`，并保存到部署配置。重启规则由 Docker 或容器管理平台控制，应用不会修改它。只要持久化数据和配置目录保留，容器再次启动后会自动恢复已启用的计划；进程 PID 复用不会阻止恢复，已完成的计划不会重复补跑。

## 通知凭据管理

Compose 根目录 `.env` 注入进程环境；控制面板写入配置目录中的 `config/.env`。进程环境优先，包括已定义的空值。

配置示例默认将通知变量保留为注释，控制面板保存后直接生效。进程环境中已定义的变量会覆盖面板配置；提交值与其不一致时，面板拒绝保存并保留原凭据和通知开关。使用旧示例部署时，需删除或注释根目录 `.env` 中的 `SPARK_SERVERCHAN_KEY=` 和 `SPARK_PUSHPLUS_TOKEN=`，然后重新创建容器。

| 管理方式 | 配置流程 |
| --- | --- |
| 部署侧管理 | 在根目录 `.env` 设置渠道变量；修改后执行 `docker compose up -d --force-recreate local` |
| 控制面板管理 | 移除根目录对应通知变量定义，包括空值行；重建容器后，通过控制面板写入 `config/.env` |

以上命令适用于 Docker 快速部署；HTTPS bridge 部署将服务名 `local` 替换为 `spark`。两种方式应保持明确的配置来源。仅重启既有容器不重新注入修改后的 `env_file`。可信代理等环境参数同样要求容器重建；重建在活动任务结束后执行，持久化挂载内容保留。

独立 MCP 容器的进程环境在其启动时确定。环境参数更新后，由客户端结束并重新启动该会话。

## 登录与手动命令

控制面板登录通过同一 HTTPS 地址执行二维码、短信及状态查询，并要求控制面板认证。独立 CLI/MCP 登录可使用 SSH 本地转发；`user@server` 为目标主机：

```bash
ssh -N -L 8789:127.0.0.1:8789 -L 6080:127.0.0.1:6080 user@server
```

在服务器独立终端执行：

```bash
docker compose stop spark
docker compose run --rm --service-ports -e SPARK_PUBLIC_URL=http://127.0.0.1:8789 spark login main --mode web
docker compose up -d spark
```

`main` 要求已在配置中定义。操作端使用 `http://127.0.0.1:8789/login/<令牌>`，SSH 隧道连接到服务所在宿主机。`SPARK_PUBLIC_URL` 仅控制独立登录入口。

noVNC 要求配置 `SPARK_VNC_PASSWORD` 并使用 `--mode vnc`。网页登录启用 noVNC 还要求有头模式，即 `login.headless = false`，或省略该字段且已配置 VNC 密码。noVNC 通过转发的 6080 访问，登录会话结束后回收。

使用 `--service-ports` 前需停止占用相同端口的容器。控制面板停止期间每日调度停止。独立 MCP 约束参见 [远程登录](mcp.md#独立远程登录)。

无需发布登录端口的单次操作：

```bash
docker compose run --rm spark status
docker compose run --rm spark check --account main
docker compose run --rm spark run --account main --dry-run --now
```

单次命令共享运行锁和账本，不承载每日调度。实际发送使用 `run --now`，执行前确认账号和对象范围。

## 原生 Linux 部署

以下命令要求系统提供 APT 包管理器及 Python 3.11+，安装 GUI、MCP、Chromium 和虚拟显示依赖：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv xvfb xauth
python3 --version  # 运行要求：Python 3.11+
python3 deploy/install.py --extras gui mcp --browser chromium
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/spark doctor --feature gui
xvfb-run -a .venv/bin/spark gui --host 127.0.0.1 --no-open
```

使用 Chromium 时，配置 `browser.channel = "chromium"`；安装器保留已存在的配置。回环监听支持 HTTP，可通过宿主 HTTPS 代理或 SSH 转发访问。非回环监听要求 HTTPS。

迁移历史部署时，应停用原外部定时触发器。安装器不创建、修改或删除宿主机任务；每日调度统一由 `spark gui` 管理。

## 部署验证

```bash
docker compose config --quiet
docker compose exec local spark doctor --feature gui
```

以上命令适用于 Docker 快速部署；HTTPS bridge 部署使用 `docker compose exec spark spark doctor --feature gui`。环境诊断要求目标服务已启动。配置解析、环境诊断、浏览器启动及平台业务结果分别使用对应的验证依据。

[使用指南](usage.md) · [项目首页](../README.md)
