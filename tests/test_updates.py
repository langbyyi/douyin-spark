from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def module(name):
    return importlib.import_module(f"dyspark.{name}")


def test_public_check_handles_large_commits_without_fetching_the_diff(tmp_path, monkeypatch):
    updates = module("updates")
    head = {"sha": "c" * 40, "commit": {"message": "First release\nRelease details"}}
    responses = {
        "https://api.github.com/repos/langbyyi/douyin-spark/commits/main": {
            **head,
            "files": [{"patch": "large diff\n" * 20000}],
        },
        "https://api.github.com/repos/langbyyi/douyin-spark/commits?sha=main&per_page=1": [head],
    }

    def respond(request, *, timeout):
        return io.BytesIO(json.dumps(responses[request.full_url]).encode())

    monkeypatch.setattr(updates.urllib.request, "urlopen", respond)
    service = updates.UpdateService(tmp_path, idle=lambda: None, commit="a" * 40)
    service._check_public()
    result = service.status()
    assert result["check_error"] is None
    assert result["latest"] == "c" * 40
    assert result["summary"] == "First release"
    assert result["managed"] is False
    assert result["can_update"] is False


@pytest.mark.parametrize("payload", [[], {}, [None], [{"sha": "invalid"}], [1, 2]])
def test_public_check_rejects_unexpected_commit_list(tmp_path, monkeypatch, payload):
    updates = module("updates")
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    service = updates.UpdateService(tmp_path, idle=lambda: None, commit="a" * 40)
    service._check_public()
    result = service.status()
    assert result["check_error"] is not None
    assert result["latest"] is None
    assert result["can_update"] is False


def test_public_check_explains_api_rate_limit_and_keeps_last_known_version(tmp_path, monkeypatch):
    updates = module("updates")

    def limited(*_args, **_kwargs):
        raise updates.urllib.error.HTTPError(
            "https://api.github.com",
            403,
            "rate limit exceeded",
            {"X-RateLimit-Remaining": "0"},
            None,
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", limited)
    service = updates.UpdateService(tmp_path, idle=lambda: None, commit="a" * 40)
    service._available = {"current": "a" * 40, "latest": "c" * 40}
    service._check_public()
    result = service.status()
    assert "限流" in result["check_error"]
    assert result["latest"] == "c" * 40
    assert result["can_update"] is False


def test_maintenance_refuses_jobs_and_edits_without_touching_user_data(tmp_path):
    state = module("update_state")
    profile = tmp_path / "accounts" / "main" / "cookies"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"keep-login")
    request = {"id": "a" * 32, "instance": "b" * 32, "target": "c" * 40}
    state.begin_maintenance(tmp_path, request, lambda: None)
    for guard in (state.operation, state.activity):
        with pytest.raises(state.UpdateError, match="更新"), guard(tmp_path):
            pytest.fail("maintenance allowed a mutation")
    assert profile.read_bytes() == b"keep-login"
    state.end_maintenance(tmp_path, request["id"])
    with state.activity(tmp_path):
        pass


def test_running_cli_prevents_maintenance(tmp_path):
    state = module("update_state")
    with state.activity(tmp_path), pytest.raises(state.UpdateError):
        state.begin_maintenance(tmp_path, {"id": "a" * 32}, lambda: None)
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_idle_inspection_failure_never_freezes_service(tmp_path):
    state = module("update_state")

    def busy():
        raise ValueError("active job")

    with pytest.raises(ValueError, match="active job"):
        state.begin_maintenance(tmp_path, {"id": "a" * 32}, busy)
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_corrupt_maintenance_fails_closed(tmp_path):
    state = module("update_state")
    folder = state.state_dir(tmp_path)
    (folder / "maintenance.json").write_text("[]")
    with pytest.raises(state.UpdateError), state.operation(tmp_path):
        pytest.fail("corrupt maintenance was ignored")


def test_only_matching_request_can_release_maintenance(tmp_path):
    state = module("update_state")
    state.begin_maintenance(tmp_path, {"id": "a" * 32}, lambda: None)
    with pytest.raises(state.UpdateError):
        state.end_maintenance(tmp_path, "b" * 32)
    assert (tmp_path / "updater" / "maintenance.json").exists()


