"""Fast local controller qualification; dummy callbacks and Python processes only."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from controller import CheckQuota, MANIFEST_NAME, QuotaExceeded, UnsafeSnapshot, run_bounded, snapshot_output


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="deng-controller-test-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "candidate"
        self.workspace.mkdir()
        self.output = self.workspace / "output"
        self.output.mkdir()
        self.operator = self.root / "operator"
        self.operator.mkdir()

    def tearDown(self):
        # The operator can deliberately restore write permissions for cleanup.
        for directory, _, files in os.walk(self.root):
            os.chmod(directory, 0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)
        self.temporary.cleanup()

    def quota(self):
        return CheckQuota(self.operator / "quota.json", self.workspace)

    def test_four_checks_persist_and_fifth_does_not_invoke(self):
        for index in range(4):
            self.assertEqual(self.quota().invoke(lambda value: value, index), index)
        with self.assertRaises(QuotaExceeded):
            self.quota().invoke(lambda: self.fail("Fifth callback was invoked"))
        self.assertEqual([item["status"] for item in self.quota().inspect()["checks"]], ["completed"] * 4)

    def test_concurrent_instances_allow_exactly_four(self):
        quotas = [self.quota() for _ in range(12)]
        barrier, callback_lock = threading.Barrier(len(quotas)), threading.Lock()
        calls = []

        def attempt(index):
            barrier.wait(timeout=5)
            def dummy():
                with callback_lock:
                    calls.append(index)
                return index
            try:
                quotas[index].invoke(dummy)
                return True
            except QuotaExceeded:
                return False

        with ThreadPoolExecutor(max_workers=len(quotas)) as pool:
            results = list(pool.map(attempt, range(len(quotas))))
        self.assertEqual(sum(results), 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual([item["id"] for item in self.quota().inspect()["checks"]], [1, 2, 3, 4])

    def test_failed_and_abandoned_reservations_consume_quota(self):
        quota = self.quota()
        def fail():
            raise RuntimeError("dummy error")
        with self.assertRaises(RuntimeError):
            quota.invoke(fail)
        quota.reserve()  # Simulate a crash after durable reservation.
        quota.invoke(lambda: None)
        quota.invoke(lambda: None)
        with self.assertRaises(QuotaExceeded):
            self.quota().reserve()
        self.assertEqual([item["status"] for item in quota.inspect()["checks"]],
                         ["error", "reserved", "completed", "completed"])

    def test_independent_processes_share_four_reservations(self):
        module_dir = str(Path(__file__).resolve().parent)
        gate = self.operator / "start"
        script = ("import sys,time,pathlib; "
                  f"sys.path.insert(0,{module_dir!r}); "
                  "from controller import CheckQuota,QuotaExceeded; "
                  f"quota=CheckQuota({str(self.operator / 'quota.json')!r},{str(self.workspace)!r}); "
                  f"gate=pathlib.Path({str(gate)!r})"
                  "\nwhile not gate.exists(): time.sleep(.005)"
                  "\ntry: quota.invoke(lambda: None)"
                  "\nexcept QuotaExceeded: sys.exit(9)")
        processes = []
        try:
            for _ in range(8):
                processes.append(subprocess.Popen([sys.executable, "-c", script]))
            gate.write_text("go")
            codes = [process.wait(timeout=8) for process in processes]
            self.assertEqual(codes.count(0), 4)
            self.assertEqual(codes.count(9), 4)
            self.assertEqual(len(self.quota().inspect()["checks"]), 4)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_ledger_cannot_be_inside_workspace_or_reset_when_malformed(self):
        with self.assertRaises(ValueError):
            CheckQuota(self.workspace / "quota.json", self.workspace)
        quota = self.quota()
        (self.operator / "quota.json").write_text("{}")
        with self.assertRaises(ValueError):
            quota.reserve()

    def test_ledger_and_lock_symlinks_are_rejected(self):
        real = self.operator / "real"
        real.write_text("{}")
        ledger = self.operator / "quota.json"
        ledger.symlink_to(real)
        with self.assertRaises(OSError):
            self.quota()
        ledger.unlink()
        lock = self.operator / "quota.json.lock"
        lock.unlink()
        lock.symlink_to(real)
        with self.assertRaises(OSError):
            self.quota()

    def test_snapshot_hashes_nested_files_and_read_only_modes(self):
        (self.output / "part.step").write_bytes(b"dummy STEP bytes\n")
        (self.output / "sub").mkdir()
        (self.output / "sub" / "empty").write_bytes(b"")
        target = self.operator / "snapshot"
        manifest = snapshot_output(self.output, target, candidate_workspace=self.workspace)
        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["files"]["part.step"]["sha256"], hashlib.sha256(b"dummy STEP bytes\n").hexdigest())
        self.assertEqual(json.loads((target / MANIFEST_NAME).read_text()), manifest)
        self.assertEqual(stat.S_IMODE((target / "part.step").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o500)
        (self.output / "part.step").write_bytes(b"candidate changed later")
        self.assertEqual((target / "part.step").read_bytes(), b"dummy STEP bytes\n")

    def test_snapshot_rejects_symlinks_in_tree_and_source_path(self):
        (self.output / "link").symlink_to(self.operator / "missing")
        with self.assertRaises(UnsafeSnapshot):
            snapshot_output(self.output, self.operator / "bad1", candidate_workspace=self.workspace)
        self.assertFalse((self.operator / "bad1" / MANIFEST_NAME).exists())
        linked = self.workspace / "linked-output"
        linked.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(OSError):
            snapshot_output(linked, self.operator / "bad2", candidate_workspace=self.workspace)

    def test_snapshot_rejects_special_files(self):
        os.mkfifo(self.output / "fifo")
        with self.assertRaises(UnsafeSnapshot):
            snapshot_output(self.output, self.operator / "bad", candidate_workspace=self.workspace)

    def test_snapshot_rejects_traversal_existing_target_and_candidate_destination(self):
        with self.assertRaises(ValueError):
            snapshot_output(str(self.output) + "/../output", self.operator / "bad", candidate_workspace=self.workspace)
        with self.assertRaises(ValueError):
            snapshot_output(self.output, self.workspace / "bad", candidate_workspace=self.workspace)
        with self.assertRaises(FileExistsError):
            snapshot_output(self.output, self.operator, candidate_workspace=self.workspace)

    def test_bounded_normal_process(self):
        result = run_bounded([sys.executable, "-c", "print('dummy completed')"], timeout_seconds=3)
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["stdout"].strip(), "dummy completed")
        self.assertEqual(result["containment"], "process_group_only")

    def test_group_probe_permission_error_does_not_skip_reaping(self):
        original = os.killpg
        def restricted_probe(pgid, number):
            if number == 0:
                raise PermissionError(1, "injected group probe denial")
            return original(pgid, number)
        with patch("controller.os.killpg", side_effect=restricted_probe):
            result = run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                                 timeout_seconds=0.15, termination_grace_seconds=0.05)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["leader_reaped"])
        self.assertEqual(result["group_cleanup_status"], "inconclusive")
        self.assertTrue(any(item["operation"] == "probe_process_group" for item in result["cleanup_errors"]))

    def test_group_signal_permission_error_uses_direct_leader_cleanup(self):
        with patch("controller.os.killpg", side_effect=PermissionError(1, "injected group signal denial")):
            result = run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                                 timeout_seconds=0.15, termination_grace_seconds=0.05)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["leader_reaped"])
        self.assertEqual(result["group_cleanup_status"], "inconclusive")
        self.assertTrue(result["cleanup_errors"])

    def test_timeout_kills_child_ignoring_sigterm_even_after_leader_dies(self):
        marker = self.operator / "child-survived"
        ready = self.operator / "child-ready"
        child = ("import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(1.5); "
                 f"pathlib.Path({str(marker)!r}).write_text('survived')")
        parent = ("import subprocess,sys,time,pathlib; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  f"ready=pathlib.Path({str(ready)!r}); "
                  "\nwhile not ready.exists(): time.sleep(.005)"
                  "\ntime.sleep(30)")
        result = run_bounded([sys.executable, "-c", parent], timeout_seconds=0.7,
                             termination_grace_seconds=0.05)
        self.assertTrue(ready.exists(), "Child did not reach its signal handler")
        self.assertTrue(result["timed_out"])
        self.assertIn("SIGKILL", result["cleanup_signals"])
        time.sleep(1.0)
        self.assertFalse(marker.exists())

    def test_setsid_escape_is_reported_and_explicitly_cleaned_by_test(self):
        pid_file = self.operator / "escaped-pid"
        ready_file = self.operator / "escaped-ready"
        # Record PID before permitting escape. If the parent dies before this
        # handoff, the child still belongs to the controller's process group.
        child = ("import os,time,pathlib; "
                 f"pid_file=pathlib.Path({str(pid_file)!r})"
                 "\nwhile not pid_file.exists(): time.sleep(.005)"
                 "\nos.setsid()"
                 f"\npathlib.Path({str(ready_file)!r}).write_text('escaped')"
                 "\ntime.sleep(10)")
        parent = ("import subprocess,sys,time,pathlib; "
                  f"child=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
                  f"ready=pathlib.Path({str(ready_file)!r}); "
                  "\nwhile not ready.exists(): time.sleep(.005)"
                  "\ntime.sleep(30)")
        escaped_pid = None
        try:
            result = run_bounded([sys.executable, "-c", parent], timeout_seconds=0.7,
                                 termination_grace_seconds=0.05)
            self.assertTrue(pid_file.exists())
            escaped_pid = int(pid_file.read_text())
            self.assertTrue(ready_file.exists(), "Child did not complete setsid()")
            os.kill(escaped_pid, 0)  # Deliberately demonstrate the primitive's boundary.
            self.assertTrue(result["timed_out"])
            self.assertTrue(any("setsid()" in item for item in result["limitations"]))
        finally:
            if escaped_pid is None and pid_file.exists():
                escaped_pid = int(pid_file.read_text())
            if escaped_pid is not None:
                try:
                    os.kill(escaped_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
