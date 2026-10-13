# MCP 接入指南

## 传输与进程模型

`spark mcp` 使用标准输入输出（stdio）传输，由 MCP 客户端启动和管理进程。MCP 传输不监听网络端口；账号登录可启动临时 HTTP 或 noVNC 服务。浏览器操作和数据持久化均在 MCP 服务主机执行。

MCP 不承载每日调度，该功能由 `spark gui` 提供。耗时操作通过后台进程执行，任务记录位于 `data/jobs/<任务编号>/`。服务退出时取消所属任务及浏览器进程，未达到保留期的历史记录仍可查询。

独立 MCP 进程不挂载调度器，其 `status` 中 `scheduler.running = false` 仅表示该后端未运行调度器。其他 GUI 进程的实时调度信息通过相应控制面板查询。

## 安装与启动验证

Linux 安装命令：

```bash
python3 deploy/install.py --extras gui mcp
.venv/bin/spark doctor --feature mcp
```

Windows 使用 `python` 和 `.venv\Scripts\spark.exe`。项目虚拟环境中可使用 `python -m pip install ".[mcp]"` 安装 MCP 组件；Docker 镜像预装该组件。

全局配置参数置于子命令之前：

```bash
spark --config /path/to/config.toml --data /path/to/data mcp
```

启动配置错误返回退出码 64；依赖不可用或 `mcp.enabled = false` 返回 69。工具调用重新读取配置，运行期间的配置错误通过工具结果报告。

协议数据写入 stdout；日志写入 stderr，并按文件大小轮转至 `data/logs/mcp.log`。客户端应保持协议输出与日志流分离。发送任务应达到任务终态后关闭会话。

MCP 启动开关缺省启用。禁用后拒绝新服务启动，不终止现有会话或 GUI 调度。

## 工具参考

| 工具 | 功能及约束 |
| --- | --- |
| `list_accounts` | 查询账号、对象列表、登录记录及脱敏代理信息 |
| `status` | 查询当日发送、最近运行及任务状态；支持可选 `account` |
| `check` | 后台验证登录、身份和出口 IP，不提交消息；最长等待 55 秒，未完成时返回任务编号 |
| `pause_account` | 停用账号，阻止后续发送；允许在任务执行期间调用 |
| `resume_account` | 显式授权后启用账号，不启用每日计划；要求登录记录，清除缓存健康状态；活动任务或未确认结果阻止启用 |
| `run` | 后台执行单次发送并跳过启动随机延迟；`dry_run=true` 禁止消息提交 |
| `run_status` | 查询 `run`、`check`、`friends` 任务；编号省略时查询三类任务中的最近记录，不包含登录任务 |
| `logs` | 读取运行日志及截图路径；`service="gui"` 或 `"mcp"` 指定常驻服务日志 |
| `fetch_conversations` | 后台读取会话及火花天数，默认不修改发送对象配置；支持 `sparks_only` 筛选 |
| `list_friends` | 查询账号发送对象配置 |
| `add_friend` / `remove_friend` | 修改发送对象配置，保留注释并执行配置校验 |
| `login_start` | 为已配置账号启动登录；返回会话编号、访问链接及二维码；超时参数为 1–60 分钟，省略或 `null` 时采用配置值 |
| `login_status` | 查询登录业务状态；`include_image=true` 包含当前二维码或验证截图 |
| `login_cancel` | 请求取消登录；`finalizing` 或 `success` 状态不终止保存和资源回收；取消结果通过 `login_status` 确认 |

浏览器任务共享运行锁，资源占用时拒绝新任务。`dry_run`、`check` 和会话读取不提交消息，但可更新运行记录、账号健康状态及会话缓存。

`check`、`fetch_conversations` 和 `login_start` 的工具元数据声明外部访问及本地状态更新。登录响应同时提供结构化数据和完整 JSON 文本，文本客户端亦可读取任务生命周期字段。

## 登录状态与任务生命周期

