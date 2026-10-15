import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyspark import store
from dyspark.locking import file_lock
from dyspark.scheduler import Scheduler


class SchedulerRestartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.marker = self.root / "scheduler.running"

    def leave_marker(self, **changes):
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "since": 1.0,
            "identity": "previous-owner",
        }
        owner.update(changes)
        self.marker.mkdir()
        (self.marker / "owner.json").write_text(json.dumps(owner), encoding="utf-8")

    def test_reclaims_legacy_marker_when_live_pid_was_reused(self):
        self.leave_marker()
        with patch.object(
            store, "_process_start", return_value=("new-process", 100.0), create=True
        ):
            self.assertIsNone(store.marker_owner(self.marker))
            with store.exclusive_run(self.marker):
                owner = json.loads((self.marker / "owner.json").read_text(encoding="utf-8"))
                self.assertNotEqual(owner["identity"], "previous-owner")
        self.assertFalse(self.marker.exists())

    def test_reclaims_marker_when_process_generation_changed(self):
        self.leave_marker(since=time.time(), process_identity="old-process")
        with (
            patch.object(store, "_process_start", return_value=("new-process", 100.0), create=True),
            store.exclusive_run(self.marker),
        ):
            self.assertIsNotNone(store.marker_owner(self.marker))

    def test_records_process_generation_for_future_restarts(self):
        with (
            patch.object(
                store, "_process_start", return_value=("current-process", 100.0), create=True
            ),
            store.exclusive_run(self.marker),
        ):
            owner = json.loads((self.marker / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner.get("process_identity"), "current-process")

    def test_matching_generation_remains_busy_even_when_marker_is_old(self):
        self.leave_marker(process_identity="current-process")
        with (
            patch.object(
                store, "_process_start", return_value=("current-process", 100.0), create=True
            ),
            self.assertRaises(store.RunBusyError),
            store.exclusive_run(self.marker),
        ):
            self.fail("a live owner must not be replaced")
        self.assertTrue(self.marker.exists())

    def test_legacy_live_owner_is_preserved_when_process_start_is_unavailable(self):
        self.leave_marker()
        with (
            patch.object(store, "_process_start", return_value=None, create=True),
            self.assertRaises(store.RunBusyError),
            store.exclusive_run(self.marker),
        ):
            self.fail("unknown process metadata must not evict a live owner")

    def test_legacy_owner_started_before_marker_remains_busy(self):
        self.leave_marker(since=101.0)
        with (
            patch.object(
                store, "_process_start", return_value=("current-process", 100.0), create=True
            ),
            self.assertRaises(store.RunBusyError),
            store.exclusive_run(self.marker),
        ):
            self.fail("an existing process must not be replaced")

    def test_another_process_cannot_take_active_os_lock(self):
        code = (
            "import sys; from pathlib import Path; "
            "from dyspark.store import exclusive_run, RunBusyError\n"
            "try:\n"
            "    with exclusive_run(Path(sys.argv[1])): sys.exit(1)\n"
            "except RunBusyError: sys.exit(0)\n"
        )
        with store.exclusive_run(self.marker):
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.marker)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.marker.exists())

    def test_active_os_lock_remains_busy_across_process_namespaces(self):
        with (
            patch.object(store, "_process_start", return_value=("holder-namespace", 100.0)),
            store.exclusive_run(self.marker),
            patch.object(store, "_process_start", return_value=("observer-namespace", 200.0)),
        ):
            self.assertIsNotNone(store.marker_owner(self.marker))

    def test_active_os_lock_is_busy_before_owner_marker_is_written(self):
        with file_lock(self.marker.with_name(".scheduler.running.lock")):
            self.assertIsNotNone(store.marker_owner(self.marker))

    def test_unused_data_directory_is_free_without_creating_lock_files(self):
        self.assertIsNone(store.marker_owner(self.marker))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_enabled_schedule_resumes_without_repeating_completed_catchup(self):
        now = datetime(2026, 10, 15, 17, tzinfo=UTC)
        calls = []
        settings = SimpleNamespace(
            data_dir=self.root,
            zone=UTC,
            schedule_enabled=True,
            schedule_times=["08:30", "20:30"],
            timing=SimpleNamespace(jitter_minutes=0),
        )

        def dispatch(**kwargs):
            calls.append(kwargs)
            return {"job_id": "catchup", "state": "finished", "exit_code": 0}

        backend = SimpleNamespace(
            settings=lambda: settings,
            jobs=lambda _settings: SimpleNamespace(active=lambda: []),
            run=dispatch,
        )
        for _ in range(2):
            scheduler = Scheduler(backend, clock=lambda: now, poll_seconds=0.01)
            try:
                scheduler.start()
                status = scheduler.status()
                self.assertTrue(status["leader"], status)
                self.assertIsNone(status["error"])
                self.assertEqual(status["next_run"], "2026-10-15T20:30:00+00:00")
            finally:
                scheduler.stop()
        self.assertEqual(
            calls,
            [{"account": None, "dry_run": False, "wait_seconds": 0, "trigger": "scheduler"}],
        )

    @unittest.skipUnless(sys.platform == "linux", "requires Linux process metadata")
    def test_recovers_real_linux_legacy_marker_without_mocking_process_identity(self):
        self.leave_marker()
        with store.exclusive_run(self.marker):
            self.assertIsNotNone(store.marker_owner(self.marker))

    @unittest.skipUnless(sys.platform == "linux", "requires Linux process metadata")
    def test_scheduler_becomes_leader_after_container_pid_reuse(self):
        self.leave_marker()
        settings = SimpleNamespace(
            data_dir=self.root, zone=UTC, schedule_enabled=False, schedule_times=[]
        )
        backend = SimpleNamespace(
            settings=lambda: settings,
            jobs=lambda _settings: SimpleNamespace(active=lambda: []),
            run=lambda **_kwargs: self.fail("disabled schedule must not dispatch"),
        )
        scheduler = Scheduler(backend, poll_seconds=0.01)
        self.addCleanup(scheduler.stop)
        scheduler.start()
        status = scheduler.status()
        self.assertTrue(status["leader"], status)
        self.assertIsNone(status["error"])
        scheduler.stop()
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
