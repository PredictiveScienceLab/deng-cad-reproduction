"""Separate synthetic transport preflight. It never supplies the Deng task."""
import argparse
from datetime import datetime,timezone
import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from broker_server_v3 import BrokerServerV3
from tool_gate_v3 import GateV3
from live_gateway import LiveGateway
from client_config import client,PROJECT,WORK
from tool_gate import response_sse
from launch_signals import install_exit_signals

PROMPT='''This is a separate synthetic transport check, not the CAD benchmark. Do not read any task inputs or drawings. The initial attached card is separate from the tool image. Use run_python to print LIVE_PYTHON_OK. Then use view_image on output/transport-preflight.png, inspect that tool image, and finish with TRANSPORT_OK followed by its color and shape. Do not build CAD or invoke check_geometry. Use only the supplied tools.'''

def dependency_hashes():
    names=['preflight.py','client_config.py','live_gateway.py','broker_v3.py','broker_server_v3.py','execution_limits.py','tool_gate_v3.py','trial_timeline.py','tool-instructions.txt','run_trial.py','launch_signals.py']
    return {name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in names}

def run(live,_test_mode=None):
    install_exit_signals()
    destination=PROJECT/'trials'/('preflight-'+('live-' if live else 'local-')+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    destination.mkdir(parents=True)
    (WORK/'output').mkdir(exist_ok=True)
    from PIL import Image,ImageDraw
    image=Image.new('RGB',(240,240),'white');ImageDraw.Draw(image).ellipse((40,40,200,200),fill=(20,70,220));image.save(WORK/'output/transport-preflight.png')
    initial=Image.new('RGB',(240,240),'white');ImageDraw.Draw(initial).rectangle((40,40,200,200),fill=(220,30,30));initial.save(WORK/'output/preflight-initial.png')
    audits=[]
    def audit(event):
        audits.append(event)
        with (destination/'tool-gate.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
    counter=[0]
    def fake(body,*_):
        if _test_mode=='inference':
            (destination/'inference-active').write_text('ready')
            while not _[-1].wait(.02):pass
            raise TimeoutError('Synthetic inference cancelled')
        counter[0]+=1;i=counter[0]
        if i==1:out={'type':'function_call','id':'py-item','call_id':'py','namespace':'deng_cad','name':'run_python','arguments':json.dumps({'source':'print("LIVE_PYTHON_OK")'}),'status':'completed'}
        elif i==2:out={'type':'function_call','id':'image-item','call_id':'image','namespace':'deng_cad','name':'view_image','arguments':json.dumps({'path':'output/transport-preflight.png'}),'status':'completed'}
        else:out={'type':'message','id':'done','role':'assistant','status':'completed','content':[{'type':'output_text','text':'TRANSPORT_OK blue circle','annotations':[]}]}
        if _test_mode=='worker' and i==1:out['arguments']=json.dumps({'source':'import os,signal,time\nfrom pathlib import Path\nos.setsid()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath("output/signal-worker-pid.txt").write_text(str(os.getpid()))\ntime.sleep(30)','timeout_seconds':60})
        return response_sse({'id':'preflight-'+str(i),'object':'response','status':'completed','model':'gpt-6.1-sol','output':[out],'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}})
    deadline=time.monotonic()+240
    broker=BrokerServerV3(deadline,destination/'broker-audit.jsonl',check_reports=destination/'checks')
    gateway=LiveGateway(GateV3(audit=audit),deadline=deadline,audit_path=destination/'gateway-audit.jsonl',transport=None if live else fake)
    process=None;code=None
    try:
        command,env=client(destination,broker,gateway,PROMPT,images=[WORK/'output/preflight-initial.png'])
        with (destination/'cli.stdout').open('w') as out,(destination/'cli.stderr').open('w') as err:
            process=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
            (destination/'cli-pid.txt').write_text(str(process.pid))
            try:code=process.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait();code=process.returncode
    finally:
        gateway.close();broker.close()
        if process and process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
    final=(destination/'last-message.txt').read_text() if (destination/'last-message.txt').exists() else ''
    names=[x['public_name'] for x in audits if x.get('event')=='allowed_call']
    workers=[json.loads(line) for line in (destination/'broker-audit.jsonl').read_text().splitlines()] if (destination/'broker-audit.jsonl').exists() else []
    workers_ok=len(workers)>=2 and all(x.get('success') is True and x.get('worker_reaped') is True for x in workers)
    passed=code==0 and workers_ok and not gateway.errors and 'run_python' in names and 'view_image' in names and 'TRANSPORT_OK' in final and 'blue' in final.lower() and 'circle' in final.lower()
    result={'passed':passed,'kind':'live synthetic transport preflight' if live else 'local scripted transport qualification','cad_trial_started':False,'real_model_inference':live,'model':'gpt-6.1-sol','reasoning_effort':'xhigh','provider_requests':gateway.sequence,'cli_exit_code':code,'tools_used':names,'errors':gateway.errors,'final_message':final,'usage':gateway.usage,'workers_successful_and_reaped':workers_ok,'dependency_hashes':dependency_hashes(),'image_evidence':'Initial attachment is a red square; blue circle is supplied only by the view_image tool.'}
    (destination/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    (HERE/('latest-live-preflight.txt' if live else 'latest-local-preflight.txt')).write_text(str(destination)+'\n')
    print(json.dumps({'passed':passed,'report':str(destination/'summary.json'),'provider_requests':gateway.sequence,'errors':gateway.errors,'cli_exit_code':code,'final_message':final},indent=2))
    return 0 if passed else 1
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');a=p.parse_args();raise SystemExit(run(a.live))
