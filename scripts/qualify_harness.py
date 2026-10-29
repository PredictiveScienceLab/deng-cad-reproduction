#!/usr/bin/env python3
"""Requalify the relocated harness with local fixed responses and real workers.

No model inference or original CAD trial. Worker DNS/network denial probes use
synthetic data; the host DNS positive control resolves example.com.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import platform,signal,subprocess,sys

REPO=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',required=True,type=Path)
    p.add_argument('--runtime-root',type=Path)
    p.add_argument('--units-only',action='store_true',help='Run unit tests only; does not qualify live launch')
    a=p.parse_args(argv)
    if platform.system()!='Darwin' or platform.machine()!='arm64':p.error('Real sandbox qualification requires macOS arm64')
    root=a.project_root.expanduser().resolve(strict=True)
    materialization=json.loads((root/'materialization.json').read_text())
    runtime=(a.runtime_root or Path(materialization['runtime_root'])).expanduser().resolve(strict=True)
    from materialize import freeze_quality,validate_layout
    validate_layout(root,runtime)
    python=runtime/'venv/bin/python';sandbox=root/'sandbox';v2=sandbox/'controller/v2';v3=sandbox/'controller/v3'
    if Path(materialization['runtime_root'])!=runtime:p.error('Runtime must match materialization')
    if (root/'trials/trial-001').exists():p.error('Qualification changes synthetic work files; use a fresh materialized project, never an existing trial')
    environment=dict(os.environ)
    for key in ('DENG_ENABLE_LIVE','DENG_CHATGPT_AUTH_FILE','OPENAI_API_KEY','PYTHONPATH'):
        environment.pop(key,None)
    report=sandbox/'qualification'/('reproduction-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    report.mkdir(parents=True)
    for path in (sandbox/'candidate/work',sandbox/'candidate/tmp',sandbox/'control',sandbox/'canaries',sandbox/'outside-canaries',root/'trials'):
        path.mkdir(parents=True,exist_ok=True)
    for name in ('operator.txt','review-notes.txt','unrelated-project.py'):
        (sandbox/'canaries'/name).write_text('SYNTHETIC_PROTECTED_CANARY\n')
    (sandbox/'outside-canaries/outside.txt').write_text('SYNTHETIC_OUTSIDE_CANARY\n')
    # A non-secret image fixture with no dependency on another user's files.
    subprocess.run([str(python),'-I','-c','from PIL import Image;import sys;Image.new("RGB",(8,8),"red").save(sys.argv[1])',str(sandbox/'canaries/operator.png')],env=environment,check=True)
    input_hashes={str(path.relative_to(sandbox/'candidate/input')):sha(path) for path in sorted((sandbox/'candidate/input').rglob('*')) if path.is_file()}
    runtime_manifest=json.loads((runtime/'runtime-manifest.json').read_text())
    manifest={'schema_version':1,'input_sha256':input_hashes,'runtime_manifest_sha256':sha(runtime/'runtime-manifest.json'),
        'runtime_versions':runtime_manifest,'qualification_scope':'Fresh relocation; no inherited original-run qualification'}
    (sandbox/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    freeze_quality(root,runtime)
    commands=[
        ('entrypoint-unit',[python,'-m','unittest','-v','test_harness_entrypoints.py'],REPO/'scripts'),
        ('controller-unit',[python,'-m','unittest','-v','test_controller.py'],sandbox/'controller'),
        ('v2-unit',[python,'-m','unittest','-v','test_worker_supervisor.py','test_tool_gate.py'],v2),
        ('v3-unit',[python,'-m','unittest','-v','test_broker_v3.py','test_live_gateway.py','test_trial_timeline.py'],v3),
    ]
    if not a.units_only:
        commands.extend([
            ('worker-isolation',[python,v2/'qualify_workers.py'],v2),
            ('cli-fixed-responses',[python,v3/'qualify_cli_v3.py'],v3),
            ('worker-deadlines',[python,v3/'qualify_deadlines_v3.py'],v3),
            ('timing-integration',[python,v3/'qualify_timing_integration.py'],v3),
            ('loopback-preflight',[python,v3/'preflight.py'],v3),
            ('signal-cleanup',[python,v3/'qualify_signals.py'],v3),
        ])
    results=[]
    for name,command,cwd in commands:
        print(json.dumps({'qualification':name,'status':'running'}),flush=True)
        with (report/(name+'.log')).open('w') as log:
            process=subprocess.Popen([str(x) for x in command],cwd=cwd,env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                code=process.wait(timeout=420)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGINT)
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
                code=124
        results.append({'name':name,'passed':code==0,'exit_code':code,'log':str(report/(name+'.log'))})
        print(json.dumps(results[-1]),flush=True)
        if code:break
    passed=len(results)==len(commands) and all(r['passed'] for r in results)
    summary={'passed':passed,'live_launch_qualified':passed and not a.units_only,'units_only':a.units_only,
        'real_model_inference':False,'cad_trial_started':False,'model_metadata':'gpt-6.1-sol','reasoning_effort_metadata':'xhigh','results':results}
    (report/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (sandbox/'reproduction-qualification.json').write_text(json.dumps({**summary,'report':str(report)},indent=2)+'\n')
    (sandbox/'trial-readiness.json').write_text(json.dumps({'sandbox_qualified':passed and not a.units_only,'user_authorized_trial':False,'cad_trial_started':False})+'\n')
    if passed and not a.units_only:
        paths=list((sandbox/'controller').rglob('*.py'))+[sandbox/'profile.toml',v2/'worker.sb',v2/'tool-instructions.txt',v3/'tool-instructions.txt',sandbox/'control/candidate-environment.json',sandbox/'runtime-manifest.json',runtime/'runtime-manifest.json',root/'quality/freeze.json',root/'quality/frozen/rubric.json',root/'review/packet-manifest.json']
        paths += [runtime/'cli/codex',runtime/'cli/codex-code-mode-host']
        paths += [REPO/'scripts'/name for name in ('setup_runtime.py','materialize.py','qualify_harness.py','run_trial.py')]
        freeze={'schema_version':1,'status':'QUALIFIED_FOR_AUTHORIZED_TRIAL','scope':'Relocated harness tested with fixed loopback responses; live inference remains opt-in','files':{str(path):sha(path) for path in sorted(paths)},'qualification_report':str(report/'summary.json')}
        (v3/'launch-freeze.json').write_text(json.dumps(freeze,indent=2)+'\n')
    else:
        (v3/'launch-freeze.json').unlink(missing_ok=True)
    return 0 if passed else 1

if __name__=='__main__':raise SystemExit(main())
