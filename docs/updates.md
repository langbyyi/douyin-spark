# 程序自动更新

支持 Windows、macOS、Linux 原生 Python 部署，以及宿主机运行现有 Compose 服务的 Docker 部署。
更新源固定为官方仓库 `main` 分支，按完整 Git 提交编号识别版本，不依赖发布标签或软件版本号变化。
管理器启动时检查一次，之后每 6 小时检查；面板也可手动检查。
有更新时在「设置 → 程序更新」点击「更新并重启」。不会定时自动安装。

普通启动方式通过 GitHub 公共 API 检查；共享出口的匿名配额耗尽时会显示限流提示，稍后重试即可。
宿主更新管理器通过 Git 拉取检查，使用独立的 Git 访问链路。

## 首次启用

先完成原有部署并确认面板可运行，再停止原面板，切换为以下启动方式。
管理器是前台常驻进程：关闭它会停止它管理的面板。保持终端运行，或使用原有的进程管理方式托管。
原生包、外部 systemd 服务及管理器应只有一个启动入口，避免重复启动面板。

### Windows 原生

在项目目录的 PowerShell 中运行：

```powershell
python deploy/update.py native
```

默认使用项目 `.venv` 的 Python、项目 `config.toml`，监听 `127.0.0.1:8765`。
数据路径从原配置及环境解析，也可明确指定。使用自定义参数时：

```powershell
python deploy/update.py native --python C:\Python312\python.exe --config D:\spark\config.toml --data D:\spark\data --port 8766 --extras gui mcp
```

含空格的路径使用引号。辅助子进程不创建额外终端窗口。

### Linux / macOS 原生

先按项目指南完成 Python 环境与浏览器安装，再运行：

```bash
python3 deploy/update.py native
```

需要 Python 3.11+ 和 Git。管理器使用项目 `.venv/bin/python` 启动面板。
自定义环境、路径和端口使用 `--python`、`--config`、`--data`、`--host`、`--port` 指定。
安装更新时始终保留 GUI 组件，默认根据原解释器是否安装 MCP 保留 MCP；
如有明确组件需求，传入 `--extras gui mcp` 或 `--extras gui mcp dev`。
配置使用 Chromium 时会在新环境安装匹配的 Chromium；也可指定 `--browser chromium`。
Chrome、Edge 或自定义浏览器路径继续沿用配置。
浏览器、虚拟显示和系统依赖仍按原生部署指南准备，更新器不修改系统依赖。

### Docker

宿主机需要 Git、**Python 3.11+**、Docker Engine 和 Docker Compose。
管理器仅用 Python 标准库，不向宿主 Python 安装项目依赖。
容器的网络和架构要求仍以原部署为准：Linux host 网络的 `local` 服务不会因此兼容其他系统。

先按原快速部署完成配置与目录初始化，再停止当前服务并切换启动方式：

```bash
docker compose stop local
python3 deploy/update.py docker --service local
```

HTTPS 反向代理部署将两处 `local` 替换为 `spark`。
管理器沿用 Compose 项目的挂载、环境、构建参数、网络和端口，只替换所选服务的镜像，
增加实例标识与所有权标签，不启动其他服务。数据必须绑定到容器 `/data`。
若更改宿主数据目录，通过 `--data` 指定与 Compose 绑定一致的路径。

宿主用户须具备已有 Docker 权限及数据目录读写权限。快速部署的目录归容器 UID 1000 所有；
若原部署以 root 管理，可继续使用相同管理用户。不要为启用更新给 GUI 容器挂载 Docker socket。

默认从宿主访问 `http://127.0.0.1:8765/auth/login` 检查服务。
自定义端口或只能通过代理访问时，可指定：

```bash
python3 deploy/update.py docker --service spark --health-url https://spark.example.com/auth/login
```

健康检查核对提交、管理器实例及本次启动随机标识，不会把旧服务或端口占用误报为成功，
也不需要面板密码。直接访问受 HTTPS 保护的后端返回 426 时，三个标识匹配也可确认进程就绪。

