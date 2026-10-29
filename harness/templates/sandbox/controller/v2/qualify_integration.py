"""Actual CLI -> fail-closed gate -> MCP relay -> external broker -> Seatbelt.
All upstream responses are fixed local fixtures. No model inference or CAD trial.
"""
from __future__ import annotations
import copy
from datetime import datetime,timezone
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import tomllib

from broker import ROOT, WORK, INPUT, PYTHON
from broker_server import BrokerServer
from tool_gate import Gate, make_server, response_sse, MODEL

CLI=Path('@RUNTIME@/cli/codex')

def toml(v):
    if isinstance(v,bool):return str(v).lower()
    if isinstance(v,str):return json.dumps(v)
    if isinstance(v,dict):return '{'+','.join(json.dumps(k)+'='+toml(x) for k,x in v.items())+'}'
    if isinstance(v,list):return '['+','.join(toml(x) for x in v)+']'
    return str(v)

def text_of(x):
    if isinstance(x,str):return x
    if isinstance(x,list):return '\n'.join(text_of(y) for y in x)
    if isinstance(x,dict):return text_of(x.get('text',''))
    return ''

def images(x):
    if isinstance(x,dict):return int(x.get('type')=='input_image')+sum(images(y) for y in x.values())
    if isinstance(x,list):return sum(images(y) for y in x)
    return 0

def run_session(destination,cases,negative=False):
    destination.mkdir()
    requests=[]; audit=[]; outcomes=[]; request_errors=[]
    class Mock(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            # Save no image bytes or auth material. These are only scripted local requests.
            request_index=len(requests)
            names=[(ns['name'],[t['name'] for t in ns.get('tools',[])]) for item in req['input'] if item.get('type')=='additional_tools' for ns in item['tools']]
            requests.append({'model':req.get('model'),'effort':req.get('reasoning',{}).get('effort'),'public_namespaces':names,'authorization_present':'Authorization' in self.headers})
            if request_index>0 and request_index<=len(cases):
                previous=cases[request_index-1]
                outputs=[x for x in req['input'] if x.get('type')=='function_call_output' and x.get('call_id')==previous['id']]
                output=outputs[0].get('output') if len(outputs)==1 else None
                raw=text_of(output)
                passed=previous['expect'](raw,images(output))
                outcomes.append({'name':previous['id'],'passed':bool(passed),'output_text':raw[:16000],'image_blocks':images(output)})
            if request_index<len(cases):
                case=cases[request_index]
                item=copy.deepcopy(case['item'])
            else:
                item={'type':'message','id':'qualification_done','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Scripted qualification complete. No inference or CAD trial.','annotations':[]}]}
            response={'id':f'resp_{request_index}','object':'response','status':'completed','model':MODEL,'output':[item],'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}}
            data=response_sse(response)
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Mock)
    gate=make_server(f'http://127.0.0.1:{upstream.server_port}',Gate(audit=audit.append))
    broker=BrokerServer(time.monotonic()+120,destination/'broker-audit.jsonl')
    for server in (upstream,gate):threading.Thread(target=server.serve_forever,daemon=True).start()
    (destination/'home').mkdir();(destination/'codex-home').mkdir()
    config=tomllib.loads((ROOT/'profile.toml').read_text())
    features={k:False for k in 'apps plugins remote_plugin hooks browser_use browser_use_external computer_use memories multi_agent multi_agent_v2 image_generation skill_search skill_mcp_dependency_install tool_suggest shell_snapshot api_key_model_discovery enable_request_compression standalone_web_search goals workspace_dependencies request_permissions_tool unbounded_connection_retries daemon_auto_start system_proxy_fallback respect_system_proxy'.split()}
    features.update(code_mode_host=True,skip_host_skill_discovery=True)
    config['features'].update(features)
    config.update(model=MODEL,model_reasoning_effort='xhigh',model_provider='deng_gate_qualification',cli_auth_credentials_store='file',analytics={'enabled':False},feedback={'enabled':False},check_for_update_on_startup=False,history={'persistence':'none'},otel={'exporter':'none'})
    config['mcp_servers']={'deng_cad':{'command':str(PYTHON),'args':[str(Path(__file__).with_name('mcp_relay.py'))],'env':broker.environment(),'enabled_tools':['run_python','view_image'],'default_tools_approval_mode':'prompt','tools':{'run_python':{'approval_mode':'approve'},'view_image':{'approval_mode':'approve'}},'required':True,'startup_timeout_sec':15,'tool_timeout_sec':70}}
    config['model_providers']={'deng_gate_qualification':{'name':'Local qualification gate','base_url':f'http://127.0.0.1:{gate.server_port}','wire_api':'responses','requires_openai_auth':False,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0}}
    command=[str(CLI),'--no-daemon','-a','never','exec','--ignore-user-config','--ignore-rules','--strict-config','--skip-git-repo-check','--ephemeral','--json','-C',str(WORK)]
    for k,v in config.items():command.extend(['-c',k+'='+toml(v)])
    command.append('Synthetic fixed local qualification. No CAD task or real model inference.')
    env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(destination/'home'),'CODEX_HOME':str(destination/'codex-home'),'LANG':'en_US.UTF-8','NO_PROXY':'127.0.0.1,localhost','TMPDIR':str(WORK)}
    process=None
    try:
        with (destination/'cli.stdout').open('w') as out,(destination/'cli.stderr').open('w') as err:
            process=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
            try:code=process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait();code=process.returncode
        if negative:
            passed=code!=0 and any(x.get('event')=='denied' for x in audit) and not any(x.get('event')=='allowed_call' for x in audit) and broker.broker.sequence==0
        else:
            passed=code==0 and len(outcomes)==len(cases) and all(x['passed'] for x in outcomes) and not any(x.get('event')=='denied' for x in audit)
        passed=passed and all(x['model']==MODEL and x['effort']=='xhigh' and x['public_namespaces']==[('deng_cad',['run_python','view_image'])] and not x['authorization_present'] for x in requests)
        report={'passed':passed,'kind':'actual CLI + gate + MCP + no-fork sandbox scripted qualification','real_model_inference':False,'cad_trial_started':False,'cli_exit_code':code,'requests':requests,'probes':outcomes,'audit':audit,'broker_calls':broker.broker.sequence,'negative_response':negative}
        (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'report':str(destination/'report.json'),'passed':passed,'probes':len(outcomes),'broker_calls':broker.broker.sequence}),flush=True)
        return report
    finally:
        if process and process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
        for server in (gate,upstream):server.shutdown();server.server_close()
        broker.close()

