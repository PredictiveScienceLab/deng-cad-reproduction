import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import worker_supervisor
from worker_supervisor import run_worker


class WorkerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deng-worker-tests-",
                                                 dir=Path(__file__).resolve().parent)
        self.cwd = Path(self.temp.name).resolve()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        self.temp.cleanup()

    def run_code(self, source, **kwargs):
        settings = dict(env=self.env, cwd=self.cwd, timeout_seconds=3,
                        absolute_deadline_monotonic=time.monotonic()+3,
                        termination_grace_seconds=0.05, reap_timeout_seconds=2)
        settings.update(kwargs)
        return run_worker([sys.executable, "-c", source], **settings)

    def test_normal_worker_and_nonzero_exit(self):
        good = self.run_code("print('complete')")
        self.assertTrue(good["success"])
        self.assertTrue(good["worker_reaped"])
        self.assertEqual(good["stdout"], "complete\n")
        bad = self.run_code("raise SystemExit(7)")
        self.assertFalse(bad["success"])
        self.assertEqual(bad["returncode"], 7)

    def test_launch_failure_is_not_success(self):
        result = run_worker([str(self.cwd / "absent")], env=self.env, cwd=self.cwd, timeout_seconds=1,
                            absolute_deadline_monotonic=time.monotonic()+1)
        self.assertEqual(result["status"], "launch_failed")
        self.assertFalse(result["launched"])

    def test_trial_deadline_expiry_prevents_launch(self):
        result = self.run_code("raise AssertionError('must not launch')",
                               absolute_deadline_monotonic=time.monotonic()-1)
        self.assertEqual(result["status"], "deadline_expired")
        self.assertFalse(result["launched"])

    def test_trial_deadline_shortens_worker_allowance(self):
        result = self.run_code("import time; time.sleep(10)", timeout_seconds=8,
                               absolute_deadline_monotonic=time.monotonic()+0.2)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["deadline_source"], "trial")
        self.assertTrue(result["worker_reaped"])
        self.assertLess(result["elapsed_seconds"], 2)

    def test_preexisting_cancellation_prevents_launch(self):
        event = threading.Event()
        event.set()
        result = self.run_code("raise AssertionError('must not launch')", cancel_event=event)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["launched"])

    def test_cancellation_terminates_running_worker(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        try:
            result = self.run_code("import time; time.sleep(10)", cancel_event=event)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["worker_reaped"])

    def test_exception_cancellation_still_reaps_worker(self):
        created = []
        original = subprocess.Popen
        def capture_process(*args, **kwargs):
            result = original(*args, **kwargs)
            created.append(result)
            return result
        with patch("worker_supervisor.subprocess.Popen", side_effect=capture_process):
            with patch("worker_supervisor._await_worker", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_code("import time; time.sleep(10)")
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    def test_output_overflow_stops_worker_and_bounds_storage(self):
        result = self.run_code("import os,time; os.write(1,b'x'*1000000); time.sleep(10)",
                               output_limit_bytes=1024)
        self.assertEqual(result["status"], "output_limit_exceeded")
        self.assertFalse(result["success"])
        self.assertTrue(result["worker_reaped"])
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["output_bytes_stored"], 1024)
        self.assertEqual(len(result["stdout"]), 1024)

    def test_output_limit_is_aggregate_and_exit_zero_does_not_hide_overflow(self):
        result = self.run_code("import os; os.write(1,b'a'*800); os.write(2,b'b'*800)",
                               output_limit_bytes=1024)
        self.assertEqual(result["status"], "output_limit_exceeded")
        self.assertFalse(result["success"])
        self.assertEqual(result["output_bytes_stored"], 1024)
        self.assertEqual(len(result["stdout"])+len(result["stderr"]), 1024)

    def test_bounded_stdin_bytes_are_delivered_without_argv_source(self):
        data = b"x" * 262144
        result = self.run_code("import sys; data=sys.stdin.buffer.read(); print(len(data))", stdin_bytes=data)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"].strip(), str(len(data)))
        self.assertEqual(result["stdin_bytes_written"], len(data))

    def test_stdin_over_limit_and_timeout_over_limit_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_code("pass", stdin_bytes=b"x" * 262145)
        with self.assertRaises(ValueError):
            self.run_code("pass", timeout_seconds=61)

    def test_worker_ignoring_stdin_is_still_timed_out_and_reaped(self):
        result = self.run_code("import time; time.sleep(10)", stdin_bytes=b"x" * 262144,
                               timeout_seconds=0.2)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["worker_reaped"])
        self.assertFalse(result["success"])

    def test_worker_early_exit_does_not_claim_all_input_was_delivered(self):
        result = self.run_code("pass", stdin_bytes=b"x" * 262144)
        self.assertEqual(result["status"], "input_failed")
        self.assertFalse(result["success"])
        self.assertLess(result["stdin_bytes_written"], result["stdin_bytes_supplied"])

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt qualification")
    def test_real_seatbelt_blocks_child_creation_preserves_threads(self):
        profile = "(version 1)\n(allow default)\n(deny process-fork)\n"
        code = """
import errno,json,os,threading
result={}
try:
    pid=os.fork()
    if pid==0: os._exit(0)
    os.waitpid(pid,0)
    result['fork_denied']=False
except OSError as e: result['fork_denied']=e.errno==errno.EPERM
try:
    pid=os.posix_spawn('/usr/bin/true',['true'],os.environ,setsid=True)
    os.waitpid(pid,0)
    result['spawn_denied']=False
except OSError as e: result['spawn_denied']=e.errno==errno.EPERM
flag=[]
t=threading.Thread(target=lambda:flag.append(True)); t.start(); t.join()
result['thread_allowed']=flag==[True]
print(json.dumps(result))
"""
        result = run_worker(["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
                            env=self.env, cwd=self.cwd, timeout_seconds=3,
                            absolute_deadline_monotonic=time.monotonic()+3)
        self.assertTrue(result["success"], result)
        self.assertTrue(all(json.loads(result["stdout"]).values()))

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt qualification")
    def test_real_nofork_worker_setsid_and_sigterm_ignore_still_killed(self):
        profile = "(version 1)\n(allow default)\n(deny process-fork)\n"
        code = "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(10)"
        result = run_worker(["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
                            env=self.env, cwd=self.cwd, timeout_seconds=0.5,
                            absolute_deadline_monotonic=time.monotonic()+3,
                            termination_grace_seconds=0.05)
        self.assertEqual(result["stdout"].strip(), "ready", result)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["returncode"], -signal.SIGKILL)
        self.assertIn("SIGKILL", result["cleanup_signals"])
        self.assertTrue(result["worker_reaped"])
        self.assertFalse(result["controller_death_protection"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
