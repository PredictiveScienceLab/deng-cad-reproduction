"""Operator-owned four-call diagnostic service, ready for a serialized broker.

The full rubric and final scoring remain operator-only. This adapter has no
model client and does not change the two-tool candidate interface by itself.
"""
from pathlib import Path
import sys
import threading
import time
from evaluate import evaluate,PROJECT
sys.path.insert(0,str(PROJECT/'sandbox/controller'))
from controller import CheckQuota

class QualityCheckService:
    def __init__(self,workspace,reports,*,worker_lock,deadline):
        self.workspace=Path(workspace).resolve(strict=True)
        self.reports=Path(reports).absolute()
        for writable in (self.workspace,PROJECT/'sandbox/candidate/work',PROJECT/'sandbox/candidate/tmp'):
            if self.reports==writable or writable in self.reports.parents:raise ValueError('Reports must be outside every candidate writable directory')
        self.reports.mkdir(parents=True,exist_ok=True)
        self.quota=CheckQuota(self.reports/'quota.json',self.workspace)
        self.worker_lock=worker_lock
        self.deadline=deadline
    def check(self):
        # The future broker must pass the same lock that serializes run_python.
        # Acquiring it makes this snapshot occur only after the worker is reaped.
        with self.worker_lock:
            check_id=self.quota.reserve()
            try:
                remaining=self.deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Global deadline already expired')
                result=evaluate(self.workspace,self.reports/f'check-{check_id:02d}',seconds=min(remaining,180),writers_stopped=True)
                self.quota._finish(check_id,'completed')
            except BaseException as exc:
                self.quota._finish(check_id,'error',type(exc).__name__);raise
            integrity=result['analysis'].get('integrity',{})
            # Do not expose grading target values, criterion IDs, rubric/source
            # paths, protected reports, or synthetic qualification geometry.
            return {'check_number':check_id,'checks_remaining':4-check_id,
                'integrity_status':integrity.get('status','inconclusive'),
                'integrity_checks':[{'name':r['name'],'status':r['status']} for r in integrity.get('checks',[])],
                'rebuild_status':next(x['status'] for x in result['gates'] if x['id']=='GATE-REBUILD'),
                'scope':'Geometry integrity and fresh rebuild diagnostics. Final drawing fidelity and design-intent review remain independent.'}