## 更新行为

1. 校验管理器心跳、目标提交及任务状态。任务运行中或状态待核对时，拒绝本次更新。
2. 进入维护，阻止 GUI、MCP、CLI 和每日计划启动任务或修改配置。
3. 校验官方上游及提交关系，从目标提交导出独立目录；不拉取、重置或覆盖用户源码工作区。
4. 原生部署在独立虚拟环境安装程序；Docker 构建独立镜像。旧面板仍可查询状态。
5. 停止旧面板，启动并检查新版本。通过后记录当前版本，解除维护。
6. 浏览器需重新登录面板；抖音账号登录状态保留。

原目录或镜像保留，管理器之后按当前版本记录启动，无需手动切换版本目录。
源码工作区的 Git HEAD 保持原样，面板显示实际运行的程序提交。
本地源码改动、未跟踪源码、非官方仓库或不属于官方后续历史的提交会阻止更新，请自行保存并处理。
Git 忽略的正常配置、凭据和数据不阻止更新。
本功能提交并进入官方上游后，才能用它安装该提交后续的更新。

## 日志与恢复

面板请求和公开进度保存在数据目录 `updater/`；权威事务、版本目录、Compose 配置及服务所有权
保存在源码目录 `.spark-updater/`，该目录仅供宿主管理器使用，不挂载给容器。
原生面板日志为 `.spark-updater/service.log`，Docker 日志沿用
`docker compose logs local` 或 `docker compose logs spark`。
管理器错误显示在其终端；子命令错误仅显示命令名和退出码，避免泄露环境凭据。

准备失败时旧服务继续运行。切换失败时尝试停止候选程序并恢复记录中的旧版。
恢复成功显示“已恢复原版本”；恢复失败保留维护，不显示成功。
被中断的更新会在下次使用相同参数启动管理器时核对事务并尝试恢复旧版。

排查步骤：

1. 保留 `.spark-updater/`、数据目录 `updater/`、配置和数据，查看管理器终端、`service.log` 或容器日志。
2. 处理空间、权限、网络、端口占用等原因，再按原命令启动管理器。
3. 仍失败时保持任务暂停，核对 `.spark-updater/transaction.json` 的阶段、`previous` 与 `current.json`。
   不要直接删除 `maintenance.json` 绕过恢复，避免状态未确认时恢复发送。

`.spark-updater/releases/` 和 `dyspark:update-*` 镜像不自动清理，以供恢复。
空间不足时停止服务后核对当前及上一个版本，仅清理确定不再使用的历史版本。
更新器不删除、覆盖或回滚业务数据，也不执行不可逆数据迁移；未来需要迁移的版本应按其单独说明处理。

无源码仓库的独立 wheel、其他编排系统和直接运行 `spark gui` 的环境仅提供自动检查；
面板一键安装需要按上文接入有明确服务所有权的宿主管理器。

## 可重复的端到端验证

在已安装项目依赖的环境中，从源码根目录运行：

```bash
PYTHONPATH=src python tests/verify_update_e2e.py native
PYTHONPATH=src python tests/verify_update_e2e.py docker --service local
PYTHONPATH=src python tests/verify_update_e2e.py docker --service spark
```

脚本只在新建的临时目录创建源码快照、本地 Git 远端、配置和数据，使用随机回环端口。
它先通过真实面板 API 安装健康版本，再安装一个故意启动失败的版本，验证回滚和文件哈希保持不变。
Docker 模式需要当前用户有 Docker 权限，会构建独立镜像并创建名称随机的测试 Compose 项目，结束时停止测试服务。
`spark` 测试用精确的 Docker bridge 网关模拟可信代理头，不替代 HTTPS 反向代理的 TLS 验证。
测试目录、镜像和日志保留供核对；脚本输出的 `base` 路径包含 `result.json`。