class IdleBackend:
    def __init__(self, root):
        self.root = root

    def update_idle(self):
        pass


def managed_service(tmp_path, monkeypatch):
    state = module("update_state")
    updates = module("updates")
    instance = "b" * 32
    monkeypatch.setenv("SPARK_UPDATE_INSTANCE", instance)
    folder = state.state_dir(tmp_path)
    state.write_state(folder / "heartbeat.json", {"instance": instance, "time": time.time()})
    state.write_state(
        folder / "available.json",
        {"current": "a" * 40, "latest": "c" * 40, "checked_at": time.time(), "error": None},
    )
    return updates.UpdateService(tmp_path, idle=lambda: None, commit="a" * 40)


@pytest.mark.parametrize("age", [30, -30])
def test_stale_or_future_heartbeat_does_not_authorize_update(tmp_path, monkeypatch, age):
    service = managed_service(tmp_path, monkeypatch)
    state = module("update_state")
    state.write_state(
        tmp_path / "updater" / "heartbeat.json",
        {"instance": "b" * 32, "time": time.time() - age},
    )
    assert service.status()["managed"] is False
    with pytest.raises(state.UpdateError):
        service.apply("c" * 40)


def test_apply_requires_exact_checked_target_and_is_single_request(tmp_path, monkeypatch):
    service = managed_service(tmp_path, monkeypatch)
    state = module("update_state")
    with pytest.raises(state.UpdateError):
        service.apply("d" * 40)
    result = service.apply("c" * 40)
    assert result["phase"] == "queued"
    assert result["target"] == "c" * 40
    with pytest.raises(state.UpdateError):
        service.apply("c" * 40)
    request = state.read_state(tmp_path / "updater" / "request.json")
    assert set(request) == {"id", "instance", "target"}


def test_another_manager_cannot_enable_this_process(tmp_path, monkeypatch):
    service = managed_service(tmp_path, monkeypatch)
    monkeypatch.setenv("SPARK_UPDATE_INSTANCE", "e" * 32)
    other = module("updates").UpdateService(tmp_path, idle=lambda: None, commit="a" * 40)
    assert service.status()["managed"] is True
    assert other.status()["managed"] is False


