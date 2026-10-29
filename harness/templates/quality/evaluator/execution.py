"""Trusted, no-fork execution for rebuilds and independent geometry inspection."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys
import time

PROJECT=Path('@PROJECT@')
SANDBOX=PROJECT/'sandbox'
RUNTIME=Path('@RUNTIME@')
PYTHON=RUNTIME/'venv/bin/python'

def _ordinary_directory(path):
    path=Path(path).absolute()
    if path.is_symlink() or path.resolve()!=path or not path.is_dir():
        raise ValueError('Execution directories must be existing canonical directories')
    return path

def run_isolated(source, *, work, scratch, control, read_roots=(), seconds=60, deadline=None):
    sys.path.insert(0,str(SANDBOX/'controller/v2'))
    from worker_supervisor import run_worker
    work,scratch,control=map(_ordinary_directory,(work,scratch,control))
    if any(a==b or a in b.parents or b in a.parents for a,b in ((work,control),(scratch,control),(work,scratch))):
        raise ValueError('Work, scratch and trusted control directories must be disjoint')
    if not isinstance(source,str) or len(source.encode())>262144:raise ValueError('Source too large')
    if not 0<seconds<=60:raise ValueError('Worker maximum is 60 seconds')
    fixed_deadline=deadline if deadline is not None else time.monotonic()+seconds
    if not math.isfinite(fixed_deadline):raise ValueError('Finite absolute deadline required')
    policy=(SANDBOX/'controller/v2/worker.sb').read_text()
    policy=policy.replace(json.dumps(str(SANDBOX/'candidate/work')),json.dumps(str(work))).replace(json.dumps(str(SANDBOX/'candidate/tmp')),json.dumps(str(scratch)))
    for root in read_roots:
        root=Path(root).resolve(strict=True)
        if any(root==p or root in p.parents or p in root.parents for p in (work,scratch,control)):
            raise ValueError('Additional read roots must not overlap the execution directories')
        policy+='\n(allow file-read* file-map-executable (subpath '+json.dumps(str(root))+'))\n'
    policy_path=control/'worker.sb'
    if policy_path.is_symlink():raise ValueError('Policy must not be a symlink')
    policy_path.write_text(policy)
    environment=json.loads((SANDBOX/'control/candidate-environment.json').read_text())
    environment.update(TMPDIR=str(scratch),MPLCONFIGDIR=str(scratch/'matplotlib'),XDG_CACHE_HOME=str(scratch/'cache'))
    bootstrap='import sys,os;data=sys.stdin.buffer.read(262145);assert len(data)<=262144;exec(compile(data,"<quality-worker>","exec"),{"__name__":"__main__"})'
    result=run_worker(['/usr/bin/sandbox-exec','-f',str(policy_path),str(PYTHON),'-I','-c',bootstrap],
        cwd=work,env=environment,timeout_seconds=seconds,absolute_deadline_monotonic=fixed_deadline,
        stdin_bytes=source.encode(),output_limit_bytes=2*1024*1024)
    result.update(policy_sha256=hashlib.sha256(policy.encode()).hexdigest(),source_sha256=hashlib.sha256(source.encode()).hexdigest())
    (control/'worker-result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
