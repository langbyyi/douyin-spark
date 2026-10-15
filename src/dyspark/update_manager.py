"""Foreground host supervisor for native Python and Docker updates."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

from dyspark.locking import LockBusyError, file_lock
from dyspark.update_deploy import DockerDeployment, NativeDeployment, venv_python
from dyspark.update_source import GitSource, command
from dyspark.update_state import (
    ACTIVE_PHASES,
    IDENTIFIER,
    SHA,
    UpdateError,
    end_maintenance,
    read_state,
    state_dir,
    write_state,
)


class UpdateManager:
    def __init__(self, data: Path, source, deployment, *, private: Path | None = None) -> None:
        self.data, self.source, self.deployment = data, source, deployment
        self.shared = state_dir(data)
        self.folder = private if private is not None else deployment.private
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.instance = secrets.token_hex(16)
        self.deployment.instance = self.instance
        self.current: dict = {}
        self.stop_event = threading.Event()

    def record(self, request: dict, phase: str, **fields) -> None:
        write_state(
            self.folder / "transaction.json",
            {
                **request,
                "phase": phase,
                "updated_at": time.time(),
                **fields,
            },
        )
        # Only a display projection crosses the container boundary. Deployment
        # paths, image records and authoritative transactions stay on the host.
        write_state(
            self.shared / "transaction.json",
            {
                **request,
                "phase": phase,
                "updated_at": time.time(),
                **({"error": fields["error"]} if "error" in fields else {}),
            },
        )

    def check(self) -> None:
        previous = read_state(self.shared / "available.json")
        try:
            available = {**self.source.check(self.current["commit"]), "error": None}
            available["checked_at"] = time.time()
        except UpdateError as exc:
            available = {**previous, "current": self.current["commit"], "error": str(exc)}
        write_state(self.shared / "available.json", available)

    def apply(self, request: dict) -> None:
        if (
            set(request) != {"id", "instance", "target"}
            or not isinstance(request.get("id"), str)
            or not IDENTIFIER.fullmatch(request["id"])
            or request.get("instance") != self.instance
            or not isinstance(request.get("target"), str)
            or not SHA.fullmatch(request["target"])
            or read_state(self.shared / "maintenance.json") != request
        ):
            raise UpdateError("更新请求身份或字段无效，未执行任何部署操作")
        previous = dict(self.current)
        switched = False
        self.record(request, "preparing", previous=previous)
        try:
            available = self.source.check(previous["commit"])
            if request["target"] != available["latest"]:
                raise UpdateError("上游版本已变化，请重新检查后再更新")
            destination = self.folder / "releases" / request["id"]
            staged = self.source.stage(request["target"], destination)
            candidate = self.deployment.prepare(staged, request["target"])
            self.record(request, "switching", previous=previous, candidate=candidate)
            switched = True  # stop may partially succeed; recovery is then mandatory.
            self.deployment.stop()
            self.deployment.start(candidate)
            self.record(request, "verifying", previous=previous, candidate=candidate)
            self.deployment.wait_healthy()
            write_state(self.folder / "current.json", candidate)
            self.current = candidate
            self.record(request, "complete", previous=previous)
            end_maintenance(self.data, request["id"])
        except (UpdateError, OSError, ValueError) as exc:
            if switched:
                self._restore(request, previous, str(exc))
            else:
                self.record(request, "failed", previous=previous, error=str(exc))
                end_maintenance(self.data, request["id"])

    def _restore(self, request: dict, previous: dict, reason: str) -> None:
        self.record(request, "recovering", previous=previous, error=reason)
        try:
            self.deployment.stop()
            self.deployment.start(previous)
            self.deployment.wait_healthy()
            write_state(self.folder / "current.json", previous)
            self.current = previous
            self.record(request, "restored", previous=previous, error=reason)
            end_maintenance(self.data, request["id"])
        except (UpdateError, OSError, ValueError) as exc:
            self.record(
                request,
                "recovery_failed",
                previous=previous,
                error=f"{reason}；旧版恢复失败：{exc}。请保留维护状态并检查部署日志",
            )

    def recover(self) -> bool:
        transaction = read_state(self.folder / "transaction.json")
        maintenance = read_state(self.shared / "maintenance.json")
        if transaction.get("phase") in ACTIVE_PHASES | {"recovery_failed"}:
            previous = transaction.get("previous")
            if not previous or not maintenance or transaction.get("id") != maintenance.get("id"):
                raise UpdateError("更新事务与维护记录不完整，请人工核对后再启动")
            self._restore(maintenance, previous, "上次更新被中断，恢复旧版")
            return True
        return False

    def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            write_state(
                self.shared / "heartbeat.json",
                {
                    "instance": self.instance,
                    "time": time.time(),
                    "pid": os.getpid(),
                },
            )
            self.stop_event.wait(2)

    def run(self, *, identity: dict | None = None) -> None:
        with file_lock(self.folder / "manager.lock"):
            if identity is not None:
                owner = read_state(self.folder / "owner.json")
                if owner and owner.get("deployment") != identity:
                    raise UpdateError("源码目录已绑定其他部署或启动参数，请沿用原参数")
                if not owner:
                    owner = {"id": secrets.token_hex(16), "deployment": identity}
                    write_state(self.folder / "owner.json", owner)
                self.deployment.owner = owner["id"]
            self.current = read_state(self.folder / "current.json")
            if not self.current:
                self.current = self.deployment.initial(self.source.current())
                write_state(self.folder / "current.json", self.current)
            heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
            heartbeat.start()
            try:
                recovered = self.recover()
                if read_state(self.folder / "transaction.json").get("phase") == "recovery_failed":
                    raise UpdateError("旧版恢复失败，请检查日志后重新运行管理器")
                if not recovered:
                    self.deployment.stop()
                    self.deployment.start(self.current)
                    self.deployment.wait_healthy()
                    maintenance = read_state(self.shared / "maintenance.json")
                    if maintenance:
                        transaction = read_state(self.folder / "transaction.json")
                        if not (
                            transaction.get("phase") == "complete"
                            and transaction.get("id") == maintenance.get("id")
                            and self.current.get("commit") == transaction.get("target")
                        ):
                            self.record(
                                maintenance, "restored", error="上次请求未完成，已重新启动原版本"
                            )
                        end_maintenance(self.data, maintenance["id"])
                print(
                    "控制面板已就绪；设置 → 程序更新。按 Ctrl+C 停止服务和更新管理器。", flush=True
                )
                next_check, check_id = 0.0, None
                while not self.stop_event.is_set():
                    if not self.deployment.alive():
                        raise UpdateError("受管理服务已停止；管理器不会擅自重启手动停止的服务")
                    request = read_state(self.shared / "request.json")
                    transaction = read_state(self.folder / "transaction.json")
                    maintenance = read_state(self.shared / "maintenance.json")
                    if maintenance and request.get("id") != transaction.get("id"):
                        self.apply(request)
                        next_check = 0
                    check_request = read_state(self.shared / "check-request.json")
                    if time.monotonic() >= next_check or check_request.get("id") != check_id:
                        self.check()
                        check_id = check_request.get("id")
                        next_check = time.monotonic() + 6 * 3600
                    self.stop_event.wait(0.5)
            finally:
                self.stop_event.set()
                heartbeat.join(timeout=3)
                self.deployment.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dyspark 跨环境更新管理器（前台运行）")
    parser.add_argument("mode", choices=["native", "docker"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--extras", nargs="+", choices=["gui", "mcp", "dev"])
    parser.add_argument("--browser", choices=["chromium"])
    parser.add_argument("--service", choices=["local", "spark"], default="local")
    parser.add_argument("--health-url", help="面板的健康检查 URL；默认访问本机面板登录页")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if args.mode == "native":
            python = (args.python or venv_python(root / ".venv")).expanduser().absolute()
            config = (args.config or root / "config.toml").expanduser().resolve()
            probe = (
                "import sys,json,importlib.util;sys.path.insert(0,sys.argv[1]);"
                "from dyspark.config import read_settings;"
                "s=read_settings(__import__('pathlib').Path(sys.argv[2]));"
                "print(json.dumps({'data':str(s.data_dir),'browser':s.browser.channel,"
                "'mcp':importlib.util.find_spec('mcp') is not None}))"
            )
            settings = json.loads(
                command([str(python), "-c", probe, str(root / "src"), str(config)], cwd=root)
            )
            data = (args.data or Path(settings["data"])).expanduser().resolve()
            extras = tuple(
                sorted(
                    set(args.extras or (["gui", "mcp"] if settings["mcp"] else ["gui"])) | {"gui"}
                )
            )
            deployment = NativeDeployment(
                root,
                data,
                config=config,
                python=python,
                host=args.host,
                port=args.port,
                extras=extras,
                browser=args.browser or ("chromium" if settings["browser"] == "chromium" else None),
                health_url=args.health_url,
            )
            identity = {
                "mode": args.mode,
                "root": str(root),
                "config": str(config),
                "host": args.host,
                "port": args.port,
            }
        else:
            data = (args.data or root / "data").expanduser().resolve()
            deployment = DockerDeployment(
                root, data, service=args.service, health_url=args.health_url
            )
            identity = {"mode": args.mode, "root": str(root), "service": args.service}
        identity["data"] = str(data)
        manager = UpdateManager(data, GitSource(root), deployment)
        signal.signal(signal.SIGINT, lambda *_: manager.stop_event.set())
        signal.signal(signal.SIGTERM, lambda *_: manager.stop_event.set())
        manager.run(identity=identity)
        return 0
    except (UpdateError, LockBusyError, OSError, ValueError, KeyError) as exc:
        print(f"更新管理器未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