def git(root: Path, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "user.name", "Update Test")
    (remote / "src" / "dyspark").mkdir(parents=True)
    (remote / "src" / "dyspark" / "value.py").write_text("VALUE = 1\n")
    (remote / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    git(remote, "add", ".")
    git(remote, "commit", "-m", "initial")
    checkout = tmp_path / "源码 with spaces"
    git(tmp_path, "clone", str(remote), str(checkout))
    initial = git(checkout, "rev-parse", "HEAD")
    (remote / "src" / "dyspark" / "value.py").write_text("VALUE = 2\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "next")
    latest = git(remote, "rev-parse", "HEAD")
    return checkout, remote, initial, latest


def test_source_stages_update_without_mutating_checkout(repository, tmp_path):
    checkout, remote, initial, latest = repository
    source = module("update_source").GitSource(checkout, source_url=str(remote))
    assert source.check(initial)["latest"] == latest
    staged = source.stage(latest, tmp_path / "release")
    assert (staged / "src" / "dyspark" / "value.py").read_text() == "VALUE = 2\n"
    assert git(checkout, "rev-parse", "HEAD") == initial
    assert (checkout / "src" / "dyspark" / "value.py").read_text() == "VALUE = 1\n"
    assert json.loads((staged / "src" / "dyspark" / "_build.json").read_text())["commit"] == latest


def test_source_refuses_dirty_checkout(repository):
    checkout, remote, initial, _latest = repository
    (checkout / "src" / "dyspark" / "value.py").write_text("my local changes\n")
    source = module("update_source").GitSource(checkout, source_url=str(remote))
    with pytest.raises(module("update_state").UpdateError, match="改动"):
        source.check(initial)
    assert (checkout / "src" / "dyspark" / "value.py").read_text() == "my local changes\n"


def test_source_refuses_unknown_target_and_rollback(repository):
    checkout, remote, initial, latest = repository
    source = module("update_source").GitSource(checkout, source_url=str(remote))
    source.check(initial)
    for target in ("--upload-pack=bad", "d" * 40, initial):
        with pytest.raises(module("update_state").UpdateError):
            source.validate(latest, target)


def test_default_source_rejects_a_fork(repository):
    checkout, _remote, initial, _latest = repository
    source = module("update_source").GitSource(checkout)
    with pytest.raises(module("update_state").UpdateError, match="官方"):
        source.check(initial)


def test_source_preserves_ignored_config_and_profiles(repository):
    checkout, remote, initial, _latest = repository
    (checkout / ".git" / "info" / "exclude").write_text("config.toml\ndata/\n")
    (checkout / "config.toml").write_text("secret")
    (checkout / "data").mkdir()
    (checkout / "data" / "profile").write_bytes(b"cookies")
    source = module("update_source").GitSource(checkout, source_url=str(remote))
    source.check(initial)
    assert (checkout / "config.toml").read_text() == "secret"
    assert (checkout / "data" / "profile").read_bytes() == b"cookies"


class FakeDeployment:
    def __init__(self, *, fail_target=False, fail_restore=False):
        self.running = "a" * 40
        self.started = []
        self.stops = 0
        self.fail_target = fail_target
        self.fail_restore = fail_restore

    def prepare(self, source, commit):
        return {"commit": commit, "source": str(source)}

    def initial(self, commit):
        return {"commit": commit, "source": "initial"}

    def alive(self):
        return self.running is not None

    def close(self):
        self.stop()

    def start(self, release):
        self.running = release["commit"]
        self.started.append(self.running)

    def stop(self):
        self.stops += 1
        self.running = None

    def wait_healthy(self):
        if self.running == "c" * 40 and self.fail_target:
            raise module("update_state").UpdateError("new version failed")
        if self.running == "a" * 40 and self.fail_restore:
            raise module("update_state").UpdateError("old version failed")


class FakeSource:
    def current(self):
        return "a" * 40

    def check(self, current):
        return {"current": current, "latest": "c" * 40, "summary": "new release"}

    def stage(self, commit, destination):
        destination.mkdir(parents=True)
        return destination


def transaction_manager(tmp_path, deployment):
    state = module("update_state")
    manager = module("update_manager").UpdateManager(
        tmp_path, FakeSource(), deployment, private=tmp_path.parent / (tmp_path.name + "-private")
    )
    manager.current = {"commit": "a" * 40, "source": str(tmp_path / "old")}
    request = {"id": "d" * 32, "instance": manager.instance, "target": "c" * 40}
    state.begin_maintenance(tmp_path, request, lambda: None)
    return manager, request


def test_transaction_switches_only_after_preparation_and_verifies(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)
    manager.apply(request)
    state = module("update_state")
    assert deployment.running == "c" * 40
    assert state.read_state(manager.folder / "current.json")["commit"] == "c" * 40
    assert state.read_state(tmp_path / "updater" / "transaction.json")["phase"] == "complete"
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_failed_new_version_restores_old_program_and_keeps_data(tmp_path):
    deployment = FakeDeployment(fail_target=True)
    manager, request = transaction_manager(tmp_path, deployment)
    (tmp_path / "ledger.json").write_bytes(b"ledger unchanged")
    manager.apply(request)
    state = module("update_state")
    assert deployment.started == ["c" * 40, "a" * 40]
    assert deployment.running == "a" * 40
    assert state.read_state(tmp_path / "updater" / "transaction.json")["phase"] == "restored"
    assert (tmp_path / "ledger.json").read_bytes() == b"ledger unchanged"
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_failed_recovery_keeps_maintenance_and_does_not_report_success(tmp_path):
    deployment = FakeDeployment(fail_target=True, fail_restore=True)
    manager, request = transaction_manager(tmp_path, deployment)
    manager.apply(request)
    state = module("update_state")
    assert state.read_state(tmp_path / "updater" / "transaction.json")["phase"] == "recovery_failed"
    assert (tmp_path / "updater" / "maintenance.json").exists()


def test_preparation_failure_leaves_original_service_running(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)

    def cannot_prepare(*args):
        raise module("update_state").UpdateError("installation failed")

    deployment.prepare = cannot_prepare
    manager.apply(request)
    assert deployment.stops == 0
    assert deployment.running == "a" * 40
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_interrupted_transaction_recovers_recorded_previous_version(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)
    state = module("update_state")
    state.write_state(
        manager.folder / "transaction.json",
        {**request, "phase": "switching", "previous": manager.current},
    )
    manager.recover()
    assert deployment.running == "a" * 40
    assert deployment.started == ["a" * 40]
    assert state.read_state(tmp_path / "updater" / "transaction.json")["phase"] == "restored"
    assert not (tmp_path / "updater" / "maintenance.json").exists()


def test_untrusted_request_cannot_select_a_command_or_skip_instance(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)
    request["command"] = "anything"
    with pytest.raises(module("update_state").UpdateError):
        manager.apply(request)
    assert deployment.stops == 0


def test_docker_override_keeps_only_selected_service_and_unique_image(tmp_path):
    deploy = module("update_deploy")
    state = module("update_state")
    adapter = deploy.DockerDeployment(tmp_path, tmp_path / "data", service="spark")
    adapter.instance = "b" * 32
    adapter.boot = "c" * 32
    path = adapter.override({"commit": "a" * 40, "image": "dyspark:update-" + "d" * 32})
    payload = state.read_state(path)
    assert list(payload["services"]) == ["spark"]
    service = payload["services"]["spark"]
    assert service["image"] == "dyspark:update-" + "d" * 32
    assert service["environment"]["SPARK_UPDATE_INSTANCE"] == "b" * 32
    assert service["environment"]["SPARK_UPDATE_BOOT"] == "c" * 32
    assert "volumes" not in service
    assert "command" not in service
    assert "ports" not in service


def test_native_release_validation_does_not_execute_arbitrary_saved_paths(tmp_path):
    deploy = module("update_deploy")
    adapter = deploy.NativeDeployment(
        tmp_path, tmp_path / "data", config=tmp_path / "config.toml", python=Path(os.sys.executable)
    )
    with pytest.raises(module("update_state").UpdateError):
        adapter.start({"commit": "a" * 40, "source": "C:/untrusted", "python": "arbitrary.exe"})


def test_nested_mutations_share_gate_but_maintenance_is_still_exclusive(tmp_path):
    state = module("update_state")
    with state.operation(tmp_path), state.operation(tmp_path):
        assert not (tmp_path / "updater" / "maintenance.json").exists()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from dyspark.gui import create_app
    from dyspark.gui_auth import COOKIE_NAME, PanelAuth, PasswordHash
    from dyspark.panel import Backend

    monkeypatch.setattr(module("updates").UpdateService, "_check_public", lambda self: None)
    monkeypatch.setenv("SPARK_UPDATE_INSTANCE", "b" * 32)
    monkeypatch.setenv("SPARK_UPDATE_BOOT", "d" * 32)
    config = tmp_path / "config.toml"
    config.write_text("[schedule]\nenabled=false\n[mcp]\nenabled=true\n")
    backend = Backend(config, tmp_path / "data")
    auth = PanelAuth(PasswordHash.create("test-password"))
    app = create_app(backend, token="test-token", auth=auth)
    with TestClient(app) as client:
        yield client, app, backend, auth, COOKIE_NAME


def login_panel(panel):
    client, app, backend, auth, cookie = panel
    client.cookies.set(cookie, auth.login("test-password", "testclient", None))
    return client, app, backend


def test_update_routes_enforce_login_and_write_token(panel):
    client, _app, _backend, _auth, _cookie = panel
    assert client.get("/api/update").status_code == 401
    client, _app, _backend = login_panel(panel)
    assert client.get("/api/update").status_code == 200
    assert client.post("/api/update/check").status_code == 403
    assert client.post("/api/update/apply", json={"target": "c" * 40}).status_code == 403
    response = client.post(
        "/api/update/apply",
        json={"target": "c" * 40, "command": "bad"},
        headers={"X-Spark-Token": "test-token"},
    )
    assert response.status_code == 400


def test_health_headers_identify_runtime_even_without_login(panel):
    client, _app, _backend, _auth, _cookie = panel
    response = client.get("/api/meta")
    assert response.status_code == 401
    assert response.headers["X-Spark-Instance"] == "b" * 32
    assert response.headers["X-Spark-Boot"] == "d" * 32
    assert len(response.headers["X-Spark-Commit"]) == 40


def test_gui_and_shared_backend_edits_are_refused_during_maintenance(panel):
    from dyspark.panel import PanelError

    client, _app, backend = login_panel(panel)
    state = module("update_state")
    state.begin_maintenance(backend.settings().data_dir, {"id": "a" * 32}, backend.update_idle)
    response = client.post(
        "/api/settings",
        json={"mcp_enabled": False},
        headers={"X-Spark-Token": "test-token"},
    )
    assert response.status_code in {400, 503}
    assert backend.settings().mcp_enabled is True
    with pytest.raises(PanelError, match="更新"):
        backend.control_mcp(False)


def test_password_change_is_refused_during_maintenance(panel):
    client, _app, backend = login_panel(panel)
    state = module("update_state")
    state.begin_maintenance(backend.settings().data_dir, {"id": "a" * 32}, backend.update_idle)
    response = client.post(
        "/auth/password",
        json={
            "current_password": "test-password",
            "new_password": "another-test-password",
            "confirm_password": "another-test-password",
        },
        headers={"X-Spark-Token": "test-token"},
    )
    assert response.status_code in {400, 503}


def test_job_launch_is_blocked_before_creating_child_or_job_files(tmp_path):
    from datetime import UTC

    from dyspark.jobs import JobError, JobManager
    from dyspark.store import DataDir

    state = module("update_state")
    state.begin_maintenance(tmp_path, {"id": "a" * 32}, lambda: None)
    manager = JobManager(DataDir(tmp_path), tmp_path / "config.toml", UTC)
    with pytest.raises(JobError, match="更新"):
        manager.start("check", ["check"])
    assert not list((tmp_path / "jobs").glob("check-*"))


def test_cli_maintenance_refusal_reports_busy_without_opening_browser(tmp_path):
    state = module("update_state")
    state.begin_maintenance(tmp_path / "data", {"id": "a" * 32}, lambda: None)
    config = tmp_path / "config.toml"
    config.write_text("")
    result = subprocess.run(
        [os.sys.executable, "-m", "dyspark", "--config", str(config), "check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 75
    assert not list((tmp_path / "data" / "runs").glob("*"))


def test_native_service_runs_with_identity_and_stops_gracefully(tmp_path):
    import socket

    from dyspark.updates import current_commit

    state = module("update_state")
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "配置 config.toml"
    config.write_text("[schedule]\nenabled=false\n")
    data = tmp_path / "数据 data"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    deployment = module("update_deploy").NativeDeployment(
        root,
        data,
        config=config,
        python=Path(os.sys.executable),
        port=port,
    )
    deployment.instance = "b" * 32
    state.write_state(
        state.state_dir(data) / "heartbeat.json",
        {"instance": "b" * 32, "time": time.time()},
    )
    try:
        deployment.start(deployment.initial(current_commit()))
        deployment.wait_healthy()
        assert deployment.alive()
        deployment.stop()
        assert not deployment.alive()
        assert (data / "gui-auth.json").is_file()
    finally:
        deployment.close()


def test_docker_execution_files_are_outside_container_writable_data(tmp_path):
    deploy = module("update_deploy")
    adapter = deploy.DockerDeployment(tmp_path, tmp_path / "data")
    path = adapter.override({"commit": "a" * 40, "image": "dyspark:update-" + "d" * 32})
    assert not path.is_relative_to(tmp_path / "data")


def test_public_transaction_does_not_expose_authoritative_execution_paths(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)
    manager.record(request, "switching", previous=manager.current)
    public = module("update_state").read_state(tmp_path / "updater" / "transaction.json")
    private = module("update_state").read_state(manager.folder / "transaction.json")
    assert "previous" not in public
    assert private["previous"]["commit"] == "a" * 40
    assert not manager.folder.is_relative_to(tmp_path / "updater")


def test_concurrent_managers_cannot_replace_owner_or_control_running_service(tmp_path):
    import threading

    state = module("update_state")
    manager_module = module("update_manager")
    deployment = FakeDeployment()
    private = tmp_path / "host-private"
    first = manager_module.UpdateManager(
        tmp_path / "data", FakeSource(), deployment, private=private
    )
    ready = threading.Event()
    deployment.wait_healthy = ready.set
    errors = []

    def run():
        try:
            first.run(identity={"mode": "test"})
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        owner = state.read_state(private / "owner.json")
        second_deployment = FakeDeployment()
        second = manager_module.UpdateManager(
            tmp_path / "data", FakeSource(), second_deployment, private=private
        )
        with pytest.raises(module("locking").LockBusyError):
            second.run(identity={"mode": "test"})
        assert state.read_state(private / "owner.json") == owner
        assert second_deployment.stops == 0
    finally:
        first.stop_event.set()
        thread.join(timeout=5)
    assert not errors


def test_slow_parent_metadata_write_does_not_make_child_reject_its_own_launch(
    tmp_path, monkeypatch
):
    from datetime import UTC

    from dyspark.jobs import JobManager
    from dyspark.store import DataDir

    jobs = module("jobs")
    original = jobs.write_json_atomic

    def slow_metadata(path, payload):
        if path.name == "job.json":
            time.sleep(1)
        original(path, payload)

    monkeypatch.setattr(jobs, "write_json_atomic", slow_metadata)
    config = tmp_path / "config.toml"
    config.write_text("")
    manager = JobManager(DataDir(tmp_path / "data"), config, UTC)
    manager.enable_service()
    try:
        job = manager.start("check", ["check"])
        result = manager.wait(job, 15)
        assert result["exit_code"] == 0
        assert list((tmp_path / "data" / "runs").glob("*"))
    finally:
        manager.shutdown(grace_s=0.1)


def test_native_preserves_virtualenv_python_symlinks(tmp_path):
    alias = tmp_path / "venv-python"
    try:
        alias.symlink_to(os.sys.executable)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    adapter = module("update_deploy").NativeDeployment(
        tmp_path, tmp_path / "data", config=tmp_path / "config.toml", python=alias
    )
    assert adapter.python == alias.absolute()


def test_docker_rejects_image_without_managed_repository_prefix(tmp_path):
    adapter = module("update_deploy").DockerDeployment(tmp_path, tmp_path / "data")
    with pytest.raises(module("update_state").UpdateError):
        adapter.override({"commit": "a" * 40, "image": "d" * 32})


def test_completed_transaction_survives_crash_before_maintenance_release(tmp_path):
    deployment = FakeDeployment()
    manager, request = transaction_manager(tmp_path, deployment)
    state = module("update_state")
    candidate = {"commit": "c" * 40, "source": "candidate"}
    state.write_state(manager.folder / "current.json", candidate)
    manager.record(request, "complete", previous=manager.current)
    deployment.alive = lambda: manager.stop_event.set() or True
    manager.run()
    assert deployment.started == ["c" * 40]
    assert state.read_state(tmp_path / "updater" / "transaction.json")["phase"] == "complete"
    assert not (tmp_path / "updater" / "maintenance.json").exists()


@pytest.mark.parametrize("service", ["local", "spark"])
def test_real_compose_merge_preserves_deployment_settings_without_daemon(tmp_path, service):
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    config = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    shutil.copy2(config, tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("")
    adapter = module("update_deploy").DockerDeployment(tmp_path, tmp_path / "data", service=service)
    adapter.instance = "b" * 32
    adapter.boot = "c" * 32
    adapter.owner = "d" * 32
    override = adapter.override({"commit": "a" * 40, "image": "dyspark:update-" + "e" * 32})
    base = json.loads(adapter.compose("config", "--format", "json"))["services"][service]
    merged = json.loads(adapter.compose("config", "--format", "json", override=override))[
        "services"
    ][service]
    for key in ("command", "volumes", "ports", "network_mode", "restart"):
        assert merged.get(key) == base.get(key)
    assert (
        merged["environment"]["FORWARDED_ALLOW_IPS"] == base["environment"]["FORWARDED_ALLOW_IPS"]
    )
    assert merged["environment"]["SPARK_UPDATE_INSTANCE"] == "b" * 32
    assert merged["labels"]["io.dyspark.updater"] == "d" * 32
    adapter.validate_config()
