"""Real CLI transport with fixed local responses; synthetic CAD fixtures only."""
import copy
from datetime import datetime, timezone
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import tomllib

from broker_v3 import PROJECT, ROOT, WORK, INPUT, PYTHON, V2, EVALUATOR, QualityCheckService
from broker_server_v3 import BrokerServerV3
from tool_gate_v3 import GateV3, make_server, response_sse, MODEL
from qualify_integration import toml, text_of, images, case

HERE = Path(__file__).resolve().parent
CLI = Path('@RUNTIME@/cli/codex')


def hashes():
    paths = list(V2.glob('*.py'))+[V2/'worker.sb']+list(EVALUATOR.glob('*.py'))
    paths += [PROJECT/'quality/freeze.json',PROJECT/'quality/frozen/rubric.json']
    return {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def decode_feedback(raw):
    # Code Mode prepends a human-readable timing block before its JSON text.
    parsed=json.JSONDecoder().raw_decode(raw[raw.index('{"result"'):])[0]
    return json.loads(parsed['result']['content'][0]['text'])


def main():
    before=hashes()
    destination=HERE/('cli-qualification-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    destination.mkdir()
    fixture=destination/'synthetic-work';output=fixture/'output';output.mkdir(parents=True)
    build='''from pathlib import Path
import cadquery as cq
out=Path(__file__).resolve().parent
main=cq.Solid.makeBox(12,10,4)
stand=cq.Solid.makeBox(12,10,3).translate((0,0,-3))
for name in ('step-1.step','step-2.step','step-3.step','main-part.step','stand.step','assembly.step'):
    shape=cq.Compound.makeCompound([main,stand]) if name=='assembly.step' else stand if name=='stand.step' else main
    cq.exporters.export(shape,str(out/name))
'''
    (output/'build.py').write_text(build)
    exec(compile(build,'<trusted-synthetic-fixture>','exec'),{'__file__':str(output/'build.py')})
    def service_factory(_workspace,reports,**kwargs):
        return QualityCheckService(fixture,reports,**kwargs)
    requests=[];audit=[];outcomes=[];handler_errors=[]
    cases=[
        case('python','run_python',{'source':'print("V3_PYTHON_OK")'},lambda raw,n:'V3_PYTHON_OK' in raw),
        case('drawing','view_image',{'path':str(INPUT/'drawings/step-1.png')},lambda raw,n:n==1),
        case('check1','check_geometry',{},lambda raw,n:decode_feedback(raw).get('integrity_status')=='pass' and decode_feedback(raw).get('rebuild_status')=='pass' and decode_feedback(raw).get('checks_remaining')==3),
    ]
    for number in (2,3,4):
        cases.append(case('check'+str(number),'check_geometry',{},
            lambda raw,n,k=number:decode_feedback(raw).get('code')=='checker_error' and decode_feedback(raw).get('checks_remaining')==4-k))
    cases.append(case('check5','check_geometry',{},lambda raw,n:decode_feedback(raw).get('code')=='quota_exhausted' and decode_feedback(raw).get('checks_remaining')==0))
    class Scripted(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            try:
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                index=len(requests)
                names=[(ns['name'],[t['name'] for t in ns.get('tools',[])]) for item in request['input'] if item.get('type')=='additional_tools' for ns in item['tools']]
                requests.append({'model':request.get('model'),'effort':request.get('reasoning',{}).get('effort'),'namespaces':names,'authorization_present':'Authorization' in self.headers})
                if 0<index<=len(cases):
                    previous=cases[index-1]
                    outputs=[x for x in request['input'] if x.get('type')=='function_call_output' and x.get('call_id')==previous['id']]
                    value=outputs[0].get('output') if len(outputs)==1 else None
                    raw=text_of(value)
                    if previous['id'].startswith('check'):
                        (destination/(previous['id']+'-transport.json')).write_text(json.dumps({'matched_outputs':outputs,'input_types':[{'type':x.get('type'),'call_id':x.get('call_id')} for x in request['input']]},indent=2)+'\n')
                    passed=previous['expect'](raw,images(value))
                    if previous['id'].startswith('check'):
                        passed=passed and all(marker not in raw for marker in (str(PROJECT),str(destination),'S1D','rubric.json','Traceback'))
                    outcomes.append({'name':previous['id'],'passed':bool(passed),'text':raw[:16000],'image_blocks':images(value)})
                    if previous['id']=='check1':
                        # Force the actual evaluator's next three calls to fail
                        # on absent input. Reservations still consume the quota.
                        output.rename(fixture/'saved-output')
                item=copy.deepcopy(cases[index]['item']) if index<len(cases) else {
                    'type':'message','id':'done','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':'Scripted three-tool qualification complete. No real inference.','annotations':[]}]}
                response={'id':f'resp_{index}','object':'response','status':'completed','model':MODEL,'output':[item],
                          'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}}
                data=response_sse(response)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except Exception as error:
                handler_errors.append(type(error).__name__+': '+str(error))
                self.send_error(500)
    upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Scripted)
    gate=make_server(f'http://127.0.0.1:{upstream.server_port}',GateV3(audit=audit.append))
    broker=BrokerServerV3(time.monotonic()+150,destination/'broker-audit.jsonl',check_reports=destination/'checks',_service_factory=service_factory)
    for server in (upstream,gate):threading.Thread(target=server.serve_forever,daemon=True).start()
    for name in ('home','codex-home','cli-tmp'):(destination/name).mkdir()
    config=tomllib.loads((ROOT/'profile.toml').read_text())
    features={key:False for key in 'apps plugins remote_plugin hooks browser_use browser_use_external computer_use memories multi_agent multi_agent_v2 image_generation skill_search skill_mcp_dependency_install tool_suggest shell_snapshot api_key_model_discovery enable_request_compression standalone_web_search goals workspace_dependencies request_permissions_tool unbounded_connection_retries daemon_auto_start system_proxy_fallback respect_system_proxy'.split()}
    features.update(code_mode_host=True,skip_host_skill_discovery=True)
    config['features'].update(features)
    config.update(model=MODEL,model_reasoning_effort='xhigh',model_provider='deng_v3_local',cli_auth_credentials_store='file',analytics={'enabled':False},feedback={'enabled':False},check_for_update_on_startup=False,history={'persistence':'none'},otel={'exporter':'none'})
    names=['run_python','view_image','check_geometry']
    config['mcp_servers']={'deng_cad':{'command':str(PYTHON),'args':[str(V2/'mcp_relay.py')],'env':broker.environment(),'enabled_tools':names,'default_tools_approval_mode':'prompt','tools':{name:{'approval_mode':'approve'} for name in names},'required':True,'startup_timeout_sec':15,'tool_timeout_sec':150}}
    config['model_providers']={'deng_v3_local':{'name':'Scripted local qualification','base_url':f'http://127.0.0.1:{gate.server_port}','wire_api':'responses','requires_openai_auth':False,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0}}
    command=[str(CLI),'--no-daemon','-a','never','exec','--ignore-user-config','--ignore-rules','--strict-config','--skip-git-repo-check','--ephemeral','--json','-C',str(WORK)]
    for key,value in config.items():command.extend(['-c',key+'='+toml(value)])
    command.append('Synthetic fixed local qualification. No real model inference or Deng CAD task.')
    environment={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(destination/'home'),'CODEX_HOME':str(destination/'codex-home'),'LANG':'en_US.UTF-8','NO_PROXY':'127.0.0.1,localhost','TMPDIR':str(destination/'cli-tmp'),'PYTHONDONTWRITEBYTECODE':'1'}
    process=None
    try:
        with (destination/'cli.stdout').open('w') as stdout,(destination/'cli.stderr').open('w') as stderr:
            process=subprocess.Popen(command,env=environment,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
            try:code=process.wait(timeout=140)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait();code=process.returncode
        quota=broker.broker.checker.quota.inspect()
        after=hashes()
        passed=(code==0 and not handler_errors and len(outcomes)==len(cases) and all(x['passed'] for x in outcomes)
            and not any(x.get('event')=='denied' for x in audit) and len(quota['checks'])==4
            and before==after and all(x['model']==MODEL and x['effort']=='xhigh'
                and x['namespaces']==[('deng_cad',names)] and not x['authorization_present'] for x in requests))
        report={'passed':passed,'real_model_inference':False,'cad_trial_started':False,'cli_exit_code':code,
            'qualification_scope':'Actual CLI/gate/MCP/broker; first check uses real frozen evaluator on staged generic boxes. Three missing-input failures consume remaining checks; fifth denied.',
            'requests':requests,'probes':outcomes,'handler_errors':handler_errors,'gate_audit':audit,
            'quota':quota,'frozen_hashes_unchanged':before==after,'frozen_hashes':before,
            'python_image_worker_count':broker.broker.sequence,'check_call_count':broker.broker.check_calls}
        (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        (HERE/'latest-cli-qualification.txt').write_text(str(destination)+'\n')
        print(json.dumps({'passed':passed,'report':str(destination/'report.json'),'probes':len(outcomes),'handler_errors':handler_errors},indent=2),flush=True)
        return 0 if passed else 1
    finally:
        if process and process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
        for server in (gate,upstream):server.shutdown();server.server_close()
        broker.close()


if __name__=='__main__':raise SystemExit(main())
