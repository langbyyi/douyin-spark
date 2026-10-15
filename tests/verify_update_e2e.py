"""Opt-in real install/restart/rollback probe; uses only disposable repositories.

Run with an installed project environment: python tests/verify_update_e2e.py native
Docker mode needs access to a running Docker daemon and builds real images.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dyspark.update_deploy import DockerDeployment, NativeDeployment
from dyspark.update_manager import UpdateManager
from dyspark.update_source import GitSource
from dyspark.update_state import read_state


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def wait_for(check, *, timeout: float = 300):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for probe: {last}")


def snapshot(source: Path, target: Path) -> None:
    target.mkdir()
    for name in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "MANIFEST.in",
        "config.example.toml",
        ".env.example",
        "docker-compose.yml",
        ".dockerignore",
        ".gitignore",
    ):
        shutil.copy2(source / name, target / name)
    for name in ("src", "docs", "deploy"):
        shutil.copytree(
            source / name,
            target / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
        )


def run(mode: str, service: str, parent: Path | None) -> dict:
    source = Path(__file__).resolve().parents[1]
    base = Path(tempfile.mkdtemp(prefix="dyspark-e2e-", dir=parent))
    app = base / ("dyspark-verify-" + secrets.token_hex(5))
    snapshot(source, app)
    password = secrets.token_urlsafe(20)
    os.environ["SPARK_GUI_PASSWORD"] = password
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    forwarded = mode == "docker" and service == "spark"
    if mode == "native":
        config = base / "配置 config.toml"
        config.write_text("[schedule]\nenabled=false\n[notify]\nenabled=false\n")
        data = base / "数据 data"
        adapter = NativeDeployment(app, data, config=config, python=Path(sys.executable), port=port)
    else:
        data = app / "data"
        data.mkdir(mode=0o700)
        (app / ".env").write_text("")
        (app / "config").mkdir(mode=0o700)
        config = app / "config/config.toml"
        config.write_text("[schedule]\nenabled=false\n[notify]\nenabled=false\n")
        adapter = DockerDeployment(app, data, service=service, health_url=url + "/auth/login")
        compose = json.loads(adapter.compose("config", "--format", "json"))
        selected = compose["services"][service]
        selected["environment"]["SPARK_GUI_PASSWORD"] = password
        if service == "local":
            selected["command"] = ["gui", "--host", "127.0.0.1", "--port", str(port), "--no-open"]
            selected["healthcheck"]["test"][-1] = (
                "import urllib.request; "
                f"assert urllib.request.urlopen('http://127.0.0.1:{port}/static/icon.svg').status==200"
            )
        else:
            bridge = json.loads(
                subprocess.check_output(
                    ["docker", "network", "inspect", "bridge", "--format", "{{json .IPAM.Config}}"],
                    text=True,
                )
            )
            selected["network_mode"] = "bridge"
            selected.pop("networks", None)
            selected["environment"]["FORWARDED_ALLOW_IPS"] = bridge[0]["Gateway"]
            selected["ports"] = [
                {
                    "target": 8765,
                    "published": str(port),
                    "host_ip": "127.0.0.1",
                    "protocol": "tcp",
                    "mode": "ingress",
                }
            ]
        # JSON is valid Compose YAML; all changes affect this disposable project only.
        (app / "docker-compose.yml").write_text(json.dumps(compose, indent=2))
    data.mkdir(mode=0o700, exist_ok=True)
    env_file = config.parent / ".env"
    if not env_file.exists():
        env_file.write_text("# temporary preservation probe\n")
    sentinel = data / "accounts/probe/profile-sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(secrets.token_bytes(32))
    ledger = data / "preservation-ledger.json"
    ledger.write_text('{"probe":"preserve"}')
    preserved = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            config,
            env_file,
            sentinel,
            ledger,
        )
    }
    git(app, "init", "-b", "main")
    for key, value in (("user.name", "E2E Probe"), ("user.email", "probe@example.invalid")):
        git(app, "config", key, value)
    git(app, "add", ".")
    git(app, "commit", "-qm", "Disposable baseline")
    initial = git(app, "rev-parse", "HEAD")
    remote = base / "remote.git"
    git(base, "clone", "--bare", str(app), str(remote))
    git(app, "remote", "add", "origin", str(remote))
    publisher = base / "publisher"
    git(base, "clone", str(remote), str(publisher))
    git(publisher, "config", "user.name", "E2E Probe")
    git(publisher, "config", "user.email", "probe@example.invalid")
    manager = UpdateManager(data, GitSource(app, source_url=str(remote)), adapter)
    failures = []

    def supervise():
        try:
            manager.run(identity={"mode": mode, "root": str(app), "data": str(data)})
        except Exception as exc:
            failures.append(repr(exc))

    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(cookies),
    )

    def request(path, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if forwarded:
            # Emulate the trusted TLS terminator only on the disposable loopback
            # endpoint. Carry its Secure cookie explicitly through this test hop.
            headers["X-Forwarded-Proto"] = "https"
            if cookies:
                headers["Cookie"] = "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)
        if token:
            headers["X-Spark-Token"] = token
        req = urllib.request.Request(
            url + path,
            headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        try:
            response = opener.open(req, timeout=4)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode()
            return response.status, content

    def login():
        status, _ = request("/auth/login", {"password": password})
        assert status == 200
        status, html = request("/")
        assert status == 200
        return re.search(r'name="spark-token" content="([^"]+)"', html).group(1)

    def update(target, terminal, token):
        assert request("/api/update/check", {}, token)[0] == 200

        def checked():
            status, content = request("/api/update")
            return status == 200 and json.loads(content)["data"].get("latest") == target

        wait_for(checked)
        status, content = request("/api/update/apply", {"target": target}, token)
        assert status == 200, content
        request_id = json.loads(content)["data"]["request_id"]

        def settled():
            transaction = read_state(manager.folder / "transaction.json")
            if transaction.get("id") != request_id:
                return False
            if transaction.get("phase") in {"recovery_failed", "failed"}:
                return True
            return (
                transaction.get("phase") == terminal
                and not (data / "updater/maintenance.json").exists()
            )

        wait_for(
            settled,
            timeout=1200,
        )
        transaction = read_state(manager.folder / "transaction.json")
        assert transaction["phase"] == terminal, transaction
        assert not (data / "updater/maintenance.json").exists()
        return login()

    try:
        print(
            json.dumps({"stage": "starting", "mode": mode, "service": service, "base": str(base)}),
            flush=True,
        )

        def ready():
            if failures:
                raise RuntimeError(failures)
            return request("/auth/login")[0] in {200, 426}

        wait_for(ready, timeout=1200)
        token = login()
        readme = publisher / "README.md"
        readme.write_text(readme.read_text() + "\nDisposable successful update probe.\n")
        git(publisher, "add", "README.md")
        git(publisher, "commit", "-qm", "Healthy probe update")
        git(publisher, "push", "origin", "main")
        healthy = git(publisher, "rev-parse", "HEAD")
        token = update(healthy, "complete", token)
        assert read_state(manager.folder / "current.json")["commit"] == healthy
        print(json.dumps({"stage": "updated", "commit": healthy}), flush=True)
        gui = publisher / "src/dyspark/gui.py"
        gui.write_text(
            gui.read_text().replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n"
                'raise RuntimeError("intentional rollback probe")\n',
                1,
            )
        )
        git(publisher, "add", "src/dyspark/gui.py")
        git(publisher, "commit", "-qm", "Broken startup probe")
        git(publisher, "push", "origin", "main")
        broken = git(publisher, "rev-parse", "HEAD")
        update(broken, "restored", token)
        assert read_state(manager.folder / "current.json")["commit"] == healthy
        for path, expected in preserved.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, path
        assert git(app, "rev-parse", "HEAD") == initial
        result = {
            "mode": mode,
            "service": service,
            "initial": initial,
            "healthy": healthy,
            "rejected": broken,
            "rollback": True,
            "data_preserved": True,
            "source_unchanged": True,
            "base": str(base),
        }
        (base / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        return result
    finally:
        manager.stop_event.set()
        thread.join(timeout=45)
        if thread.is_alive():
            raise AssertionError("manager did not stop in time")
        if failures:
            raise AssertionError(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["native", "docker"])
    parser.add_argument("--service", choices=["local", "spark"], default="local")
    parser.add_argument("--parent", type=Path)
    options = parser.parse_args()
    run(options.mode, options.service, options.parent)
