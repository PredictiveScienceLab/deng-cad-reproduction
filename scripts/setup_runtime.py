#!/usr/bin/env python3
"""Prepare a self-contained macOS arm64 runtime; never reads authentication."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]
PYTHON_VERSION='3.12.14'
CODEX_VERSION='0.164.0'

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def run(command):return subprocess.run([str(x) for x in command],check=True,capture_output=True,text=True).stdout.strip()

def clone(source,destination):
    # APFS copy-on-write avoids another 2GB physical copy where supported.
    result=subprocess.run(['/bin/cp','-cRp',str(source),str(destination)],capture_output=True,text=True)
    if result.returncode:
        if destination.exists():shutil.rmtree(destination)
        shutil.copytree(source,destination,symlinks=True)

def rebase(destination,old_root):
    for path in destination.rglob('*'):
        if path.is_symlink():
            target=os.readlink(path)
            if target.startswith(str(old_root)+'/'):
                path.unlink();path.symlink_to(str(destination)+target[len(str(old_root)):])
    for path in [destination/'venv/pyvenv.cfg',*(destination/'venv/bin').iterdir()]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>262144:continue
        raw=path.read_bytes()
        if b'\0' not in raw and str(old_root).encode() in raw:
            path.chmod(path.stat().st_mode|0o200)
            path.write_bytes(raw.replace(str(old_root).encode(),str(destination).encode()))

def validate(destination):
    python=destination/'venv/bin/python'
    data=json.loads(run([python,'-I','-c',
        'import importlib.metadata as m,json,sys;print(json.dumps({"python":sys.version.split()[0],"base_prefix":sys.base_prefix,"prefix":sys.prefix,"packages":{d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}}))']))
    if data['python']!=PYTHON_VERSION:raise RuntimeError('Expected CPython '+PYTHON_VERSION)
    for key in ('base_prefix','prefix'):
        if not Path(data[key]).resolve().is_relative_to(destination):raise RuntimeError('Python runtime escapes installed root: '+key)
    expected={}
    for line in (REPO/'runtime/requirements-lock.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name,version=line.split('==');expected[name.lower().replace('_','-')]=version
    errors={name:{'expected':version,'found':data['packages'].get(name)} for name,version in expected.items() if data['packages'].get(name)!=version}
    if errors:raise RuntimeError('Runtime version mismatch: '+json.dumps(errors))
    version=run([destination/'cli/codex','--version'])
    if CODEX_VERSION not in version:raise RuntimeError('Expected Codex CLI '+CODEX_VERSION)
    for name in ('codex','codex-code-mode-host'):
        if not (destination/'cli'/name).is_file():raise RuntimeError('Missing CLI companion '+name)
    external=[]
    for path in destination.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(destination):external.append(str(path.relative_to(destination)))
    if external:raise RuntimeError('Runtime contains external symlink targets: '+json.dumps(external[:20]))
    return {'python':data['python'],'packages':data['packages'],'codex_version':version,
            'critical_sha256':{str(p.relative_to(destination)):sha(p) for p in
                [destination/'python/bin/python3.12',destination/'cli/codex',destination/'cli/codex-code-mode-host']},
            'requirements_sha256':sha(REPO/'runtime/requirements-lock.txt'),'external_symlink_targets':0}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination',required=True,type=Path)
    p.add_argument('--runtime-source',type=Path,help='Copy an existing self-contained runtime; performs no download')
    p.add_argument('--install',action='store_true',help='Explicitly allow pip to download pinned scientific packages')
    p.add_argument('--python-source',type=Path,help='Self-contained CPython3.12.14 root containing bin/python3.12')
    p.add_argument('--codex-source',type=Path,help='Directory containing Codex0.164.0 and codex-code-mode-host')
    a=p.parse_args(argv)
    if platform.system()!='Darwin' or platform.machine()!='arm64':p.error('The qualified sandbox requires macOS arm64; other platforms are not qualified')
    destination=a.destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():p.error('Destination must not exist')
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination=destination.parent.resolve()/destination.name
    if a.runtime_source:
        if a.install or a.python_source or a.codex_source:p.error('Use either --runtime-source or the standalone --install path')
        source=a.runtime_source.expanduser().resolve(strict=True)
        for name in ('python','venv','cli'):
            if not (source/name).is_dir():p.error('Source lacks '+name)
        # Copy only the three runtime directories, never an arbitrary root tree.
        destination.mkdir()
        for name in ('python','venv'):clone(source/name,destination/name)
        (destination/'cli').mkdir()
        for name in ('codex','codex-code-mode-host'):shutil.copy2(source/'cli'/name,destination/'cli'/name)
        rebase(destination,source)
        mode='copy_existing_runtime'
    else:
        if not (a.install and a.python_source and a.codex_source):p.error('Standalone setup needs --install --python-source ROOT --codex-source DIR')
        source=a.python_source.expanduser().resolve(strict=True)
        if not (source/'bin/python3.12').is_file():p.error('Python source must be a standalone CPython root')
        cli=a.codex_source.expanduser().resolve(strict=True)
        destination.mkdir();clone(source,destination/'python');(destination/'cli').mkdir()
        for name in ('codex','codex-code-mode-host'):shutil.copy2(cli/name,destination/'cli'/name)
        run([destination/'python/bin/python3.12','-m','venv',destination/'venv'])
        run([destination/'venv/bin/python','-m','pip','install','--disable-pip-version-check','-r',REPO/'runtime/requirements-lock.txt'])
        mode='standalone_pinned_install'
    report={'schema_version':1,'platform':{'system':platform.system(),'machine':platform.machine()},
        'setup_mode':mode,'credentials_copied':False,'network_download_requested':a.install,**validate(destination)}
    (destination/'runtime-manifest.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'runtime_root':str(destination),'manifest':str(destination/'runtime-manifest.json'),'validated':True,'setup_mode':mode},indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
