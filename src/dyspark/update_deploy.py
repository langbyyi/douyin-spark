"""Host-only native and Docker deployment adapters. No GUI dependencies required."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dyspark.process_scope import ProcessScope
from dyspark.update_source import command
from dyspark.update_state import SHA, UpdateError, state_dir, write_state


def venv_python(folder: Path) -> Path:
    return folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def private_dir(root: Path) -> Path:
    folder = root / ".spark-updater"
    if folder.is_symlink():
        raise UpdateError("宿主更新目录不能是符号链接")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        folder.chmod(0o700)
    return folder


class Deployment:
    def __init__(self, root: Path, data: Path, *, health_url: str) -> None:
        self.root = root.resolve()
        self.data = data.resolve()
        self.folder = state_dir(self.data)
        self.private = private_dir(self.root)
        if self.private.is_relative_to(self.data):
            raise UpdateError("宿主更新目录必须位于共享数据目录之外")
        self.health_url = health_url
        self.instance = ""
        self.boot = ""
        self.commit = ""

    def wait_healthy(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = None
            try:
                try:
                    response = opener.open(self.health_url, timeout=2)
                except urllib.error.HTTPError as exc:
                    response = exc  # 401 / HTTPS-required still identifies the running app.
                headers = response.headers
                if (
                    headers.get("X-Spark-Commit") == self.commit
                    and headers.get("X-Spark-Instance") == self.instance
                    and headers.get("X-Spark-Boot") == self.boot
                ):
                    return
            except OSError:
                pass
            finally:
                if response is not None:
                    response.close()
            if not self.alive():
                raise UpdateError("新服务提前退出，请查看 .spark-updater/service.log 或容器日志")
            time.sleep(0.25)
        raise UpdateError("服务健康检查超时，未确认目标程序启动成功")

    def alive(self) -> bool:
        return True


class NativeDeployment(Deployment):
    def __init__(
        self,
        root: Path,
        data: Path,
        *,
        config: Path,
        python: Path,
        host: str = "127.0.0.1",
        port: int = 8765,
        extras: tuple[str, ...] = ("gui",),
        browser: str | None = None,
        health_url: str | None = None,
    ) -> None:
        address = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        if ":" in address:
            address = f"[{address}]"
        super().__init__(root, data, health_url=health_url or f"http://{address}:{port}/auth/login")
        self.config = config.resolve()
        # Dereferencing a POSIX venv symlink loses the virtual environment.
        self.python = python.absolute()
        self.host, self.port = host, port
        self.extras, self.browser = extras, browser
        self.process: subprocess.Popen | None = None
        self.scope: ProcessScope | None = None

    def initial(self, commit: str) -> dict:
        return {"source": str(self.root), "python": str(self.python), "commit": commit}

    def prepare(self, source: Path, commit: str) -> dict:
        environment = source / ".venv"
        command([sys.executable, "-m", "venv", str(environment)], cwd=self.root, timeout=180)
        python = venv_python(environment)
        package = str(source) + f"[{','.join(self.extras)}]"
        command([str(python), "-m", "pip", "install", package], cwd=self.root, timeout=1800)
        if self.browser:
            command(
                [str(python), "-m", "playwright", "install", self.browser],
                cwd=self.root,
                timeout=1800,
            )
        return {"source": str(source), "python": str(python), "commit": commit}

    def start(self, release: dict) -> None:
        source = Path(release.get("source", "")).resolve()
        python = Path(release.get("python", "")).absolute()
        commit = release.get("commit")
        if (
            not isinstance(commit, str)
            or not SHA.fullmatch(commit)
            or not (source == self.root or source.is_relative_to(self.private / "releases"))
            or python != (self.python if source == self.root else venv_python(source / ".venv"))
        ):
            raise UpdateError("原生版本记录无效，拒绝执行未知程序")
        self.boot, self.commit = secrets.token_hex(16), commit
        environment = {
            **os.environ,
            "PYTHONPATH": str(source / "src"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "SPARK_UPDATE_INSTANCE": self.instance,
            "SPARK_UPDATE_BOOT": self.boot,
        }
        argv = [
            str(python),
            "-m",
            "dyspark",
            "--config",
            str(self.config),
            "--data",
            str(self.data),
            "gui",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--no-open",
        ]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        self.scope = ProcessScope()
        with (self.private / "service.log").open("ab") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "dyspark.process_scope", "--supervise", "--", *argv],
                cwd=self.config.parent,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                **options,
            )
        try:
            self.scope.attach(self.process.pid)
            self.process.stdin.write(b"start\n")
            self.process.stdin.flush()
        except Exception:
            self.process.stdin.close()
            self.scope.close()
            raise

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is None:
            return
        if self.alive():
            write_state(self.folder / "stop.json", {"instance": self.instance, "boot": self.boot})
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise UpdateError("服务未正常退出，已停止切换版本，请检查任务与日志") from exc
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.scope is not None:
            self.scope.close()
        self.process = None

    def close(self) -> None:
        try:
            self.stop()
        finally:
            if self.process is not None:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.scope is not None:
                    self.scope.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=5)


class DockerDeployment(Deployment):
    def __init__(
        self, root: Path, data: Path, *, service: str = "local", health_url: str | None = None
    ) -> None:
        if service not in {"local", "spark"}:
            raise UpdateError("仅支持 local 或 spark GUI 服务")
        super().__init__(root, data, health_url=health_url or "http://127.0.0.1:8765/auth/login")
        self.service = service
        self.owner = ""
        self.runtime = self.private / "compose-runtime.json"

    def compose(self, *args: str, override: Path | None = None, timeout: float = 90) -> str:
        argv = [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "-f",
            str(self.root / "docker-compose.yml"),
        ]
        if override is not None:
            argv += ["-f", str(override)]
        if self.service == "local":
            argv += ["--profile", "local"]
        return command([*argv, *args], cwd=self.root, timeout=timeout)

    def validate_config(self) -> None:
        config = json.loads(self.compose("config", "--format", "json"))
        volumes = config["services"][self.service].get("volumes", [])
        if not any(
            volume.get("type") == "bind"
            and volume.get("target") == "/data"
            and Path(volume.get("source", "")).resolve() == self.data
            for volume in volumes
        ):
            raise UpdateError("Docker 必须将选定的宿主数据目录绑定到 /data")

    def initial(self, commit: str) -> dict:
        self.validate_config()
        # Require ownership before changing an existing running service.
        self._owned_container()
        return self.prepare(self.root, commit)

    def prepare(self, source: Path, commit: str) -> dict:
        image = "dyspark:update-" + secrets.token_hex(16)
        build = self.private / "compose-build.json"
        write_state(
            build,
            {
                "services": {
                    self.service: {
                        "image": image,
                        "build": {"context": str(source), "args": {"SPARK_BUILD_COMMIT": commit}},
                    }
                }
            },
        )
        self.compose("build", self.service, override=build, timeout=1800)
        return {"commit": commit, "image": image}

    def override(self, release: dict) -> Path:
        commit, image = release.get("commit"), release.get("image", "")
        suffix = image.removeprefix("dyspark:update-") if isinstance(image, str) else ""
        if (
            not isinstance(commit, str)
            or not SHA.fullmatch(commit)
            or not isinstance(image, str)
            or not image.startswith("dyspark:update-")
            or not (len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix))
        ):
            raise UpdateError("Docker 版本记录无效")
        write_state(
            self.runtime,
            {
                "services": {
                    self.service: {
                        "image": image,
                        "environment": {
                            "SPARK_UPDATE_INSTANCE": self.instance,
                            "SPARK_UPDATE_BOOT": self.boot,
                        },
                        "labels": {"io.dyspark.updater": self.owner},
                    }
                }
            },
        )
        return self.runtime

    def start(self, release: dict) -> None:
        self._owned_container()
        self.boot, self.commit = secrets.token_hex(16), release["commit"]
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            self.service,
            override=self.override(release),
            timeout=120,
        )

    def _owned_container(self) -> str:
        container = self.compose("ps", "--status", "running", "-q", self.service)
        if container:
            labels = json.loads(
                command(
                    ["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
                    cwd=self.root,
                )
            )
            if not self.owner or labels.get("io.dyspark.updater") != self.owner:
                raise UpdateError("现有容器由其他方式启动，请先停止该服务，再启用更新管理器")
        return container

    def stop(self) -> None:
        if self._owned_container():
            self.compose("stop", "--timeout", "30", self.service, timeout=45)

    def alive(self) -> bool:
        return bool(self._owned_container())

    def close(self) -> None:
        self.stop()
