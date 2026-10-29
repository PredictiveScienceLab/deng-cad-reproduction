#!/usr/bin/env python3
"""Explicit opt-in launcher for the optional authenticated live experiment."""
import argparse,hashlib,json,os
from pathlib import Path
import subprocess,sys

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',required=True,type=Path)
    p.add_argument('--live',action='store_true',help='Authorize a live synthetic preflight and the 60-minute model trial')
    p.add_argument('--auth-file',type=Path,help='Owner-only existing ChatGPT CLI auth file; never copied')
    a=p.parse_args(argv)
    if not a.live:p.error('No model request made. Explicit --live is required; use qualify_harness.py for offline qualification')
    if a.auth_file is None:p.error('--auth-file is required for the optional ChatGPT-authenticated path')
    root=a.project_root.expanduser().resolve(strict=True)
    auth=a.auth_file.expanduser().resolve(strict=True)
    if auth.is_relative_to(root):p.error('Keep authentication outside the materialized experiment')
    here=root/'sandbox/controller/v3'
    if not (root/'sandbox/reproduction-qualification.json').is_file():p.error('Run qualify_harness.py successfully first')
    q=json.loads((root/'sandbox/reproduction-qualification.json').read_text())
    if not q.get('passed') or q.get('real_model_inference') is not False:p.error('Local qualification is absent or failed')
    freeze=json.loads((here/'launch-freeze.json').read_text())
    if freeze.get('status')!='QUALIFIED_FOR_AUTHORIZED_TRIAL':p.error('Launch freeze is not qualified')
    for name,digest in freeze['files'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:p.error('Qualified dependency changed: '+name)
    if (root/'trials/trial-001').exists():p.error('Use a fresh materialized project for another trial')
    settings=json.loads((root/'materialization.json').read_text())
    runtime=Path(settings['runtime_root']).resolve(strict=True)
    from materialize import validate_layout
    validate_layout(root,runtime)
    if auth.is_relative_to(runtime):p.error('Keep authentication outside the candidate-readable runtime')
    env=dict(os.environ,DENG_ENABLE_LIVE='1',DENG_CHATGPT_AUTH_FILE=str(auth))
    env.pop('PYTHONPATH',None)
    # Each command enforces existing code freezes; the preflight uses separate
    # synthetic inputs and produces live model requests before the CAD trial.
    subprocess.run([str(runtime/'venv/bin/python'),str(here/'preflight.py'),'--live'],env=env,check=True)
    (here/'trial-authorization.json').write_text(json.dumps({'authorized':True,'mechanism':'explicit --live switch','model':'gpt-6.1-sol','reasoning_effort':'xhigh'})+'\n')
    return subprocess.call([str(runtime/'venv/bin/python'),str(here/'run_trial.py')],env=env)

if __name__=='__main__':raise SystemExit(main())
