"""Accelerated real Seatbelt deadline/cancel probes; no model or CAD trial."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time

from broker_v3 import BrokerV3, QualityCheckService, EVALUATOR
from execution_limits import execution_scope, install
from qualify_cli_v3 import hashes
import execution

HERE=Path(__file__).resolve().parent


def main():
    before=hashes();install()
    destination=HERE/('deadline-qualification-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    destination.mkdir()
    results=[]
    for mode in ('deadline','cancel','already_expired'):
        base=destination/mode;base.mkdir()
        for name in ('work','scratch','control'):(base/name).mkdir()
        cancel=threading.Event();started=time.monotonic()
        deadline=started+(.4 if mode=='deadline' else -1 if mode=='already_expired' else 5)
        timer=threading.Timer(.4,cancel.set) if mode=='cancel' else None
        if timer:timer.start()
        with execution_scope(deadline,cancel) as scope:
            # This is the same supervisor dynamically imported by the frozen
            # execution module; the context wrapper clamps it independently.
            result=execution.run_isolated('import os,signal,time\nos.setsid()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint("DIRECT_WORKER",flush=True)\ntime.sleep(10)',
                work=base/'work',scratch=base/'scratch',control=base/'control',seconds=60,deadline=started+60)
        if timer:timer.cancel();timer.join()
        elapsed=time.monotonic()-started
        expected={'deadline':'timed_out','cancel':'cancelled','already_expired':'deadline_expired'}[mode]
        passed=(result['status']==expected and scope.quiescent and elapsed<2.5)
        if mode=='already_expired':passed=passed and result['launched'] is False
        else:passed=passed and result['worker_reaped'] is True and 'SIGKILL' in result['cleanup_signals']
        results.append({'probe':mode,'passed':passed,'elapsed_seconds':elapsed,'worker':result,'clamps':scope.clamps})
    # Exercise the actual service's rebuild + later analysis, not only wrappers.
    fixture=destination/'checker-work';(fixture/'output').mkdir(parents=True)
    (fixture/'output/build.py').write_text('import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(10)\n')
    def factory(_workspace,reports,**kwargs):return QualityCheckService(fixture,reports,**kwargs)
    broker=BrokerV3(time.monotonic()+.6,check_reports=destination/'checks',_service_factory=factory)
    started=time.monotonic();response=broker.call('check_geometry',{});elapsed=time.monotonic()-started
    public=json.loads(response['content'][0]['text']);record=broker.last_check_execution
    states=[item['status'] for item in record['workers']]
    check_passed=(response['isError'] and public['code']=='deadline_reached' and record['quiescent']
        and states==['timed_out','deadline_expired'] and elapsed<3
        and len(broker.checker.quota.inspect()['checks'])==1
        and all(item['effective_deadline']<=broker.trial_deadline for item in record['clamps']))
    results.append({'probe':'real_checker_deadline','passed':check_passed,'elapsed_seconds':elapsed,
                    'public_response':public,'execution':record})
    after=hashes();passed=all(item['passed'] for item in results) and before==after
    report={'passed':passed,'real_model_inference':False,'cad_trial_started':False,
        'frozen_hashes_unchanged':before==after,'probes':results}
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    (HERE/'latest-deadline-qualification.txt').write_text(str(destination)+'\n')
    print(json.dumps({'passed':passed,'report':str(destination/'report.json'),
        'probes':[{'probe':item['probe'],'passed':item['passed']} for item in results]},indent=2))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
