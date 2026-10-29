"""Three-tool bridge around the frozen v2 CAD broker and quality service."""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
from pathlib import Path
import sys
import threading
import time

PROJECT = Path('@PROJECT@')
V2 = PROJECT/'sandbox/controller/v2'
EVALUATOR = PROJECT/'quality/evaluator'
sys.path.insert(0, str(V2))
from broker import Broker as V2Broker, TOOLS as V2_TOOLS, MAX_LINE, ROOT, WORK, INPUT, PYTHON
sys.path.insert(0, str(EVALUATOR))
from check_service import QualityCheckService
from controller import QuotaExceeded
from execution_limits import CHECK_SECONDS, execution_scope, install

CHECK_DESCRIPTION = ('Check the saved output/ STEP artifacts and rebuild from saved source. '
    'At most four calls per trial, including failed checks. Save files before calling. '
    'The check serializes with Python/image workers and has a 105-second execution budget, '
    'also bounded by the current checkpoint and trial deadline. Feedback covers artifact '
    'integrity and reproducibility; final drawing fidelity and design-intent review are independent.')
TOOLS = copy.deepcopy(V2_TOOLS) + [{'name': 'check_geometry', 'description': CHECK_DESCRIPTION,
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]
_tool_name = ContextVar('deng_v3_tool_name', default='run_python')
_VALID_STATUS = {'pass', 'fail', 'inconclusive', 'requires_review'}
_FILES = ('step-1.step', 'step-2.step', 'step-3.step', 'main-part.step', 'stand.step', 'assembly.step')
_CHECK_NAMES = {'input_directory', 'posed_components_no_overlap', 'assembly_parts_no_overlap',
                'assembly_matches_posed_components', 'assembly_consistency'} | {
    name+':'+suffix for name in _FILES for suffix in
    ('file_contract', 'stable_input', 'import_geometry', 'geometry', 'millimeter_units')}


def _public_feedback(value):
    number = value.get('check_number')
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 4:
        raise ValueError('Invalid checker feedback')
    if value.get('checks_remaining') != 4-number:
        raise ValueError('Invalid checker quota feedback')
    integrity, rebuild = value.get('integrity_status'), value.get('rebuild_status')
    if integrity not in _VALID_STATUS or rebuild not in _VALID_STATUS:
        raise ValueError('Invalid checker status')
    checks = value.get('integrity_checks')
    if not isinstance(checks, list) or len(checks) > 64:
        raise ValueError('Invalid checker rows')
    return {'check_number': number, 'checks_remaining': 4-number,
        'integrity_status': integrity, 'rebuild_status': rebuild,
        'integrity_checks': [{'name': row['name'], 'status': row['status']} for row in checks
            if isinstance(row, dict) and row.get('name') in _CHECK_NAMES and row.get('status') in _VALID_STATUS],
        'scope': 'Artifact integrity and fresh rebuild diagnostics. Final drawing fidelity and design-intent review remain independent.'}


class _Lease:
    def __init__(self, deadline): self.deadline = deadline
    def complete(self, result=None, *, quiescent=None):
        if result is not None:
            quiescent = result.get('launched') is False or result.get('worker_reaped') is True
        if quiescent is not True:
            raise RuntimeError('Worker quiescence was not established')


class BrokerV3(V2Broker):
    def __init__(self, deadline, audit_path=None, *, check_reports, _service_factory=None):
        super().__init__(deadline, audit_path)
        self.lock = threading.RLock()
        self.trial_deadline = deadline
        self.check_calls = 0
        self.timeline = None
        self.last_check_execution = None
        install()
        factory = _service_factory or QualityCheckService
        self.checker = factory(WORK, check_reports, worker_lock=self.lock, deadline=deadline)

    def set_timeline(self, timeline):
        if self.sequence or self.check_calls:
            raise RuntimeError('Attach timeline before tool execution')
        if timeline.worker_lock is not self.lock:
            raise ValueError('Timeline must share the broker worker lock')
        if timeline.cancel_event is not self.cancel_event:
            raise ValueError('Timeline must share the broker cancellation event')
        if timeline.deadline != self.trial_deadline:
            raise ValueError('Timeline and broker must share the absolute trial deadline')
        self.timeline = timeline

    @contextmanager
    def _operation(self, name):
        with self.lock:
            if self.cancel_event.is_set() or time.monotonic() >= self.trial_deadline:
                raise TimeoutError('Trial execution is unavailable')
            if self.timeline is None:
                yield _Lease(self.trial_deadline)
            else:
                with self.timeline.operation(name) as lease:
                    yield lease

    def execute(self, source, timeout_seconds=60, output_limit_bytes=2*1024*1024):
        with self._operation(_tool_name.get()) as lease:
            previous = self.deadline
            self.deadline = min(previous, lease.deadline)
            try:
                result = super().execute(source, timeout_seconds, output_limit_bytes)
                lease.complete(result)
                return result
            finally:
                self.deadline = previous

    def _remaining(self):
        try:
            return max(0, 4-len(self.checker.quota.inspect()['checks']))
        except Exception:
            return None

    def _check(self):
        started = time.monotonic()
        outcome = 'checker_error'
        scope = None
        try:
            with self._operation('check_geometry') as lease:
                self.check_calls += 1
                deadline = min(self.trial_deadline, lease.deadline, time.monotonic()+CHECK_SECONDS)
                with execution_scope(deadline, self.cancel_event) as scope:
                    try:
                        result = self.checker.check()
                    finally:
                        lease.complete(quiescent=scope.quiescent)
                if self.cancel_event.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError('Check execution deadline reached')
                public = _public_feedback(result)
                outcome = 'completed'
                return {'content': [{'type': 'text', 'text': json.dumps(public)}], 'isError': False}
        except QuotaExceeded:
            outcome, message = 'quota_exhausted', 'The four geometry checks have been used.'
        except TimeoutError:
            outcome, message = 'deadline_reached', 'The current execution window has ended; the check did not produce usable feedback.'
        except Exception:
            # Never forward evaluator exception text, paths, source, or traceback.
            outcome, message = 'checker_error', 'The geometry check could not complete. Any reserved check remains consumed.'
        finally:
            self.last_check_execution = {'outcome': outcome, 'elapsed_seconds': time.monotonic()-started,
                'workers': scope.workers if scope else [], 'clamps': scope.clamps if scope else [],
                'quiescent': scope.quiescent if scope else True}
            if self.audit_path:
                with self.audit_path.open('a') as stream:
                    stream.write(json.dumps({'event': 'check_geometry', **self.last_check_execution})+'\n')
        error = {'status': 'error', 'code': outcome, 'message': message, 'checks_remaining': self._remaining()}
        return {'content': [{'type': 'text', 'text': json.dumps(error)}], 'isError': True}

    def call(self, name, arguments):
        if name == 'check_geometry':
            if not isinstance(arguments, dict) or arguments:
                raise ValueError('check_geometry takes an empty argument object')
            return self._check()
        token = _tool_name.set(name)
        try:
            return super().call(name, arguments)
        finally:
            _tool_name.reset(token)


def serve_stream(broker, incoming, outgoing):
    from tool_gate import strict_loads
    while True:
        line = incoming.readline(MAX_LINE+1)
        if not line: return
        if len(line) > MAX_LINE: raise ValueError('MCP message exceeds bound')
        request = strict_loads(line)
        if not isinstance(request, dict): raise ValueError('MCP object required')
        identifier = request.get('id')
        if identifier is None: continue
        try:
            params = request.get('params') or {}
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                    'capabilities': {'tools': {}}, 'serverInfo': {'name': 'deng-cad-sandbox-broker', 'version': '3'}}
            elif method == 'tools/list': result = {'tools': TOOLS}
            elif method == 'tools/call': result = broker.call(params.get('name'), params.get('arguments', {}))
            elif method == 'ping': result = {}
            else: raise ValueError('Unsupported method')
            response = {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except Exception:
            response = {'jsonrpc': '2.0', 'id': identifier,
                        'error': {'code': -32602, 'message': 'Broker request rejected.'}}
        outgoing.write((json.dumps(response)+'\n').encode())
        outgoing.flush()
