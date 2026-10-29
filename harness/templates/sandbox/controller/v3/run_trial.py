"""One authorized Deng trial, with frozen inputs/rubric and external supervision."""
from datetime import datetime,timezone
import hashlib,json,os,shutil,signal,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
from client_config import client,PROJECT,WORK
from broker_server_v3 import BrokerServerV3
from tool_gate_v3 import GateV3
from live_gateway import LiveGateway
from trial_timeline import TrialTimeline,bounded_snapshot_preflight
from controller import snapshot_output
from evaluate import verify_freeze,evaluate
from launch_signals import install_exit_signals

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,data):
    target=Path(path);temporary=target.with_suffix(target.suffix+'.tmp')
    temporary.write_text(json.dumps(data,indent=2,allow_nan=False)+'\n');temporary.replace(target)
def verify_protocol():
    freeze=json.loads((HERE/'launch-freeze.json').read_text())
    if freeze['status']!='QUALIFIED_FOR_AUTHORIZED_TRIAL':raise RuntimeError('Trial launch is not qualified')
    for path,digest in freeze['files'].items():
        if sha(path)!=digest:raise RuntimeError('Launch dependency changed: '+path)
    verify_freeze()
    packet=json.loads((PROJECT/'review/packet-manifest.json').read_text())
    for name,digest in packet['candidate_input_files'].items():
        for base in (PROJECT/'agent-input',PROJECT/'sandbox/candidate/input'):
            if sha(base/name)!=digest:raise RuntimeError('Candidate input changed: '+name)
    auth=json.loads((HERE/'trial-authorization.json').read_text())
    if auth.get('authorized') is not True or auth.get('mechanism')!='explicit --live switch' or os.environ.get('DENG_ENABLE_LIVE')!='1':raise RuntimeError('Live trial requires explicit current authorization')
    preflight=Path((HERE/'latest-live-preflight.txt').read_text().strip())
    result=json.loads((preflight/'summary.json').read_text())
    if not result.get('passed') or result.get('model')!='gpt-6.1-sol' or result.get('reasoning_effort')!='xhigh':raise RuntimeError('Live preflight did not pass')
    from preflight import dependency_hashes
    if result.get('dependency_hashes')!=dependency_hashes():raise RuntimeError('Live preflight used different launch code or tool instructions')
    return freeze,preflight

