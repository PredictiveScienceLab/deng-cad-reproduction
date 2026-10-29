"""Terminate the actual preflight controller during inference and a sandbox worker."""
from datetime import datetime,timezone
import json,os,signal,subprocess,sys,time
from pathlib import Path
from client_config import PROJECT,WORK,PYTHON
HERE=Path(__file__).resolve().parent

def alive(pid):
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False

def main():
    root=PROJECT/'sandbox/qualification'/('v3-signals-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'));root.mkdir()
    records=[]
    for mode in ['worker','inference']:
        before=set((PROJECT/'trials').glob('preflight-local-*'))
        pidfile=WORK/'output/signal-worker-pid.txt';pidfile.unlink(missing_ok=True)
        with (root/(mode+'.stdout')).open('w') as out,(root/(mode+'.stderr')).open('w') as err:
            process=subprocess.Popen([str(PYTHON),'-c',f'import preflight;preflight.run(False,_test_mode={mode!r})'],cwd=HERE,stdout=out,stderr=err,start_new_session=True)
            destination=None;ready=False;until=time.monotonic()+20
            while time.monotonic()<until and process.poll() is None:
                new=set((PROJECT/'trials').glob('preflight-local-*'))-before
                if new:destination=sorted(new)[-1]
                ready=bool(destination and (pidfile.exists() if mode=='worker' else (destination/'inference-active').exists()))
                if ready:break
                time.sleep(.03)
            cli_pid=int((destination/'cli-pid.txt').read_text()) if destination and (destination/'cli-pid.txt').exists() else None
            worker_pid=int(pidfile.read_text()) if mode=='worker' and pidfile.exists() else None
            process.send_signal(signal.SIGTERM)
            try:code=process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait();code=process.returncode
        logs=[json.loads(x) for x in (destination/'broker-audit.jsonl').read_text().splitlines()] if destination and (destination/'broker-audit.jsonl').exists() else []
        reaped=(any(x.get('pid')==worker_pid and x.get('worker_reaped') is True for x in logs) if worker_pid else mode=='inference')
        passed=ready and code==143 and cli_pid is not None and not alive(cli_pid) and (worker_pid is None or not alive(worker_pid)) and reaped
        records.append({'mode':mode,'passed':passed,'ready':ready,'controller_exit_code':code,'cli_pid':cli_pid,'worker_pid':worker_pid,'worker_reaped':reaped,'evidence':str(destination)})
    result={'passed':all(x['passed'] for x in records),'probes':records,'real_model_inference':False,'cad_trial_started':False}
    (root/'summary.json').write_text(json.dumps(result,indent=2)+'\n');(HERE/'latest-signal-qualification.txt').write_text(str(root)+'\n')
    print(json.dumps(result,indent=2));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
