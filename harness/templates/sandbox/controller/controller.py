"""External-controller primitives for sandbox qualification, not a CAD evaluator.

The operator must keep this module, its ledger and snapshots outside the
candidate's writable sandbox. Filesystem modes do not isolate a same-UID
candidate with host access, and nothing here is immutable to the operator/root.
Quiesce candidate writers before snapshotting. Process groups cannot contain
children that escape with setsid()/setpgid(); use a container/cgroup or another
external supervisor when complete descendant containment is required.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable, TypeVar


MAX_CHECKS = 4
MANIFEST_NAME = ".snapshot-manifest.json"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
T = TypeVar("T")


class QuotaExceeded(RuntimeError):
    """All four reservations have been consumed, including failed/crashed checks."""


class UnsafeSnapshot(ValueError):
    """Input is not an ordinary, stable file/directory tree in the workspace."""


def _absolute_without_traversal(value: str | Path) -> Path:
    raw = os.fspath(value)
    path = Path(raw)
    if not path.is_absolute() or ".." in raw.split(os.sep):
        raise ValueError("Use an absolute path without '..' traversal")
    return path


def _outside_workspace(path: Path, workspace: Path) -> None:
    resolved = path.resolve()
    if resolved == workspace or workspace in resolved.parents:
        raise ValueError("Operator state must be outside the candidate workspace")


def _regular_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Ledger must be an ordinary file")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Malformed quota ledger")
    return value


class CheckQuota:
    """Reserve a check durably before invoking an operator-owned callback.

    The flock protects distinct processes; a shared RLock also protects threads
    and multiple CheckQuota objects in the same process. A crash after reserve
    consumes the reservation, preventing a fifth invocation after restart.
    This is accident/concurrency protection, not a defense against an operator
    that can edit the ledger or a candidate allowed to write its parent folder.
    """

    def __init__(self, ledger: str | Path, candidate_workspace: str | Path):
        self.workspace = _absolute_without_traversal(candidate_workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Candidate workspace must be a directory")
        self.ledger = _absolute_without_traversal(ledger)
        _outside_workspace(self.ledger, self.workspace)
        self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = self.ledger.parent.resolve() / self.ledger.name
        self.lock_path = self.ledger.with_name(self.ledger.name + ".lock")
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(str(self.lock_path), threading.RLock())
        with self._locked():
            if self.ledger.exists() or self.ledger.is_symlink():
                self._read()
            else:
                self._write({"schema_version": 1, "limit": MAX_CHECKS,
                             "candidate_workspace": str(self.workspace), "checks": []})

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Lock must be an ordinary file")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self) -> dict:
        value = _regular_json(self.ledger)
        checks = value.get("checks")
        if (value.get("schema_version") != 1 or value.get("limit") != MAX_CHECKS
                or value.get("candidate_workspace") != str(self.workspace)
                or not isinstance(checks, list) or len(checks) > MAX_CHECKS
                or any(not isinstance(item, dict) or item.get("id") != index
                       or item.get("status") not in {"reserved", "completed", "error"}
                       for index, item in enumerate(checks, 1))):
            raise ValueError("Malformed or mismatched quota ledger; refusing to reset quota")
        return value

    def _write(self, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=self.ledger.name + ".", dir=self.ledger.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.ledger)
            directory_fd = os.open(self.ledger.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def reserve(self) -> int:
        with self._locked():
            value = self._read()
            if len(value["checks"]) >= MAX_CHECKS:
                raise QuotaExceeded("Four checks have already been reserved; fifth check rejected")
            check_id = len(value["checks"]) + 1
            value["checks"].append({"id": check_id, "status": "reserved",
                                    "reserved_unix_seconds": time.time()})
            self._write(value)
            return check_id

    def _finish(self, check_id: int, status: str, error_type: str | None = None) -> None:
        with self._locked():
            value = self._read()
            item = value["checks"][check_id - 1]
            if item["status"] != "reserved":
                raise ValueError("Check reservation has already finished")
            item.update(status=status, finished_unix_seconds=time.time())
            if error_type:
                item["error_type"] = error_type
            self._write(value)

    def invoke(self, callback: Callable[..., T], *args, **kwargs) -> T:
        check_id = self.reserve()
        try:
            result = callback(*args, **kwargs)
        except BaseException as error:
            self._finish(check_id, "error", type(error).__name__)
            raise
        self._finish(check_id, "completed")
        return result

    def inspect(self) -> dict:
        with self._locked():
            return self._read()


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _open_source_directory(workspace: Path, source: Path) -> int:
    """Anchor traversal at a directory FD and refuse every source-path symlink."""
    try:
        relative = source.relative_to(workspace)
    except ValueError as error:
        raise UnsafeSnapshot("Candidate output must be inside its workspace") from error
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def snapshot_output(candidate_output: str | Path, destination: str | Path, *,
                    candidate_workspace: str | Path) -> dict:
    """Copy only ordinary files/directories and record hashes of copied bytes.

    Source access is directory-FD-relative with O_NOFOLLOW. Changes during a
    file copy or directory traversal are rejected. This is not an atomic view
    of concurrently mutating files: the operator must first quiesce writers.
    A failed copy leaves a clearly incomplete destination without a manifest;
    do not admit it as a snapshot. Successful files/dirs have 0400/0500 modes.
    """
    workspace = _absolute_without_traversal(candidate_workspace)
    source = _absolute_without_traversal(candidate_output)
    target = _absolute_without_traversal(destination)
    # Resolve trusted ancestors, not candidate-controlled descendants.
    if workspace.is_symlink():
        raise UnsafeSnapshot("Candidate workspace itself must not be a symlink")
    workspace_resolved = workspace.resolve(strict=True)
    try:
        suffix = source.relative_to(workspace)
    except ValueError as error:
        raise UnsafeSnapshot("Candidate output must be inside its workspace") from error
    source = workspace_resolved / suffix
    _outside_workspace(target, workspace_resolved)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Snapshot destination must be new")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target.parent.resolve() / target.name
    source_fd = _open_source_directory(workspace_resolved, source)
    files: dict[str, dict] = {}
    directories: list[Path] = []

    def copy_directory(source_dir_fd: int, output: Path, prefix: str) -> None:
        before = os.fstat(source_dir_fd)
        names = sorted(os.listdir(source_dir_fd))
        output.mkdir(mode=0o700)
        directories.append(output)
        for name in names:
            if name in {".", ".."} or os.sep in name or "\x00" in name:
                raise UnsafeSnapshot("Invalid directory entry")
            if not prefix and name == MANIFEST_NAME:
                raise UnsafeSnapshot("Candidate uses reserved snapshot manifest name")
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=source_dir_fd)
                try:
                    if _fingerprint(os.fstat(child_fd)) != _fingerprint(info):
                        raise UnsafeSnapshot(f"Source changed before copy: {relative}")
                    copy_directory(child_fd, output / name, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                child_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=source_dir_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not stat.S_ISREG(opened.st_mode) or _fingerprint(opened) != _fingerprint(info):
                        raise UnsafeSnapshot(f"Source changed before copy: {relative}")
                    digest, size = hashlib.sha256(), 0
                    with (output / name).open("xb") as result:
                        while chunk := os.read(child_fd, 1024 * 1024):
                            result.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        result.flush()
                        os.fsync(result.fileno())
                    if _fingerprint(os.fstat(child_fd)) != _fingerprint(opened) or size != opened.st_size:
                        raise UnsafeSnapshot(f"Source changed during copy: {relative}")
                    os.chmod(output / name, 0o400)
                    files[relative] = {"sha256": digest.hexdigest(), "bytes": size}
                finally:
                    os.close(child_fd)
            else:
                raise UnsafeSnapshot(f"Refusing symlink or special file: {relative}")
        if sorted(os.listdir(source_dir_fd)) != names or _fingerprint(os.fstat(source_dir_fd)) != _fingerprint(before):
            raise UnsafeSnapshot(f"Source directory changed during copy: {prefix or '.'}")

    try:
        copy_directory(source_fd, target, "")
        manifest = {"schema_version": 1, "source": str(source),
                    "files": files, "file_count": len(files),
                    "scope": "Hashes cover copied file bytes; manifest is not a root-immutable attestation.",
                    "isolation_requirement": "Keep snapshot outside candidate writable mounts; same-UID host access is not isolated by file modes.",
                    "writer_requirement": "Candidate writers must be stopped before snapshot; concurrent changes are screened, not atomically excluded."}
        with (target / MANIFEST_NAME).open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(target / MANIFEST_NAME, 0o400)
        for directory in reversed(directories):
            os.chmod(directory, 0o500)
        return manifest
    finally:
        os.close(source_fd)


def _cleanup_error(operation: str, error: OSError) -> dict:
    return {"operation": operation, "error_type": type(error).__name__,
            "errno": error.errno, "message": str(error)}


def _group_state(pgid: int, errors: list[dict]) -> str:
    try:
        os.killpg(pgid, 0)
        return "present"
    except ProcessLookupError:
        return "absent"
    except OSError as error:
        # EPERM is not evidence of absence. Some macOS sandbox policies also
        # return EPERM during the transition from a dead leader to no group.
        errors.append(_cleanup_error("probe_process_group", error))
        return "unknown"


def _terminate_group(process: subprocess.Popen, grace_seconds: float) -> dict:
    record = {"signals": [], "errors": [], "status": "signals_submitted"}

    def send(number: int, name: str) -> None:
        try:
            os.killpg(process.pid, number)
            record["signals"].append(name)
        except ProcessLookupError:
            pass
        except OSError as error:
            record["errors"].append(_cleanup_error("send_group_" + name, error))

    send(signal.SIGTERM, "SIGTERM")
    deadline = time.monotonic() + grace_seconds
    state = "unknown"
    while time.monotonic() < deadline:
        process.poll()  # Reap a dead leader before probing its former group.
        state = _group_state(process.pid, record["errors"])
        if state != "present":
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    # Always address the group, even if its original leader already exited.
    send(signal.SIGKILL, "SIGKILL")
    record["last_probe_state"] = state
    if record["errors"]:
        record["status"] = "inconclusive"
    elif state == "absent":
        record["status"] = "group_absent_at_probe"
    return record


def _reap_leader(process: subprocess.Popen, errors: list[dict]) -> bool:
    """Always attempt direct-leader cleanup even when group operations fail."""
    try:
        process.wait(timeout=1)
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(_cleanup_error("kill_leader_fallback", error))
        try:
            process.wait(timeout=1)
            return True
        except subprocess.TimeoutExpired:
            errors.append({"operation": "reap_leader", "error_type": "TimeoutExpired",
                           "message": "Leader still running after group cleanup and direct kill attempt"})
            return False


def run_bounded(command: list[str], *, timeout_seconds: float,
                termination_grace_seconds: float = 0.2, cwd: str | Path | None = None,
                env: dict[str, str] | None = None, output_limit_bytes: int = 1_048_576) -> dict:
    """Run without a shell; terminate remaining members of its process group.

    Output uses temporary files, so an escaped child holding stdout open cannot
    hang collection. The timeout bounds the leader, plus configured cleanup and
    at most two seconds of leader reaping. Native process creation and filesystem
    I/O are outside a real-time guarantee. No setsid escape containment is claimed.
    """
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError("Command must be a nonempty argv list")
    if (not math.isfinite(timeout_seconds) or timeout_seconds <= 0
            or not math.isfinite(termination_grace_seconds) or termination_grace_seconds < 0
            or output_limit_bytes < 1):
        raise ValueError("Invalid time or output limits")
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        cleanup = {"signals": [], "errors": [], "status": "inconclusive"}
        leader_reaped = False
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            try:
                cleanup = _terminate_group(process, termination_grace_seconds)
            finally:
                # Reaping is independent of group probes/signals: an EPERM or
                # another cleanup exception must never skip this step.
                leader_reaped = _reap_leader(process, cleanup["errors"])
            if cleanup["errors"] or not leader_reaped:
                cleanup["status"] = "inconclusive"
        streams = {}
        for label, stream in (("stdout", stdout), ("stderr", stderr)):
            stream.seek(0)
            data = stream.read(output_limit_bytes + 1)
            streams[label] = data[:output_limit_bytes].decode("utf-8", errors="replace")
            streams[label + "_truncated"] = len(data) > output_limit_bytes
    return {"returncode": process.returncode, "timed_out": timed_out,
            "elapsed_seconds": time.monotonic() - started,
            "cleanup_signals": cleanup["signals"], "cleanup_errors": cleanup["errors"],
            "group_cleanup_status": cleanup["status"], "leader_reaped": leader_reaped, **streams,
            "containment": "process_group_only",
            "limitations": ["setsid()/setpgid() descendants can escape and are not tracked or killed by this primitive.",
                            "SIGKILL submission is recorded; it does not establish that every descendant has been reaped.",
                            "Temporary output files avoid inherited-pipe hangs; output generation is not disk-quota limited."]}
