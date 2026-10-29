"""Rerun the prior qualification suite through the replacement no-fork broker."""
from datetime import datetime,timezone
import hashlib
import json
import os
import pty
from pathlib import Path
import signal
import socket
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qualify import Qualification, ROOT, WORK, SCRATCH, INPUT, RUNTIME, PYTHON
from controller import CheckQuota, QuotaExceeded, snapshot_output
from broker import Broker, POLICY
from broker_server import BrokerServer

class WorkerQualification(Qualification):
    def __init__(self):
        super().__init__()
        self.broker=Broker(time.monotonic()+180,self.report/'worker-audit.jsonl')
    def python(self,code,timeout=30):
        r=self.broker.execute(code,timeout_seconds=min(timeout,60))
        return {**r,'timeout':r['status']=='timed_out','seconds':r['elapsed_seconds']}
    def run(self,argv,timeout=30):
        return self.python(f'import os;os.execv({str(argv[0])!r},{[str(x) for x in argv]!r})',timeout)
    def allowed(self,name,code,timeout=30):
        if name=='subprocess.policy_inherited':
            code='import subprocess\ntry:subprocess.run(["/usr/bin/true"])\nexcept PermissionError:print("CHILD_CREATION_DENIED")\nelse:raise RuntimeError("Child process unexpectedly allowed")'
        r=self.python(code,timeout)
        self.record(name,'operation succeeds (subprocess probe now requires child-creation denial)',r,r['success'])

