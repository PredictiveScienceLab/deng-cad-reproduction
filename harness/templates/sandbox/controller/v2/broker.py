#!/usr/bin/env python3
"""Two-tool stdio MCP broker; candidate Python runs under the no-fork policy.

This server has no model client and cannot start a CAD trial by itself.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import math

from worker_supervisor import run_worker

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path('@RUNTIME@')
PYTHON = RUNTIME / 'venv/bin/python'
WORK = ROOT / 'candidate/work'
INPUT = ROOT / 'candidate/input'
SCRATCH = ROOT / 'candidate/tmp'
POLICY = Path(__file__).with_name('worker.sb')
MAX_SOURCE = 262144
MAX_LINE = 1048576
BOOTSTRAP = ('import sys,os; data=sys.stdin.buffer.read(262145); '
             'assert len(data)<=262144,"Source exceeds limit"; '
             'sys.path.insert(0,os.getcwd()); '
             'exec(compile(data,"<cad-worker>","exec"),{"__name__":"__main__","__file__":"<cad-worker>"})')

TOOLS = [
    {'name':'run_python','description':'Run Python source using the installed CadQuery runtime. Read inputs at ../input. Save work in the current directory. No network or subprocess creation; threads are allowed. Use runpy.run_path to execute saved Python files. Builds, measurements, rendering, file reads and edits can all be done here. Each call is limited to 60 seconds and the external trial deadline.','inputSchema':{'type':'object','properties':{'source':{'type':'string'},'timeout_seconds':{'type':'number','minimum':1,'maximum':60}},'required':['source'],'additionalProperties':False}},
    {'name':'view_image','description':'View a PNG/JPEG image under the permitted input or work directories. Relative paths are resolved from the work directory.','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}
]

class Broker:
    def __init__(self, deadline, audit_path=None):
        if not isinstance(deadline,(int,float)) or not math.isfinite(deadline):
            raise ValueError('A fixed absolute controller deadline is required')
        self.deadline=deadline
        self.audit_path=Path(audit_path) if audit_path else None
        self.env=json.loads((ROOT/'control/candidate-environment.json').read_text())
        self.lock=threading.Lock()
        self.cancel_event=threading.Event()
        self.sequence=0

    def execute(self, source, timeout_seconds=60, output_limit_bytes=2*1024*1024):
        if not isinstance(source,str) or len(source.encode())>MAX_SOURCE or '\x00' in source:
            raise ValueError('Invalid/bounded Python source')
        if isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not 1<=timeout_seconds<=60:
            raise ValueError('timeout_seconds must be between 1 and 60')
        with self.lock:
            self.sequence+=1
            result=run_worker(['/usr/bin/sandbox-exec','-f',str(POLICY),str(PYTHON),'-I','-c',BOOTSTRAP],
                cwd=WORK, env=self.env, timeout_seconds=timeout_seconds,
                absolute_deadline_monotonic=self.deadline, stdin_bytes=source.encode(),
                output_limit_bytes=output_limit_bytes,cancel_event=self.cancel_event)
            if self.audit_path:
                record={k:v for k,v in result.items() if k not in ('stdout','stderr')}
                record.update(sequence=self.sequence, source_sha256=hashlib.sha256(source.encode()).hexdigest(), policy_sha256=hashlib.sha256(POLICY.read_bytes()).hexdigest())
                with self.audit_path.open('a') as stream: stream.write(json.dumps(record)+'\n')
            return result

    def call(self, name, arguments):
        if not isinstance(arguments,dict): raise ValueError('Arguments must be an object')
        if name=='run_python':
            if set(arguments)-{'source','timeout_seconds'} or 'source' not in arguments: raise ValueError('Invalid run_python arguments')
            result=self.execute(**arguments)
            return {'content':[{'type':'text','text':json.dumps(result)}], 'isError':not result.get('success',False)}
        if name=='view_image':
            if set(arguments)!={'path'} or not isinstance(arguments['path'],str) or len(arguments['path'])>4096 or '\x00' in arguments['path']:
                raise ValueError('Invalid view_image arguments')
            requested=Path(arguments['path'])
            lexical=requested if requested.is_absolute() else WORK/requested
            resolved=lexical.resolve()
            if not any(resolved.is_relative_to(root) for root in (WORK,INPUT,SCRATCH)):
                raise PermissionError('Image access denied: outside permitted input/work roots')
            code=('from PIL import Image\nimport io,base64,json\n'
                  f'im=Image.open({str(lexical)!r})\n'
                  'assert im.width*im.height<=16000000,"Image pixel limit exceeded"\n'
                  'im.load()\nim.thumbnail((2400,2400))\n'
                  'b=io.BytesIO()\nim.convert("RGB").save(b,format="PNG")\n'
                  'assert len(b.getvalue())<=6000000,"Image byte limit exceeded"\n'
                  'print(json.dumps({"data":base64.b64encode(b.getvalue()).decode(),"mimeType":"image/png"}))\n')
            result=self.execute(code,timeout_seconds=15,output_limit_bytes=9*1024*1024)
            if not result.get('success',False):
                return {'content':[{'type':'text','text':json.dumps(result)}],'isError':True}
            decoded=json.loads(result['stdout'])
            return {'content':[{'type':'image',**decoded}], 'isError':False}
        raise PermissionError('Tool denied: only run_python and view_image are enabled')

def serve_stream(broker, incoming, outgoing):
    while True:
        line=incoming.readline(MAX_LINE+1)
        if not line:return
        if len(line)>MAX_LINE:raise ValueError('MCP message exceeds bound')
        request=json.loads(line)
        identifier=request.get('id')
        method=request.get('method')
        if identifier is None:continue
        try:
            params=request.get('params') or {}
            if method=='initialize':
                result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'deng-cad-sandbox-broker','version':'2'}}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':result=broker.call(params.get('name'),params.get('arguments',{}))
            elif method=='ping':result={}
            else:raise PermissionError('Unsupported MCP method')
            response={'jsonrpc':'2.0','id':identifier,'result':result}
        except Exception as exc:
            response={'jsonrpc':'2.0','id':identifier,'error':{'code':-32602,'message':f'{type(exc).__name__}: {exc}'}}
        outgoing.write((json.dumps(response)+'\n').encode());outgoing.flush()

def main():
    raise SystemExit('Start the broker from the trusted controller; direct stdio launch is disabled so the CLI cannot own or kill worker supervision.')

if __name__=='__main__':main()