def main():
    interrupted=install_exit_signals()
    freeze,preflight=verify_protocol()
    destination=PROJECT/'trials/trial-001';destination.mkdir()
    previous=destination/'prior-qualification-state';previous.mkdir()
    for directory in (WORK,PROJECT/'sandbox/candidate/tmp'):
        if directory.is_symlink() or not directory.is_dir():raise ValueError('Canonical candidate directory required')
        directory.rename(previous/directory.name);directory.mkdir(mode=0o700)
    (WORK/'output').mkdir()
    (destination/'input-packet.json').write_text((PROJECT/'review/packet-manifest.json').read_text())
    shutil.copyfile(HERE/'launch-freeze.json',destination/'launch-freeze.json')
    shutil.copyfile(HERE/'tool-instructions.txt',destination/'tool-instructions.txt')
    prompt=(PROJECT/'agent-input/prompt.txt').read_text()+'\n\n'+(HERE/'tool-instructions.txt').read_text()
    started=time.monotonic();started_wall=time.time();deadline=started+3600
    state={'schema_version':1,'trial':'trial-001','status':'starting','cad_trial_started':True,'model':'gpt-6.1-sol','reasoning_effort':'xhigh','started_utc':datetime.fromtimestamp(started_wall,timezone.utc).isoformat(),'started_unix':started_wall,'checkpoint_seconds':2100,'stop_seconds':3600,'quality_freeze_sha256':sha(PROJECT/'quality/freeze.json'),'launch_freeze_sha256':sha(HERE/'launch-freeze.json'),'input_packet_sha256':sha(PROJECT/'review/packet-manifest.json'),'live_preflight':str(preflight)}
    save(destination/'status.json',state)
    process_box=[None];stop_reasons=[]
    broker=BrokerServerV3(deadline,destination/'broker-audit.jsonl',check_reports=destination/'checks')
    def audit(event):
        with (destination/'tool-gate.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
    gateway=LiveGateway(GateV3(audit=audit),deadline=deadline,audit_path=destination/'gateway-audit.jsonl')
    def stop(reason):
        stop_reasons.append(reason);gateway.cancel_event.set()
        process=process_box[0]
        if process and process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    def snapshot(target):
        bounded_snapshot_preflight(WORK/'output')
        return snapshot_output(WORK/'output',target,candidate_workspace=WORK)
    timeline=TrialTimeline(started_at=started,worker_lock=broker.broker.lock,cancel_event=broker.broker.cancel_event,snapshot_root=destination/'snapshots',snapshot=snapshot,stop_callback=stop)
    broker.broker.set_timeline(timeline);timeline.start()
    process=None;code=None;failure=None
    try:
        images=[PROJECT/'agent-input/drawings'/name for name in ('step-1.png','step-2.png','step-3.png','step-4-assembly.png','step-4-stand.png')]
        command,env=client(destination,broker,gateway,prompt,images=images)
        with (destination/'cli.stdout').open('w') as out,(destination/'cli.stderr').open('w') as err:
            process=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);process_box[0]=process
            state.update(status='running',cli_pid=process.pid,controller_pid=os.getpid());save(destination/'status.json',state)
            print(json.dumps({'status':'running','trial':str(destination),'model':'gpt-6.1-sol','effort':'xhigh','started_utc':state['started_utc']}),flush=True)
            while process.poll() is None:
                now=time.monotonic()
                if now>=deadline or gateway.cancel_event.is_set():
                    stop('deadline' if now>=deadline else 'controller_stop')
                    try:process.wait(timeout=1)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
                    break
                state.update(elapsed_seconds=now-started,provider_requests=gateway.sequence,python_image_calls=broker.broker.sequence,geometry_check_calls=broker.broker.check_calls,timeline=timeline.report())
                save(destination/'status.json',state);time.sleep(.25)
            code=process.returncode
            reason='agent_finished' if code==0 else 'deadline' if time.monotonic()>=deadline else 'cli_error'
            timeline.finish(reason)
    except BaseException as error:
        failure={'type':type(error).__name__,'message':str(error)[:2000]};timeline.abort('interrupted' if interrupted[0] else 'launcher_error')
    finally:
        gateway.close();broker.close()
        if process:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            if process.poll() is None:process.wait(timeout=2)
        timeline_report=timeline.report()
        state.update(status='execution_finished',finished_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.monotonic()-started,cli_exit_code=code,provider_requests=gateway.sequence,gateway_errors=gateway.errors,usage=gateway.usage,python_image_calls=broker.broker.sequence,geometry_check_calls=broker.broker.check_calls,timeline=timeline_report,launcher_error=failure,stop_reasons=stop_reasons)
        save(destination/'status.json',state);save(destination/'timeline.json',timeline_report)
    # Evaluation is outside candidate runtime and does not continue the trial.
    evaluations={}
    for label,record in [('checkpoint-35',timeline_report.get('checkpoint')),('final',timeline_report.get('final'))]:
        if interrupted[0]:
            evaluations[label]={'status':'not_run','reason':'Controller interrupted; sealed output preserved'};continue
        if not record or not record.get('valid') or not record.get('snapshot'):
            evaluations[label]={'status':'unavailable','reason':'No valid cutoff snapshot'};continue
        source=Path(record['snapshot']['path'])
        if label=='final' and evaluations.get('checkpoint-35',{}).get('snapshot')==str(source):
            evaluations[label]=dict(evaluations['checkpoint-35'],same_as='checkpoint-35');continue
        workspace=destination/(label+'-evaluation-work');output=workspace/'output';output.mkdir(parents=True)
        for child in source.iterdir():
            if child.name=='.snapshot-manifest.json':continue
            if child.is_dir():shutil.copytree(child,output/child.name)
            else:shutil.copyfile(child,output/child.name)
        try:
            result=evaluate(workspace,destination/(label+'-evaluation'),writers_stopped=True)
            evaluations[label]={'status':result['status'],'confirmed_correct':result['score']['confirmed_correct'],'counts':result['score']['status_counts'],'snapshot':str(source),'report':str(destination/(label+'-evaluation/worksheet.html'))}
        except Exception as error:evaluations[label]={'status':'inconclusive','snapshot':str(source),'error':type(error).__name__+': '+str(error)[:1000]}
    state.update(status='interrupted' if interrupted[0] else 'finished',evaluations=evaluations,final_agent_message=(destination/'last-message.txt').read_text() if (destination/'last-message.txt').is_file() else '')
    save(destination/'status.json',state)
    readiness_path=PROJECT/'sandbox/trial-readiness.json';ready=json.loads(readiness_path.read_text());ready.update(user_authorized_trial=True,cad_trial_started=True,trial_finished=True,trial_directory=str(destination),reason='Trial interrupted; available snapshots retained.' if interrupted[0] else 'Trial execution ended; review artifact and evaluation results in the trial record.');save(readiness_path,ready)
    print(json.dumps({'status':'finished','trial':str(destination),'elapsed_seconds':state['elapsed_seconds'],'cli_exit_code':code,'evaluations':evaluations,'launcher_error':failure},indent=2),flush=True)
    return 0 if code==0 and failure is None and not timeline_report['errors'] else 1
if __name__=='__main__':raise SystemExit(main())
