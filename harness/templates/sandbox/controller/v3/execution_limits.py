"""Context-local limits around frozen quality modules; no on-disk mutation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import functools
import math
from pathlib import Path
import threading
import time

CHECK_SECONDS = 105.0
_scope = ContextVar('deng_v3_execution_scope', default=None)
_install_lock = threading.Lock()
_installed = False


@dataclass
class ExecutionScope:
    deadline: float
    cancel_event: object
    workers: list = field(default_factory=list)
    clamps: list = field(default_factory=list)
    unknown_worker_failure: bool = False

    @property
    def quiescent(self):
        return not self.unknown_worker_failure and all(
            record.get('launched') is False or record.get('worker_reaped') is True
            for record in self.workers)


@contextmanager
def execution_scope(deadline, cancel_event):
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError('Finite absolute execution deadline required')
    prior = _scope.get()
    scope = ExecutionScope(min(deadline, prior.deadline) if prior else deadline, cancel_event)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
        if prior:
            prior.workers.extend(scope.workers)
            prior.clamps.extend(scope.clamps)
            prior.unknown_worker_failure |= scope.unknown_worker_failure


def isolated_wrapper(function, site):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        scope = _scope.get()
        if scope is not None:
            requested = kwargs.get('deadline')
            effective = scope.deadline if requested is None else min(requested, scope.deadline)
            kwargs['deadline'] = effective
            scope.clamps.append({'site': site, 'requested_deadline': requested,
                                 'effective_deadline': effective})
        return function(*args, **kwargs)
    return wrapped


def evaluation_wrapper(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if _scope.get() is not None:
            # Called by QualityCheckService only after quota reservation and
            # under the shared worker lock. Bound empty-directory traversal as
            # well as file bytes before frozen evaluate walks/copies the tree.
            from trial_timeline import bounded_snapshot_preflight
            workspace = args[0] if args else kwargs.get('workspace')
            if workspace is None:
                raise ValueError('Evaluator workspace is required')
            bounded_snapshot_preflight(Path(workspace)/'output')
            kwargs['seconds'] = min(kwargs.get('seconds', CHECK_SECONDS), CHECK_SECONDS)
        return function(*args, **kwargs)
    return wrapped


class _EitherEvent:
    def __init__(self, first, second): self.first, self.second = first, second
    def is_set(self): return self.first.is_set() or self.second.is_set()


def worker_wrapper(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        scope = _scope.get()
        if scope is None:
            return function(*args, **kwargs)
        requested = kwargs['absolute_deadline_monotonic']
        kwargs['absolute_deadline_monotonic'] = min(requested, scope.deadline)
        prior_event = kwargs.get('cancel_event')
        kwargs['cancel_event'] = (_EitherEvent(prior_event, scope.cancel_event)
                                 if prior_event is not None and prior_event is not scope.cancel_event
                                 else scope.cancel_event)
        scope.clamps.append({'site': 'worker_supervisor.run_worker',
            'requested_deadline': requested, 'effective_deadline': kwargs['absolute_deadline_monotonic']})
        try:
            result = function(*args, **kwargs)
        except BaseException:
            scope.unknown_worker_failure = True
            raise
        scope.workers.append({key: result.get(key) for key in
            ('launched', 'worker_reaped', 'status', 'pid', 'returncode', 'elapsed_seconds')})
        return result
    return wrapped


def install():
    """Install once; wrappers are inert outside execution_scope in this thread."""
    global _installed
    with _install_lock:
        if _installed:
            return
        import evaluate
        import rebuild
        import check_service
        import worker_supervisor
        evaluate.run_isolated = isolated_wrapper(evaluate.run_isolated, 'evaluate.run_isolated')
        rebuild.run_isolated = isolated_wrapper(rebuild.run_isolated, 'rebuild.run_isolated')
        check_service.evaluate = evaluation_wrapper(check_service.evaluate)
        # Frozen execution imports this symbol at call time. The frozen v2
        # candidate broker already holds its original, explicitly bounded one.
        worker_supervisor.run_worker = worker_wrapper(worker_supervisor.run_worker)
        _installed = True
