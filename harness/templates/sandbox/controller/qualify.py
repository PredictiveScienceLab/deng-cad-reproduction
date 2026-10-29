#!/usr/bin/env python3
"""Deterministic isolation probes. Never invokes a model or attempts the Deng CAD task."""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
RUNTIME = Path('@RUNTIME@')
CODEX = str(RUNTIME / 'cli/codex')
PYTHON = RUNTIME / 'venv/bin/python'
WORK = ROOT / 'candidate/work'
SCRATCH = ROOT / 'candidate/tmp'
INPUT = ROOT / 'candidate/input'

def literal(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + literal(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(literal(v) for v in value) + ']'
    return str(value)

def config_args(config):
    result = []
    for key, value in config.items():
        result.extend(['-c', key + '=' + literal(value)])
    return result

class Qualification:
    def __init__(self):
        self.config = tomllib.loads((ROOT / 'profile.toml').read_text())
        self.env = json.loads((ROOT / 'control/candidate-environment.json').read_text())
        self.home = ROOT / 'control/probe-codex-home'
        self.home.mkdir(exist_ok=True)
        self.host_env = dict(self.env, CODEX_HOME=str(self.home))
        # CODEX_HOME is for the trusted sandbox launcher. Remove it in the candidate.
        self.rows = []
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.report = ROOT / 'qualification' / stamp
        self.report.mkdir()

    def run(self, argv, timeout=30):
        command = [CODEX, 'sandbox', '-P', 'deng', '-C', str(WORK), *config_args(self.config), '--',
                   '/usr/bin/env', '-i', *[f'{k}={v}' for k, v in self.env.items()], *map(str, argv)]
        start = time.monotonic()
        p = subprocess.Popen(command, cwd=WORK, env=self.host_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            stdout, stderr = p.communicate(timeout=5)
            return {'returncode': p.returncode, 'stdout': stdout, 'stderr': stderr, 'timeout': True, 'seconds': time.monotonic()-start}
        return {'returncode': p.returncode, 'stdout': stdout, 'stderr': stderr, 'timeout': False, 'seconds': time.monotonic()-start}

    def python(self, code, timeout=30):
        return self.run([PYTHON, '-I', '-c', code], timeout)

    def record(self, name, expected, result, passed):
        row = {'name': name, 'expected': expected, 'passed': bool(passed), **result}
        self.rows.append(row)
        (self.report / 'progress.json').write_text(json.dumps(self.rows, indent=2) + '\n')
        print(json.dumps({'name': name, 'passed': bool(passed), 'seconds': round(result.get('seconds',0),3)}), flush=True)

    def allowed(self, name, code, timeout=30):
        r = self.python(code, timeout)
        self.record(name, 'operation succeeds', r, r['returncode'] == 0 and not r['timeout'])

    def denied(self, name, code):
        # Only an explicit access denial is success; missing files and crashes are not.
        guarded = 'import errno,os,sys\ntry:\n' + ''.join('    '+line+'\n' for line in code.splitlines())
        guarded += '\nexcept OSError as e:\n    print("ERRNO",e.errno,type(e).__name__)\n    sys.exit(0 if e.errno in (errno.EACCES,errno.EPERM) else 7)\nelse:\n    print("ACCESS_UNEXPECTEDLY_ALLOWED")\n    sys.exit(9)\n'
        r = self.python(guarded)
        self.record(name, 'EACCES or EPERM', r, r['returncode'] == 0 and not r['timeout'])

    def main(self):
        protected_input = INPUT / '.qualification-write-canary'
        protected_runtime = RUNTIME / '.qualification-write-canary'
        protected_controller = ROOT / 'controller/.qualification-write-canary'
        for fixture in (protected_input, protected_runtime, protected_controller):
            fixture.write_text('SYNTHETIC_WRITE_CANARY\n')
        manifest = json.loads((ROOT / 'runtime-manifest.json').read_text())
        for relative, digest in manifest['input_sha256'].items():
            self.allowed('read.input.'+relative, f'import hashlib;from pathlib import Path;p=Path({str(INPUT / relative)!r});assert hashlib.sha256(p.read_bytes()).hexdigest()=={digest!r};print("HASH_MATCH")')
        self.allowed('write.work', f'from pathlib import Path;p=Path({str(WORK / "write-probe.txt")!r});p.write_text("qualification-only");assert p.read_text()=="qualification-only"')
        self.allowed('write.private_tmp', f'from pathlib import Path;p=Path({str(SCRATCH / "tmp-probe.txt")!r});p.write_text("scratch");assert p.read_text()=="scratch"')
        self.allowed('clean.environment', 'import os; forbidden=[k for k in os.environ if k in ("CODEX_HOME","DENG_SECRET_ENV_CANARY","HOME","PYTHONPATH") or k.endswith(("_TOKEN","_API_KEY"))];assert not forbidden, forbidden;print(sorted(os.environ))')
        canary = ROOT / 'canaries/operator.txt'
        sibling = ROOT / 'outside-canaries/outside.txt'
        outside_paths = {
            'operator_canary': canary,
            'sibling_temp_canary': sibling,
            'heldout_rubric': PROJECT / 'quality/frozen/rubric.json',
            'review_notes': ROOT / 'canaries/review-notes.txt',
            'unrelated_project': ROOT / 'canaries/unrelated-project.py',
            'controller_source': Path(__file__).resolve(),
            'profile': ROOT / 'profile.toml',
        }
        for label, path in outside_paths.items():
            if not path.is_file():
                raise RuntimeError(f'Invalid negative control: {path} absent outside sandbox')
            self.denied('read.denied.'+label, f'open({str(path)!r},"rb").read(1)')
        self.denied('list.operator_directory', f'os.listdir({str(ROOT / "canaries")!r})')
        self.denied('write.input', f'open({str(protected_input)!r},"ab").write(b"TAMPER")')
        self.denied('write.runtime', f'open({str(protected_runtime)!r},"ab").write(b"TAMPER")')
        self.denied('write.operator_canary', f'open({str(canary)!r},"ab").write(b"TAMPER")')
        self.denied('write.shared_tmp', 'open("/private/tmp/deng-unauthorized-write.txt","wb").write(b"TAMPER")')
        self.denied('write.controller', f'open({str(protected_controller)!r},"ab").write(b"TAMPER")')
        link = WORK / 'outside-link.txt'
        if link.is_symlink(): link.unlink()
        link.symlink_to(canary)
        self.denied('read.symlink_escape', f'open({str(link)!r},"rb").read(1)')
        self.denied('write.symlink_escape', f'open({str(link)!r},"ab").write(b"TAMPER")')
        self.denied('read.path_traversal', 'open("../../canaries/operator.txt","rb").read(1)')
        self.denied('hardlink.outside', f'os.link({str(canary)!r},{str(WORK / "outside-hardlink")!r})')
        # Real shell/search subprocesses, not just Python file APIs.
        r = self.run(['/bin/cat', str(canary)])
        self.record('shell.cat_denied', 'explicit permission denial', r, r['returncode'] != 0 and ('Operation not permitted' in r['stderr'] or 'Permission denied' in r['stderr']))
        r = self.run(['/usr/bin/grep', 'DENG_', str(canary)])
        self.record('search.grep_denied', 'explicit permission denial', r, r['returncode'] != 0 and ('Operation not permitted' in r['stderr'] or 'Permission denied' in r['stderr']))
        self.allowed('subprocess.policy_inherited', f'import subprocess; p=subprocess.run(["/bin/cat",{str(canary)!r}],capture_output=True,text=True);assert p.returncode!=0 and ("Operation not permitted" in p.stderr or "Permission denied" in p.stderr);print(p.stderr)')
        # Image decoding inside the sandbox must obey the same kernel boundary.
        image = INPUT / 'drawings/step-1.png'
        self.allowed('image.python_allowed', f'from PIL import Image;im=Image.open({str(image)!r});im.load();print(im.size)')
        self.denied('image.python_denied', f'from PIL import Image\nImage.open({str(ROOT / "canaries/operator.png")!r}).load()')
        # Never attempt to transmit private content. Targets are dummy network probes.
        self.denied('network.external_tcp', 'import socket\ns=socket.socket();s.settimeout(2);s.connect(("1.1.1.1",443))')
        self.denied('network.external_udp', 'import socket\ns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.sendto(b"DENG_QUALIFICATION_NO_PRIVATE_DATA",("1.1.1.1",53))')
        tcp = socket.socket();tcp.bind(('127.0.0.1',0));tcp.listen(1)
        try:
            self.denied('network.loopback_tcp', f'import socket\ns=socket.socket();s.settimeout(2);s.connect(("127.0.0.1",{tcp.getsockname()[1]}))')
        finally: tcp.close()
        unix_path = str(SCRATCH / 'host-test.sock')
        if Path(unix_path).exists(): Path(unix_path).unlink()
        unix = socket.socket(socket.AF_UNIX)
        original_cwd = Path.cwd()
        try:
            os.chdir(SCRATCH)
            unix.bind('host-test.sock')
            unix.listen(1)
        finally:
            os.chdir(original_cwd)
        try:
            self.denied('network.unix_socket', f'import socket\nos.chdir({str(SCRATCH)!r})\ns=socket.socket(socket.AF_UNIX);s.settimeout(2);s.connect("host-test.sock")')
        finally:
            unix.close();Path(unix_path).unlink()
        # SIGUSR1 has no destructive effect: only a dedicated synthetic process is targeted.
        target = subprocess.Popen([str(PYTHON), '-I', '-c', 'import signal,time;signal.signal(signal.SIGUSR1,lambda *_:None);print("READY",flush=True);time.sleep(30)'], stdout=subprocess.PIPE, text=True)
        try:
            assert target.stdout.readline().strip() == 'READY'
            self.denied('process.signal_outside_sandbox', f'import signal\nos.kill({target.pid},signal.SIGUSR1)')
        finally:
            target.terminate();target.wait(timeout=5)
        # Pure smoke geometry, unrelated to the Deng drawings. Export/reload a 1 x 2 x 3 box.
        self.allowed('cadquery.box_step_roundtrip', f'import cadquery as cq;from pathlib import Path;p=Path({str(WORK / "qualification-box.step")!r});shape=cq.Workplane("XY").box(1,2,3);cq.exporters.export(shape,str(p));reloaded=cq.importers.importStep(str(p));s=reloaded.val();assert s.isValid();assert abs(s.Volume()-6)<1e-8;print("CadQuery",cq.__version__,"volume",s.Volume())', timeout=60)
        # A real render exercises graphics/runtime dependencies without using the study design.
        self.allowed('cadquery.svg_render', f'import cadquery as cq;from pathlib import Path;p=Path({str(WORK / "qualification-box.svg")!r});cq.exporters.export(cq.Workplane("XY").box(1,2,3),str(p));assert "<svg" in p.read_text();print(p.stat().st_size)', timeout=60)
        version = subprocess.run([CODEX,'--version'],capture_output=True,text=True,env=self.host_env,check=True).stdout.strip()
        report = {'qualification_kind':'deterministic OS probes, not a CAD trial','created_utc':datetime.now(timezone.utc).isoformat(),'codex_version':version,'profile_sha256':hashlib.sha256((ROOT/'profile.toml').read_bytes()).hexdigest(),'cad_trial_started':False,'model_generation_calls':0,'all_passed':all(r['passed'] for r in self.rows),'passed':sum(r['passed'] for r in self.rows),'total':len(self.rows),'results':self.rows}
        (self.report/'os-results.json').write_text(json.dumps(report,indent=2)+'\n')
        (ROOT/'qualification/latest-os-report.txt').write_text(str(self.report)+'\n')
        for fixture in (protected_input, protected_runtime, protected_controller):
            fixture.unlink()
        unexpected_write = Path('/private/tmp/deng-unauthorized-write.txt')
        if unexpected_write.exists(): unexpected_write.unlink()
        print(json.dumps({'report':str(self.report/'os-results.json'),'passed':report['passed'],'total':report['total'],'all_passed':report['all_passed']},indent=2))
        return 0 if report['all_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(Qualification().main())
