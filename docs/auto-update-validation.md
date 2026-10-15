# 自动更新验证记录

日期：2026-10-15。执行环境：Windows / Python 3.12，以及 Kali Linux / Python 3.14.6。
所有测试使用临时配置和数据，不更新真实部署。

## 自动检查

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q -rs --tb=short` | 73 passed，3 skipped |
| `node --test tests/update-ui.test.cjs` | 4 passed |
| `node --check src/dyspark/gui_static/app.js` | 通过 |
| `node --check src/dyspark/gui_static/updates.js` | 通过 |
| `.venv/Scripts/python.exe -m ruff check .` | 通过 |
| `.venv/Scripts/python.exe -m ruff format --check .` | 通过 |
| `git diff --check` | 通过，只有已有换行转换提示 |
| `.venv/Scripts/python.exe -m pip wheel . --no-deps --no-build-isolation --wheel-dir <临时目录>` | wheel 构建通过 |

PowerShell 中先设置 `$env:PYTHONPATH = (Join-Path (Get-Location) 'src')`，再运行 Python 测试。

跳过项：原有两个 Linux 进程元数据测试，以及新增的虚拟环境解释器符号链接测试；当前 Windows 不允许创建该符号链接。

## Kali Linux 验证

在 Kali 的独立目录 `/tmp/dyspark-verify-p04vgkb7` 上传当前源码快照，创建临时虚拟环境并安装 `[dev]`。
没有修改系统 Python、全局 Git 配置、现有 Compose 项目或真实账号数据。

| 检查 | 结果 |
| --- | --- |
| 完整 Python 测试 | **76 passed，0 skipped**，包含 Linux 进程元数据和虚拟环境符号链接测试 |
| Node 前端测试 | **4 passed** |
| Ruff / 格式检查 | 通过 |
| 临时虚拟环境 `pip check` | 通过，无依赖冲突 |
| wheel 构建 | 通过 |
| 当前 Dockerfile 的真实镜像构建 | 通过，独立镜像标签 `dyspark:kali-verify-p04vgkb7` |
| 原生端到端升级与启动失败回滚 | 通过 |
| Docker `local` 端到端升级与启动失败回滚 | 通过 |
| Docker `spark` 端到端升级与启动失败回滚 | 通过 |

三组端到端测试均通过真实面板 API 请求更新，从临时本地 Git 远端取得新提交，实际安装虚拟环境或重建镜像并重启。
随后提交一个故意使 GUI 启动失败的版本，确认自动恢复前一个健康版本。
配置、环境文件、测试登录资料哨兵文件及测试账本的 SHA-256 保持不变，原始源码 Git HEAD 保持不变。
Docker `spark` 通过精确的 bridge 网关模拟可信代理头访问 API，未额外验证真实 TLS 终止代理。

发现并修复的 Linux 专属问题：按脚本文件路径启动 `process_scope.py` 时，包目录中的 `selectors.py`
遮蔽 Python 标准库，导致监督进程退出。改为 `python -m dyspark.process_scope` 后，
原失败用例、完整 Kali 测试及 Windows 回归测试均通过。

可复跑脚本为 `tests/verify_update_e2e.py`。本次实际执行的端到端命令（目录为临时源码根目录）：

```bash
PYTHONPATH=src /tmp/dyspark-verify-p04vgkb7/venv/bin/python tests/verify_update_e2e.py native --parent /tmp/dyspark-verify-p04vgkb7
PATH=/tmp/dyspark-verify-p04vgkb7/bin:$PATH PYTHONPATH=src /tmp/dyspark-verify-p04vgkb7/venv/bin/python tests/verify_update_e2e.py docker --service local --parent /tmp/dyspark-verify-p04vgkb7
PATH=/tmp/dyspark-verify-p04vgkb7/bin:$PATH PYTHONPATH=src /tmp/dyspark-verify-p04vgkb7/venv/bin/python tests/verify_update_e2e.py docker --service spark --parent /tmp/dyspark-verify-p04vgkb7
```

临时 `bin/docker` 只为本次测试转发到已有的 `sudo -n /usr/bin/docker`，没有修改宿主 Docker 配置或用户组。
测试结束后管理器已停止测试服务，原先运行的 Compose 项目保持运行。临时版本、镜像和日志保留供审阅。
机器可读结果见 [kali-update-verification.json](kali-update-verification.json)，其中记录测试提交及源码文件校验值；
已确认所列源码文件与本地交付内容完全一致。

## 真实运行与隔离安装

- 在 Windows 启动实际 GUI 服务，核对提交、实例与启动标识，然后通过管理器正常关闭。
- 临时配置和数据路径含中文及空格，账号配置与真实登录资料未参与测试。
- 将本次源码复制到临时独立版本目录，使用 `NativeDeployment.prepare` 创建全新虚拟环境并安装项目 `[gui]`。
- 在新虚拟环境运行 `python -m pip check`，结果为 `No broken requirements found.`。
- 启动安装后的目标版本，通过实际 HTTP 健康检查并正常停止。
- 浏览器实际登录并打开更新设置页；页面无 JavaScript 异常，说明文档返回 200。
- 在 390px 手机视口验证卡片可见、无横向溢出，未启用管理器时更新按钮禁用。
- 实际执行 Docker Compose 的配置解析，验证 `local` 和 `spark` 两种服务的合并配置保留命令、挂载、网络、端口和已有环境变量。
- 检查构建的 wheel，包含全部更新模块和前端脚本，不包含配置、凭据、业务数据或宿主控制状态。

## 故障与审查

本地临时 Git 仓库验证提交更新、脏工作区、未知目标、分叉／回退和数据保留。
事务测试验证成功切换、准备失败、新版健康检查失败、旧版恢复失败和中断恢复。
独立代码审查发现的宿主控制文件权限边界、任务启动争锁、首次启动所有权竞态已修复并添加回归测试。
另外验证完成记录写入后、维护标记清理前发生中断的恢复行为，以及维护期间的面板密码修改保护。

## 验证限制

- Windows 本机的 Docker Desktop Linux Engine 未运行；真实镜像构建、容器替换和回滚已在 Kali 完成。
- macOS 尚未实机验证，不能将 Windows / Linux 测试视作 macOS 实测。
- 当前开发 Python 环境的 `pip check` 未通过，存在 fastmcp/mcp、mitmproxy/cryptography、numpy 等与其他工具有关的依赖冲突；本次没有修改该环境。全新项目隔离环境的依赖检查已通过。
- 官方仓库未发布本次未提交改动；未对真实上游或现有部署执行升级。当前工作区的未提交修改会按设计阻止一键安装，需先保存、集成并发布相应代码。
