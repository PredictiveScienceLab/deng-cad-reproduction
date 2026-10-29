"""Owner-controlled Responses transport with fixed deadline and credential isolation."""
from __future__ import annotations
import base64
import hashlib
import http.client
import http.server
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
import sys
V2=Path('@PROJECT@/sandbox/controller/v2')
sys.path.insert(0,str(V2))
from tool_gate import GateError,strict_loads,completed_response,response_sse,MODEL
MAX_REQUEST=128*1024*1024
MAX_RESPONSE=32*1024*1024

class CachedChatGPTAuth:
    """Read the existing login only in the trusted gateway; never copy it to a trial."""
    def __init__(self,path=None):
        selected=path or os.environ.get('DENG_CHATGPT_AUTH_FILE')
        if not selected:raise ValueError('Explicit private DENG_CHATGPT_AUTH_FILE is required for optional live transport')
        self.path=Path(selected).expanduser().resolve(strict=True)
        if any(self.path.is_relative_to(root) for root in (Path('@PROJECT@'),Path('@RUNTIME@'))):
            raise ValueError('Authentication must remain outside project and candidate-readable runtime roots')
    def headers(self,minimum_validity_seconds=60):
        data=json.loads(self.path.read_text())
        if data.get('auth_mode')!='chatgpt':raise ValueError('The qualified live path requires the existing ChatGPT login')
        tokens=data.get('tokens',{});token=tokens.get('access_token');account=tokens.get('account_id')
        if not isinstance(token,str) or not token or not isinstance(account,str) or not account:raise ValueError('Cached ChatGPT authentication unavailable')
        payload=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
        if payload.get('exp',0)-time.time()<minimum_validity_seconds:raise ValueError('Cached login expires before the fixed trial deadline; refresh authentication first')
        return {'Authorization':'Bearer '+token,'ChatGPT-Account-ID':account}

class OfficialTransport:
    def __init__(self,auth=None):self.auth=auth or CachedChatGPTAuth()
    def __call__(self,body,incoming_headers,deadline,cancel_event):
        if os.environ.get('DENG_ENABLE_LIVE')!='1':raise PermissionError('Live transport requires explicit --live authorization')
        if cancel_event.is_set() or time.monotonic()>=deadline:raise TimeoutError('Trial deadline reached before inference')
        headers={'Content-Type':'application/json','Accept':'text/event-stream','Originator':'codex_cli_rs','User-Agent':'codex_cli_rs/0.164.0'}
        for key in ('OpenAI-Beta','Version','Session_id','X-Codex-Turn-Metadata'):
            if key in incoming_headers:headers[key]=incoming_headers[key]
        credentials=self.auth.headers(max(60,deadline-time.monotonic()+60));headers.update(credentials)
        conn=http.client.HTTPSConnection('chatgpt.com',timeout=min(180,max(.1,deadline-time.monotonic())),context=ssl.create_default_context())
        done=threading.Event()
        def cancel_connection():
            while not done.wait(.05):
                if cancel_event.is_set() or time.monotonic()>=deadline:
                    sock=conn.sock
                    if sock:
                        try:sock.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                    conn.close();return
        watcher=threading.Thread(target=cancel_connection,daemon=True);watcher.start()
        try:
            conn.request('POST','/backend-api/codex/responses',body=body,headers=headers)
            response=conn.getresponse()
            data=response.read(MAX_RESPONSE+1)
            if cancel_event.is_set() or time.monotonic()>=deadline:raise TimeoutError('Inference crossed trial deadline')
            if response.status!=200:
                detail='Official provider HTTP '+str(response.status)
                try:
                    problem=json.loads(data);detail+=': '+str(problem.get('error',{}).get('message',''))[:1200]
                except Exception:pass
                for value in credentials.values():
                    detail=detail.replace(value,'[credential redacted]')
                    if value.startswith('Bearer '):detail=detail.replace(value[7:],'[credential redacted]')
                raise GateError(detail)
            return data
        finally:done.set();conn.close();watcher.join(timeout=.2)

class LiveGateway:
    def __init__(self,gate,*,deadline,audit_path,transport=None):
        self.gate=gate;self.deadline=deadline;self.audit_path=Path(audit_path);self.transport=transport or OfficialTransport()
        self.cancel_event=threading.Event();self.sequence=0;self.errors=[];self.usage=[];self.audit_lock=threading.Lock()
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.send_error(405)
            def do_POST(self):
                with owner.gate.lock:
                    try:
                        if self.path!='/responses' or self.client_address[0]!='127.0.0.1':raise GateError('Only loopback POST /responses is supported')
                        if owner.cancel_event.is_set() or time.monotonic()>=owner.deadline:raise TimeoutError('Trial deadline reached')
                        if self.headers.get('Content-Encoding','identity')!='identity':raise GateError('Compressed request rejected')
                        size=int(self.headers.get('Content-Length','0'))
                        if not 0<size<=MAX_REQUEST:raise GateError('Request body exceeds128MiB limit')
                        request=owner.gate.request(strict_loads(self.rfile.read(size)))
                        request['store']=False
                        request['input'].append({'type':'message','role':'developer','content':[{'type':'input_text','text':'Controller time remaining: '+str(max(0,round(owner.deadline-time.monotonic())))+' seconds. The original task and drawing requirements remain unchanged.'}]})
                        owner.sequence+=1;number=owner.sequence
                        owner.record({'event':'inference_started','number':number,'model':request['model'],'effort':request['reasoning']['effort'],'request_bytes':size,'input_items':len(request['input'])})
                        raw=owner.transport(json.dumps(request,allow_nan=False).encode(),dict(self.headers),owner.deadline,owner.cancel_event)
                        response=completed_response(raw)
                        owner.record({'event':'provider_completed','number':number,'returned_model':response.get('model'),'response_id':response.get('id'),'usage':response.get('usage'),'response_sha256':hashlib.sha256(raw).hexdigest()})
                        if owner.cancel_event.is_set() or time.monotonic()>=owner.deadline:raise TimeoutError('Late model response rejected')
                        transformed=owner.gate.response(response)
                        owner.usage.append(response.get('usage',{}))
                        data=response_sse(transformed);code=200;content_type='text/event-stream'
                    except Exception as exc:
                        error={'event':'gateway_rejected','type':type(exc).__name__,'reason':str(exc)[:1500]};owner.errors.append(error);owner.record(error)
                        data=json.dumps({'error':{'type':'qualified_gateway_rejection','message':'The controller rejected this exchange; no tool dispatch was forwarded.'}}).encode();code=502;content_type='application/json'
                    self.send_response(code);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers()
                    try:self.wfile.write(data)
                    except (BrokenPipeError,ConnectionResetError):pass
        class Server(http.server.ThreadingHTTPServer):daemon_threads=True
        self.server=Server(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base_url=f'http://127.0.0.1:{self.server.server_port}'
    def record(self,event):
        with self.audit_lock:
            with self.audit_path.open('a') as f:f.write(json.dumps(dict(event,unix_seconds=time.time()))+'\n')
    def close(self):
        self.cancel_event.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