def case(name,tool,arguments,expect):
    return {'id':name,'item':{'type':'function_call','id':name+'_item','call_id':name,'namespace':'deng_cad','name':tool,'arguments':json.dumps(arguments),'status':'completed'},'expect':expect}

def main():
    destination=ROOT/'qualification'/('v2-integration-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    destination.mkdir()
    outside=ROOT/'canaries/operator.txt'
    cases=[
        case('python_allowed','run_python',{'source':'print("PY_WORKER_OK")'},lambda raw,n:'PY_WORKER_OK' in raw and '\\"success\\": true' in raw),
        case('drawing_allowed','view_image',{'path':str(INPUT/'drawings/step-1.png')},lambda raw,n:n==1),
        case('image_outside_denied','view_image',{'path':str(ROOT/'canaries/operator.png')},lambda raw,n:n==0 and 'denied' in raw.lower()),
        case('file_outside_denied','run_python',{'source':f'import errno\ntry:open({str(outside)!r}).read()\nexcept PermissionError:print("READ_DENIED")\nelse:raise RuntimeError("escaped")'},lambda raw,n:'READ_DENIED' in raw),
        case('fork_denied','run_python',{'source':'import os\ntry:os.fork()\nexcept PermissionError:print("FORK_DENIED")\nelse:os._exit(19)'},lambda raw,n:'FORK_DENIED' in raw),
        case('network_denied','run_python',{'source':'import socket\ns=socket.socket();s.settimeout(.2)\ntry:s.connect(("1.1.1.1",443))\nexcept PermissionError:print("NETWORK_DENIED")\nelse:raise RuntimeError("escaped")'},lambda raw,n:'NETWORK_DENIED' in raw),
        case('setsid_timeout_reaped','run_python',{'source':'import os,signal,time\nos.setsid()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint("DETACHED_SELF",flush=True)\ntime.sleep(10)','timeout_seconds':1},lambda raw,n:'DETACHED_SELF' in raw and 'timed_out' in raw and 'SIGKILL' in raw and '\\"worker_reaped\\": true' in raw),
    ]
    positive=run_session(destination/'allowed',cases)
    denied_case={'id':'blocked_delegation','item':{'type':'function_call','id':'bad_item','call_id':'bad','namespace':'collaboration','name':'spawn_agent','arguments':json.dumps({'task_name':'must_not_start','message':'Synthetic forbidden call; must never reach dispatcher.'}),'status':'completed'},'expect':lambda *_:False}
    negative=run_session(destination/'forbidden',[denied_case],True)
    result={'passed':positive['passed'] and negative['passed'],'positive_report':str(destination/'allowed/report.json'),'negative_report':str(destination/'forbidden/report.json'),'real_model_inference':False,'cad_trial_started':False}
    (destination/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    (ROOT/'qualification/latest-v2-integration.txt').write_text(str(destination)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
