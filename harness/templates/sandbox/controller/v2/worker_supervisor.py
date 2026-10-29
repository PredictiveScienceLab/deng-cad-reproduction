"""Direct-PID supervision for externally sandboxed, no-fork candidate workers.

The trusted caller MUST launch candidate code behind the frozen Seatbelt policy
that denies process-fork, external signals, and out-of-process execution routes.
This module does not apply or certify that policy. It holds the unreaped direct
child PID, so setsid/setpgid and exec do not change the supervised identity.

The caller/broker must remain alive. This primitive cannot survive SIGKILL or a
crash of its trusted owning process; an independent deadline supervisor would
need to own the worker, not merely poll a potentially reused PID. Candidate code
must have no ability to signal, modify, or replace this trusted owner.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Any


MAX_STDIN_BYTES = 262_144
MAX_WORKER_TIMEOUT_SECONDS = 60.0


def _error(operation: str, error: BaseException) -> dict[str, Any]:
    return {"operation": operation, "type": type(error).__name__,
            "errno": getattr(error, "errno", None), "message": str(error)}


def _stop_and_reap(process: subprocess.Popen, grace: float, reap_timeout: float) -> dict:
    record = {"signals": [], "errors": [], "reaped": False}
    if process.poll() is not None:
        record["reaped"] = True
        return record
    try:
        process.terminate()
        record["signals"].append("SIGTERM")
    except ProcessLookupError:
        pass
    except OSError as error:
        record["errors"].append(_error("terminate_worker", error))
    try:
        process.wait(timeout=grace)
        record["reaped"] = True
        return record
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
        record["signals"].append("SIGKILL")
    except ProcessLookupError:
        pass
    except OSError as error:
        record["errors"].append(_error("kill_worker", error))
    try:
        process.wait(timeout=reap_timeout)
        record["reaped"] = True
    except subprocess.TimeoutExpired as error:
        record["errors"].append(_error("reap_worker", error))
    return record


class _Capture:
    """Bound aggregate temporary-file bytes and stop on the first excess byte.

    OS pipes provide backpressure. Readers store at most limit bytes in total;
    they stop draining upon overflow, and the supervisor kills the worker.
    This bounds retained output without relying on a polling disk-size quota.
    """

    def __init__(self, limit: int, stdout_file, stderr_file):
        self.limit = limit
        self.files = {"stdout": stdout_file, "stderr": stderr_file}
        self.lock = threading.Lock()
        self.limit_hit = threading.Event()
        self.failed = threading.Event()
        self.total_stored = 0
        self.stored = {"stdout": 0, "stderr": 0}
        self.observed = {"stdout": 0, "stderr": 0}
        self.errors = []
        self.threads = []

    def _reader(self, label: str, source) -> None:
        try:
            while not self.limit_hit.is_set() and not self.failed.is_set():
                chunk = os.read(source.fileno(), 65536)
                if not chunk:
                    break
                with self.lock:
                    self.observed[label] += len(chunk)
                    count = min(len(chunk), max(0, self.limit - self.total_stored))
                    if count:
                        self.files[label].write(chunk[:count])
                        self.stored[label] += count
                        self.total_stored += count
                    if count != len(chunk):
                        self.limit_hit.set()
                        break
        except BaseException as error:
            with self.lock:
                self.errors.append(_error("capture_" + label, error))
            self.failed.set()

    def start(self, process: subprocess.Popen) -> None:
        for label, source in (("stdout", process.stdout), ("stderr", process.stderr)):
            thread = threading.Thread(target=self._reader, args=(label, source),
                                      name="candidate-capture-" + label, daemon=True)
            thread.start()
            self.threads.append(thread)

    def finish(self, process: subprocess.Popen, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        finished = all(not thread.is_alive() for thread in self.threads)
        if finished:
            process.stdout.close()
            process.stderr.close()
        else:
            self.errors.append({"operation": "finish_capture", "type": "TimeoutExpired",
                                "errno": None, "message": "Output descriptors did not close after worker termination; no-fork/IPC policy may be violated"})
            self.failed.set()
        return finished

    def result(self) -> dict:
        result = {"output_bytes_stored": self.total_stored,
                  "output_bytes_observed": dict(self.observed),
                  "output_limit_bytes": self.limit,
                  "output_truncated": self.limit_hit.is_set(),
                  "capture_errors": list(self.errors)}
        for label, stream in self.files.items():
            stream.flush()
            stream.seek(0)
            result[label] = stream.read(self.limit).decode("utf-8", errors="replace")
        return result


class _Input:
    def __init__(self, data: bytes | None):
        self.data = data
        self.written = 0
        self.error = None
        self.failed = threading.Event()
        self.thread = None

    def _writer(self, destination) -> None:
        try:
            remaining = memoryview(self.data)
            while remaining:
                count = os.write(destination.fileno(), remaining[:65536])
                if count <= 0:
                    raise OSError("Worker input pipe made no progress")
                self.written += count
                remaining = remaining[count:]
        except BaseException as error:
            self.error = _error("write_worker_input", error)
            self.failed.set()
        finally:
            try:
                destination.close()
            except OSError as error:
                if self.error is None:
                    self.error = _error("close_worker_input", error)
                    self.failed.set()

    def start(self, process: subprocess.Popen) -> None:
        if self.data is not None:
            self.thread = threading.Thread(target=self._writer, args=(process.stdin,),
                                           name="candidate-input", daemon=True)
            self.thread.start()

    def finish(self, timeout: float) -> bool:
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            if self.thread.is_alive():
                self.error = {"operation": "finish_worker_input", "type": "TimeoutExpired",
                              "errno": None, "message": "Worker input pipe remained open after termination"}
                self.failed.set()
                return False
        return True


def _await_worker(process: subprocess.Popen, capture: _Capture, deadline: float,
                  cancel_event: threading.Event | None, supplied_input: _Input) -> str:
    while True:
        returncode = process.poll()
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if capture.failed.is_set():
            return "capture_failed"
        if capture.limit_hit.is_set():
            return "output_limit_exceeded"
        if supplied_input.failed.is_set():
            return "input_failed"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timed_out"
        # Observe completion before the deadline; if scheduling delays make
        # that uncertain, the conservative result is timeout, not success.
        if returncode is not None:
            return "completed"
        try:
            process.wait(timeout=min(0.02, remaining))
        except subprocess.TimeoutExpired:
            pass


def run_worker(argv: list[str], *, env: dict[str, str], cwd: str | Path,
               timeout_seconds: float, absolute_deadline_monotonic: float,
               termination_grace_seconds: float = 0.2, reap_timeout_seconds: float = 2.0,
               output_limit_bytes: int = 1_048_576,
               stdin_bytes: bytes | None = None,
               cancel_event: threading.Event | None = None,
               start_new_session: bool = False) -> dict:
    """Run one already-sandboxed worker and return a fail-closed result record.

    argv should begin with the trusted sandbox launcher and frozen policy.
    env is explicit; no parent environment is implicitly merged. The effective
    deadline is min(start+timeout, absolute_deadline_monotonic). Each call is at
    most 60 seconds; the caller must supply its shared absolute trial deadline.
    Optional stdin_bytes is delivered through a pipe and limited to 256 KiB;
    candidate code never inherits the broker's stdin. An expired trial
    deadline or preexisting cancellation prevents launch. Shutdown/reaping adds
    its configured grace and reap allowance. OS process creation and scheduling
    do not provide hard real-time guarantees.

    success is true only for an exit-0 completed worker with confirmed reaping,
    complete output collection, and no cleanup/capture failure. Callers must
    refuse output admission on every other result. BaseException cancellation
    still cleans up the child before propagating to the caller.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("argv must be a nonempty sequence of strings")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("env must be an explicit string-to-string mapping")
    if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    for value, name, allow_zero in ((timeout_seconds, "timeout_seconds", False),
                                   (termination_grace_seconds, "termination_grace_seconds", True),
                                   (reap_timeout_seconds, "reap_timeout_seconds", False)):
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError("Invalid " + name)
    if timeout_seconds > MAX_WORKER_TIMEOUT_SECONDS:
        raise ValueError("Worker timeout exceeds the 60-second per-call maximum")
    if not math.isfinite(absolute_deadline_monotonic):
        raise ValueError("Absolute deadline must be finite")
    if stdin_bytes is not None and (not isinstance(stdin_bytes, bytes) or len(stdin_bytes) > MAX_STDIN_BYTES):
        raise ValueError("stdin_bytes must be bytes of at most 256 KiB")
    if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int) or output_limit_bytes < 1:
        raise ValueError("output_limit_bytes must be a positive integer")

    started = time.monotonic()
    local_deadline = started + timeout_seconds
    deadline = min(local_deadline, absolute_deadline_monotonic)
    report = {"schema_version": 1, "success": False, "status": "not_started",
              "launched": False, "pid": None, "returncode": None, "worker_reaped": None,
              "timeout_seconds": timeout_seconds, "effective_deadline_monotonic": deadline,
              "deadline_source": "trial" if deadline < local_deadline else "worker",
              "containment": "direct_unreaped_child_pid_requires_kernel_no_fork_policy",
              "controller_death_protection": False, "cleanup_signals": [], "cleanup_errors": [],
              "stdout": "", "stderr": "", "output_bytes_stored": 0,
              "output_limit_bytes": output_limit_bytes, "output_truncated": False,
              "capture_errors": [], "stdin_bytes_supplied": len(stdin_bytes or b""),
              "stdin_bytes_written": 0, "stdin_error": None}
    if deadline <= started or (cancel_event is not None and cancel_event.is_set()):
        report["status"] = "deadline_expired" if deadline <= started else "cancelled"
        report["elapsed_seconds"] = time.monotonic() - started
        return report

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        capture = _Capture(output_limit_bytes, stdout_file, stderr_file)
        supplied_input = _Input(stdin_bytes)
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env,
                                       stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=start_new_session, close_fds=True)
        except OSError as error:
            report.update(status="launch_failed", launch_error=_error("launch_worker", error),
                          elapsed_seconds=time.monotonic() - started)
            return report
        report.update(launched=True, pid=process.pid)
        cleanup = {"signals": [], "errors": [], "reaped": False}
        readers_finished = False
        try:
            capture.start(process)
            supplied_input.start(process)
            report["status"] = _await_worker(process, capture, deadline, cancel_event, supplied_input)
        finally:
            try:
                cleanup = _stop_and_reap(process, termination_grace_seconds, reap_timeout_seconds)
            finally:
                readers_finished = capture.finish(process, reap_timeout_seconds)
                supplied_input.finish(reap_timeout_seconds)
        report.update(returncode=process.returncode, worker_reaped=cleanup["reaped"],
                      cleanup_signals=cleanup["signals"], cleanup_errors=cleanup["errors"],
                      elapsed_seconds=time.monotonic() - started,
                      stdin_bytes_written=supplied_input.written, stdin_error=supplied_input.error)
        report.update(capture.result())
        if not cleanup["reaped"] or cleanup["errors"]:
            report["status"] = "cleanup_failed"
        elif not readers_finished or capture.failed.is_set():
            report["status"] = "capture_failed"
        elif capture.limit_hit.is_set():
            report["status"] = "output_limit_exceeded"
        elif supplied_input.failed.is_set() and report["status"] == "completed":
            report["status"] = "input_failed"
        report["success"] = (report["status"] == "completed" and process.returncode == 0
                             and cleanup["reaped"] and readers_finished)
        return report
