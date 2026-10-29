"""Trusted wall-clock orchestration; no model client and no CAD execution.

All candidate writers/checkers MUST enter operation() with the same RLock.
The snapshot callback copies output/ to an owner-only, new destination using
controller.snapshot_output and task-specific size/file preflight. Files copied
after a cutoff can represent an earlier state only after writers were reaped.
An operation crossing a cutoff is excluded in full using its saved baseline.
This is conservative temporal admission, not a claim of real-time OS stopping.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable


class TrialClosed(RuntimeError):
    pass


class TrialNotStarted(RuntimeError):
    pass


class QuiescenceError(RuntimeError):
    pass


def bounded_snapshot_preflight(output: str | Path, *, max_files: int = 256,
                               max_directories: int = 256, max_depth: int = 32,
                               max_file_bytes: int = 100_000_000,
                               max_total_bytes: int = 500_000_000) -> dict:
    """Bound snapshot traversal after writers have been quiesced by the caller.

    Unlike os.walk, scandir consumes entries incrementally and stops as soon as
    a count limit is exceeded, including an excessive number of empty dirs.
    The root is depth zero; directory count excludes the root. The authoritative
    copy still uses controller.snapshot_output's no-follow FD-relative traversal.
    """
    limits = (max_files, max_directories, max_depth, max_file_bytes, max_total_bytes)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in limits):
        raise ValueError("Snapshot limits must be nonnegative integers")
    output = Path(output)
    if not stat.S_ISDIR(output.lstat().st_mode):
        raise ValueError("Snapshot source must be an ordinary directory")
    files = directories = total_bytes = deepest = 0
    pending = [(output, 0)]
    while pending:
        parent, depth = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    directories += 1
                    if directories > max_directories or depth + 1 > max_depth:
                        raise ValueError("Snapshot exceeds directory count/depth limit")
                    deepest = max(deepest, depth + 1)
                    pending.append((Path(entry.path), depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    total_bytes += info.st_size
                    if files > max_files or info.st_size > max_file_bytes or total_bytes > max_total_bytes:
                        raise ValueError("Snapshot exceeds file count/byte limit")
                else:
                    raise ValueError("Snapshot contains a symlink or special file")
    return {"files": files, "directories": directories, "depth": deepest,
            "bytes": total_bytes}


@dataclass
class OperationLease:
    deadline: float
    kind: str
    started_at: float
    _owner: "TrialTimeline"
    completed_at: float | None = None
    quiescent: bool | None = None
    successful: bool = False

    def complete(self, result: dict | None = None, *, quiescent: bool | None = None):
        """Call immediately after the underlying operation returns/reaps.

        For run_worker pass its result. For a check pass quiescent=True only
        after independently confirming ALL subprocesses reaped/not launched.
        Never use a timeout or a submitted kill signal as proof of reaping.
        """
        if self.completed_at is not None:
            raise RuntimeError("Operation completion may be reported only once")
        if result is not None:
            if not isinstance(result, dict) or quiescent is not None:
                raise ValueError("Supply worker result or explicit quiescence")
            quiescent = (result.get("launched") is False or
                         result.get("worker_reaped") is True)
            self.successful = result.get("success") is True
        else:
            if not isinstance(quiescent, bool):
                raise ValueError("An explicit subprocess quiescence proof is required")
            self.successful = quiescent
        self.quiescent = quiescent
        self.completed_at = self._owner.clock()


class TrialTimeline:
    """One absolute budget including thinking, tools, checks, and copy time.

    Public API: start(), operation(kind), poll(), finish(reason), abort(reason),
    report(). Supply broker.lock as worker_lock and broker.cancel_event. start()
    arms only the timing thread; it does not launch a model. stop_callback must
    be short/nonblocking, close model request admission, and signal CLI shutdown.
    Full broker/transport/process cleanup remains the launcher's responsibility.
    """
    def __init__(self, *, started_at: float, worker_lock, cancel_event: threading.Event,
                 snapshot_root: str | Path, snapshot: Callable[[Path], dict],
                 checkpoint_seconds: float = 35 * 60,
                 total_seconds: float = 60 * 60,
                 stop_callback: Callable[[str], None] | None = None,
                 quiesce_timeout_seconds: float = 10,
                 poll_seconds: float = .02,
                 clock: Callable[[], float] = time.monotonic):
        for value in (started_at, checkpoint_seconds, total_seconds,
                      quiesce_timeout_seconds, poll_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Timeline values must be finite numbers")
        if not 0 < checkpoint_seconds < total_seconds or quiesce_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("Require 0 < checkpoint < total and positive controller bounds")
        self.started_at = started_at
        self.checkpoint_at = started_at + checkpoint_seconds
        self.deadline = started_at + total_seconds
        self.worker_lock = worker_lock
        self.cancel_event = cancel_event
        self.snapshot_root = Path(snapshot_root).absolute()
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self.stop_callback = stop_callback
        self.quiesce_timeout_seconds = quiesce_timeout_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._state_lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._active = None
        self._last_safe = None
        self._live_matches_safe = False
        self._snapshot_sequence = 0
        self._checkpoint = None
        self._final = None
        self._terminal_reason = None
        self._terminal_at = None
        self._stop_sent = False
        self._errors = []
        self._operations = []

    def _error(self, where, error):
        with self._state_lock:
            self._errors.append({"where": where, "type": type(error).__name__, "message": str(error)})

    def _terminal(self, reason, at=None):
        with self._state_lock:
            if self._terminal_reason is None:
                self._terminal_reason = reason
                self._terminal_at = self.clock() if at is None else at
            self.cancel_event.set()
            self._wake.set()

    def _send_stop(self):
        with self._state_lock:
            if self._terminal_reason is None or self._stop_sent:
                return
            self._stop_sent = True
            reason = self._terminal_reason
        if self.stop_callback:
            try:
                self.stop_callback(reason)
            except BaseException as error:
                self._error("stop_callback", error)

    def _capture(self, state_at):
        """Caller owns writer lock and has proven no live writers."""
        self._snapshot_sequence += 1
        target = self.snapshot_root / f"stable-{self._snapshot_sequence:06d}"
        try:
            manifest = self.snapshot(target)
            if not isinstance(manifest, dict):
                raise TypeError("Snapshot callback must return the completed manifest")
        except BaseException as error:
            self._error("snapshot", error)
            self._terminal("snapshot_failed")
            raise
        record = {"path": str(target), "state_at_monotonic": state_at,
                  "copy_completed_at_monotonic": self.clock(),
                  "file_count": manifest.get("file_count")}
        self._last_safe = record
        self._live_matches_safe = True
        return record

    def _record(self, label, cutoff, reason):
        # No new filesystem access here. The immutable snapshot may predate
        # an excluded crossing operation, including one that failed to reap.
        safe = self._last_safe
        valid = safe is not None and safe["state_at_monotonic"] <= cutoff
        return {"label": label, "cutoff_monotonic": cutoff,
                "recorded_at_monotonic": self.clock(), "reason": reason,
                "snapshot": dict(safe) if valid else None,
                "valid": valid, "admission": "last_quiescent_state_at_or_before_cutoff"}

    def _due_locked(self):
        """Caller owns writer lock; never capture mutable live output here."""
        now = self.clock()
        if self._checkpoint is None and now >= self.checkpoint_at:
            self._checkpoint = self._record("35_minute", self.checkpoint_at, "checkpoint")
        if now >= self.deadline:
            self._terminal("deadline", self.deadline)
        if self._terminal_reason is not None:
            if self._final is None:
                cutoff = min(self.deadline, self._terminal_at)
                self._final = self._record("final", cutoff, self._terminal_reason)
                if self._checkpoint is None:
                    # Once sealed, an early result is unchanged at the later
                    # 35-minute mark; do not wait or pretend it used 35 minutes.
                    self._checkpoint = self._record("35_minute", self.checkpoint_at,
                                                    "early_finish_sealed_result")
            return
        # Clearing is permitted only after lock acquisition proved the previous
        # writer/checker has exited and the 35-minute checkpoint is recorded.
        self.cancel_event.clear()

    def _next_boundary(self):
        return self.deadline if self._checkpoint is not None else self.checkpoint_at

    @contextmanager
    def operation(self, kind: str):
        if not isinstance(kind, str) or not kind:
            raise ValueError("Operation kind is required")
        try:
            with self.worker_lock:
                if self._active is not None:
                    raise RuntimeError("Nested timeline operations are not supported")
                if self.clock() < self.started_at:
                    raise TrialNotStarted("Trial clock has not started")
                if self._last_safe is None:
                    self._capture(self.started_at)
                self._due_locked()
                if self._terminal_reason is not None:
                    raise TrialClosed(self._terminal_reason)
                if not self._live_matches_safe:
                    self._capture(self.clock())
                # Baseline capture consumes real budget. Re-check before dispatch.
                self._due_locked()
                if self._terminal_reason is not None:
                    raise TrialClosed(self._terminal_reason)
                lease = OperationLease(self._next_boundary(), kind, self.clock(), self)
                self._active = lease
                self._live_matches_safe = False
                try:
                    yield lease
                finally:
                    self._active = None
                    cutoff = lease.deadline
                    if self._terminal_at is not None:
                        cutoff = min(cutoff, self._terminal_at)
                    proven = lease.quiescent is True and lease.completed_at is not None
                    # Snapshot the actual stable filesystem, even after exit != 0.
                    # The evaluator judges those files; exit status is not a reason
                    # to silently substitute an older in-budget result.
                    admitted = proven and lease.completed_at <= cutoff
                    self._operations.append({"kind": kind, "started_at_monotonic": lease.started_at,
                        "deadline_monotonic": lease.deadline, "completed_at_monotonic": lease.completed_at,
                        "quiescent": lease.quiescent, "worker_success": lease.successful,
                        "committed": admitted})
                    if not proven:
                        self._error("operation", QuiescenceError(f"{kind}: subprocess quiescence unconfirmed"))
                        self._terminal("quiescence_failed")
                    elif admitted:
                        self._capture(lease.completed_at)
                    self._due_locked()
                    self._wake.set()
        finally:
            self._send_stop()

    def poll(self):
        """Process due boundaries; useful for deterministic/accelerated tests."""
        now = self.clock()
        if now >= self.deadline:
            self._terminal("deadline", self.deadline)
        due = self._terminal_reason is not None or (self._checkpoint is None and now >= self.checkpoint_at)
        if not due:
            return
        self.cancel_event.set()  # Do not first wait behind the worker lock.
        self._send_stop()        # Stop model/new-call admission immediately at 60.
        acquired = self.worker_lock.acquire(timeout=self.quiesce_timeout_seconds)
        if not acquired:
            self._error("quiesce", QuiescenceError("Writer lock did not quiesce within shutdown bound"))
            self._terminal("quiescence_failed")
            # Existing external baseline remains safe; no live copy is attempted.
            with self._state_lock:
                if self._checkpoint is None and now >= self.checkpoint_at:
                    self._checkpoint = self._record("35_minute", self.checkpoint_at, "quiescence_failed")
                if self._final is None:
                    self._final = self._record("final", min(self.deadline, self._terminal_at), "quiescence_failed")
            self._send_stop()
            return
        try:
            if self._last_safe is None and self._active is None and self._snapshot_sequence == 0:
                # No candidate operation has ever run; current output is its
                # initial state, regardless of how late this first poll occurs.
                self._capture(self.started_at)
            self._due_locked()
        finally:
            self.worker_lock.release()
        self._send_stop()

    def _monitor(self):
        try:
            while self._final is None:
                self.poll()
                if self._final is not None:
                    break
                remaining = max(0, self._next_boundary() - self.clock())
                self._wake.wait(min(self.poll_seconds, remaining))
                self._wake.clear()
        except BaseException as error:
            self._error("monitor", error)
            self._terminal("controller_failed")
            self._send_stop()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Timeline is already started")
        self._thread = threading.Thread(target=self._monitor, name="deng-trial-timeline", daemon=True)
        self._thread.start()
        return self

    def finish(self, reason="agent_finished"):
        self._terminal(reason)
        self.poll()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.quiesce_timeout_seconds + 1)
            if self._thread.is_alive():
                self._error("monitor_join", TimeoutError("Timeline monitor did not stop"))
        return self.report()

    def abort(self, reason="aborted"):
        return self.finish(reason)

    def report(self):
        with self._state_lock:
            return {"schema_version": 1, "started_at_monotonic": self.started_at,
                "checkpoint_at_monotonic": self.checkpoint_at, "deadline_monotonic": self.deadline,
                "terminal_reason": self._terminal_reason, "terminal_at_monotonic": self._terminal_at,
                "checkpoint": self._checkpoint, "final": self._final,
                "snapshot_count": self._snapshot_sequence, "operations": list(self._operations),
                "errors": list(self._errors), "cancelled": self.cancel_event.is_set(),
                "stop_callback_sent": self._stop_sent,
                "scope": "Monotonic wall time; conservative stable snapshots exclude crossing operations. OS termination is bounded cleanup, not real-time enforcement."}