| 状态类别 | 状态值 |
| --- | --- |
| 初始化及等待 | `starting`、`pending` |
| 移动端确认 | `scanned` |
| 平台验证 | `sms_required`、`verification_required` |
| 验证与持久化 | `finalizing` |
| 业务终态 | `success`、`expired`、`failed`、`cancelled` |

`finished` 表示登录业务终态，`job_state` 表示所属后台任务状态。业务终态出现后，进程可暂时保留结果查询入口；`job_state` 为 `running` 时，不启动其他浏览器任务。

建议调用顺序：状态查询、必要的账号登录、业务终态与资源回收确认、显式账号启用、定位验证、授权发送、最终任务结果查询。重新登录保留账号启用状态；每次实际发送独立执行账号和运行环境验证。

## 客户端配置

控制面板「设置」页面根据当前操作系统生成启动命令和 `mcpServers` JSON，包含 `--config` 和实际数据目录的 `--data`。路径均为绝对路径；客户端配置文件位置和结构以相应客户端约定为准。

Windows 配置示例：

```json
{
  "mcpServers": {
    "dyspark": {
      "type": "stdio",
      "command": "C:\\dyspark\\.venv\\Scripts\\spark.exe",
      "args": ["--config", "C:\\dyspark\\config.toml", "--data", "C:\\dyspark\\data", "mcp"]
    }
  }
}
```

Linux 使用对应的绝对路径，例如 `/opt/dyspark/.venv/bin/spark`。TOML 等客户端配置格式应映射 `command` 和 `args` 字段，不直接使用 JSON 结构。

`--data` 要求与 GUI 使用的数据目录一致。省略时采用 `SPARK_DATA_DIR` 或配置文件同目录的 `data/`；不同目录使用独立的登录状态和发送账本。

远程 Docker 配置示例：

```json
{
  "mcpServers": {
    "dyspark-remote": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "user@server",
        "cd /opt/dyspark && docker compose run --rm -T mcp"
      ]
    }
  }
}
```

该示例要求服务器源码、镜像和账号配置已就绪，并具备非交互 SSH 认证。SSH 和 Compose 使用 `-T` 禁用伪终端，保持协议数据流完整。此入口不发布独立登录端口，可与 GUI 共享配置及数据。

非容器远程入口可使用 `/opt/dyspark/.venv/bin/spark --config /opt/dyspark/config.toml --data /opt/dyspark/data mcp`。无 DISPLAY 的 Linux 有头任务要求 `xvfb-run` 和 xauth。

## 独立远程登录

需要独立网页登录入口时，Compose 命令使用：

```bash
docker compose run --rm -T --service-ports -e SPARK_PUBLIC_URL=http://127.0.0.1:8789 mcp
```

操作端通过 SSH 转发 8789 和可选的 6080，参见 [登录与手动命令](cloud.md#登录与手动命令)。`SPARK_PUBLIC_URL` 对应操作端隧道入口，避免使用容器内部地址作为客户端访问地址。

`--service-ports` 要求相同宿主端口未被其他容器占用；GUI 服务占用这些端口时应停止该服务。GUI 停止期间每日调度停止，MCP 不接管调度。端口映射持续整个 MCP 容器会话。

Docker GUI 和 MCP 共享 `/config/config.toml`。配置目录及文件要求 UID 1000 可写，目录挂载支持原子替换；权限设置参见 [Docker 部署](cloud.md#docker-部署)。

登录链接格式为 `<login.public_url>/login/<随机令牌>`；未配置前缀时使用生成的 HTTP 地址。随机令牌为临时访问凭证，不应进入公开记录。完成、取消或超时后，临时服务可保留约 6 秒用于结果查询，随后释放端口。远程 HTTP 入口通过 SSH 本地转发访问，公网入口要求 HTTPS。

[账号登录](usage.md#账号登录) · [配置参考](configuration.md) · [项目首页](../README.md)