def main():
    q=WorkerQualification()
    status=q.main()
    initial=json.loads((q.report/'os-results.json').read_text())
    initial.update(execution='v2 supervised no-fork worker',worker_policy_sha256=hashlib.sha256(POLICY.read_bytes()).hexdigest())
    (q.report/'os-results.json').write_text(json.dumps(initial,indent=2)+'\n')
    baseline_count=len(q.rows)

    quota=CheckQuota(q.report/'quota.json',WORK)
    callbacks=[]
    for index in range(4):quota.invoke(lambda n=index:callbacks.append(n))
    rejected=False
    try:quota.invoke(lambda:callbacks.append(5))
    except QuotaExceeded:rejected=True
    q.record('broker.fifth_call_rejected','4 callbacks; fifth rejected',{'callbacks':callbacks},rejected and callbacks==[0,1,2,3])
    q.denied('broker.ledger_read_denied',f'open({str(q.report / "quota.json")!r}).read()')
    q.denied('broker.ledger_write_denied',f'open({str(q.report / "quota.json")!r},"ab").write(b"tamper")')
    fixture=WORK/'v2-snapshot-fixture';fixture.mkdir(exist_ok=True)
    (fixture/'test.txt').write_text('SYNTHETIC')
    snapshot=q.report/'snapshot'
    snapshot_output(fixture,snapshot,candidate_workspace=WORK)
    q.denied('snapshot.read_denied',f'open({str(snapshot / "test.txt")!r}).read()')
    q.denied('snapshot.write_denied',f'open({str(snapshot / "test.txt")!r},"ab").write(b"tamper")')
    try:host_dns=bool(socket.getaddrinfo('example.com',443))
    except OSError:host_dns=False
    r=q.python('import socket,sys\ntry:socket.getaddrinfo("example.com",443)\nexcept OSError as e:print(type(e).__name__,e.errno);sys.exit(17)')
    q.record('network.system_dns_resolver','Host resolves; sandbox resolver blocked',{**r,'host_positive_control':host_dns},host_dns and r['returncode']==17)
    q.allowed('cadquery.png_render','''
import cadquery as cq
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PIL import Image
shape=cq.Workplane('XY').box(1,2,3).val()
vertices,triangles=shape.tessellate(.1)
points=[(v.x,v.y,v.z) for v in vertices]
fig=plt.figure(figsize=(4,4));ax=fig.add_subplot(projection='3d')
ax.add_collection3d(Poly3DCollection([[points[i] for i in t] for t in triangles],facecolors='skyblue',edgecolors='black'))
ax.set(xlim=(-2,2),ylim=(-2,2),zlim=(-2,2));fig.savefig('v2-qualification-box.png',dpi=100);plt.close(fig)
im=Image.open('v2-qualification-box.png');im.load();assert im.size==(400,400)
print('PNG_RENDER_OK')
''',60)
    # No child creation path may survive, even with session/group detachment flags.
    q.allowed('process.fork_denied','import os\ntry:p=os.fork()\nexcept PermissionError:print("DENIED")\nelse:\n if p==0:os._exit(0)\n os.waitpid(p,0)\n raise RuntimeError("fork allowed")')
    q.allowed('process.libc_fork_denied','import ctypes,os\nl=ctypes.CDLL(None,use_errno=True);l.fork.restype=ctypes.c_int;p=l.fork()\nif p==0:os._exit(0)\nif p>0:os.waitpid(p,0)\nassert p==-1 and ctypes.get_errno()==1;print("DENIED")')
    for mode in ('setsid','setpgroup'):
        option='setsid=True' if mode=='setsid' else 'setpgroup=0'
        q.allowed('process.posix_spawn_'+mode+'_denied',f'import os\ntry:p=os.posix_spawn("/usr/bin/true",["true"],os.environ,{option})\nexcept PermissionError:print("DENIED")\nelse:os.waitpid(p,0);raise RuntimeError("spawn allowed")')
    q.allowed('process.thread_allowed','import threading\na=[];t=threading.Thread(target=lambda:a.append(1));t.start();t.join();assert a==[1];print("THREAD_OK")')
    master,slave=pty.openpty()
    try:q.denied('device.unrelated_terminal_open',f'os.open({os.ttyname(slave)!r},os.O_RDWR)')
    finally:os.close(master);os.close(slave)
    q.denied('device.pseudo_terminal_creation','os.openpty()')
    r=q.python('import os,signal,time\nos.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint("SELF_DETACHED",flush=True);time.sleep(10)',1)
    q.record('deadline.setsid_sigterm_ignored','Direct PID killed and reaped',r,r['status']=='timed_out' and r['worker_reaped'] and r['returncode']==-signal.SIGKILL and 'SELF_DETACHED' in r['stdout'])
    replacement='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("EXEC_REPLACED",flush=True);time.sleep(10)'
    r=q.python(f'import os;os.execv({str(PYTHON)!r},{[str(PYTHON),"-I","-c",replacement]!r})',1)
    q.record('deadline.exec_replacement','Replaced executable retains supervised PID',r,r['status']=='timed_out' and r['worker_reaped'] and r['returncode']==-signal.SIGKILL and 'EXEC_REPLACED' in r['stdout'])
    expired=Broker(time.monotonic()-1).execute('raise RuntimeError("MUST_NOT_RUN")')
    q.record('deadline.expired_rejects_new_work','No launch after shared deadline',expired,not expired['launched'] and expired['status']=='deadline_expired')
    short=Broker(time.monotonic()+.4).execute('import time;time.sleep(10)',timeout_seconds=30)
    q.record('deadline.shared_budget_caps_worker','Global deadline wins over per-call budget',short,short['status']=='timed_out' and short['deadline_source']=='trial' and short['worker_reaped'] and short['elapsed_seconds']<2)
    # A disappearing model client/relay cannot kill the external owner of the worker.
    server=BrokerServer(time.monotonic()+5,q.report/'disconnect-audit.jsonl')
    marker=WORK/'v2-disconnect-pid.txt';marker.unlink(missing_ok=True)
    client=socket.socket(socket.AF_UNIX);client.connect(str(server.path));reader=client.makefile('rb');writer=client.makefile('wb')
    writer.write((json.dumps({'token':server.token})+'\n').encode());writer.flush();assert reader.readline()==b'OK\n'
    source=f'import os,signal,time\nos.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN)\nopen({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(10)'
    request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'run_python','arguments':{'source':source,'timeout_seconds':1}}}
    writer.write((json.dumps(request)+'\n').encode());writer.flush()
    deadline=time.monotonic()+2
    while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
    client.shutdown(socket.SHUT_RDWR);reader.close();writer.close();client.close()
    deadline=time.monotonic()+3
    while not (q.report/'disconnect-audit.jsonl').exists() and time.monotonic()<deadline:time.sleep(.01)
    server.close()
    worker_pid=int(marker.read_text()) if marker.exists() else None
    alive=False
    if worker_pid:
        try:os.kill(worker_pid,0);alive=True
        except ProcessLookupError:pass
    audit=[json.loads(line) for line in (q.report/'disconnect-audit.jsonl').read_text().splitlines()]
    q.record('deadline.relay_disconnect','External owner still kills and reaps at deadline',{'pid':worker_pid,'alive':alive,'audit':audit},bool(worker_pid) and not alive and audit[-1]['worker_reaped'] and audit[-1]['status']=='timed_out')
    for authenticated in (False,True):
        server=BrokerServer(time.monotonic()+120)
        client=socket.socket(socket.AF_UNIX);client.connect(str(server.path))
        if authenticated:
            client.sendall((json.dumps({'token':server.token})+'\n').encode());assert client.recv(3)==b'OK\n'
        else:client.sendall(b'{')
        start=time.monotonic();server.close();elapsed=time.monotonic()-start;client.close()
        q.record('broker.close_idle_'+('authenticated' if authenticated else 'partial_auth'),'Idle connection cannot block shutdown',{'elapsed_seconds':elapsed},elapsed<2)
    summary={'passed':all(x['passed'] for x in q.rows),'pass_count':sum(x['passed'] for x in q.rows),'total':len(q.rows),'baseline_count':baseline_count,'results':q.rows,'worker_policy_sha256':hashlib.sha256(POLICY.read_bytes()).hexdigest(),'cad_trial_started':False,'real_model_inference':False}
    (q.report/'v2-worker-results.json').write_text(json.dumps(summary,indent=2)+'\n')
    (ROOT/'qualification/latest-v2-workers.txt').write_text(str(q.report)+'\n')
    print(json.dumps({'report':str(q.report/'v2-worker-results.json'),'passed':summary['passed'],'pass_count':summary['pass_count'],'total':summary['total']},indent=2))
    return 0 if summary['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
