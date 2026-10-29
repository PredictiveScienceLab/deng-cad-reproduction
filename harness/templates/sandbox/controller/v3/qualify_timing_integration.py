"""Accelerated real no-fork worker + three-tool broker + immutable cutoff proof."""
from datetime import datetime,timezone
import json,sys,time,threading
from pathlib import Path
from broker_v3 import BrokerV3,WORK,PROJECT
from trial_timeline import TrialTimeline,bounded_snapshot_preflight
from controller import snapshot_output

def main():
    root=PROJECT/'sandbox/qualification'/('v3-timing-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'));root.mkdir()
    (WORK/'output').mkdir(exist_ok=True)
    target=WORK/'output/timing-probe.txt';target.write_text('initial')
    started=time.monotonic();stops=[]
    broker=BrokerV3(started+6,root/'broker.jsonl',check_reports=root/'checks')
    def snapshot(destination):
        bounded_snapshot_preflight(WORK/'output')
        return snapshot_output(WORK/'output',destination,candidate_workspace=WORK)
    timeline=TrialTimeline(started_at=started,worker_lock=broker.lock,cancel_event=broker.cancel_event,snapshot_root=root/'snapshots',snapshot=snapshot,checkpoint_seconds=2,total_seconds=6,stop_callback=stops.append).start()
    broker.set_timeline(timeline)
    first=broker.execute('from pathlib import Path\nPath("output/timing-probe.txt").write_text("before")',timeout_seconds=1)
    crossing=broker.execute('import os,signal,time\nfrom pathlib import Path\nos.setsid()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath("output/timing-probe.txt").write_text("crossing")\ntime.sleep(10)',timeout_seconds=5)
    timeline.poll()
    third=broker.execute('from pathlib import Path\nPath("output/timing-probe.txt").write_text("after")',timeout_seconds=1)
    report=timeline.finish()
    checkpoint=Path(report['checkpoint']['snapshot']['path']);final=Path(report['final']['snapshot']['path'])
    probes={'first_worker_reaped':first['worker_reaped'],'crossing_worker_reaped':crossing['worker_reaped'],'crossing_worker_stopped':crossing['status'] in {'timed_out','cancelled'},'checkpoint_excludes_crossing_write':(checkpoint/'timing-probe.txt').read_text()=='before','continued_after_checkpoint':third['success'],'final_contains_later_work':(final/'timing-probe.txt').read_text()=='after','same_fixed_deadline':report['deadline_monotonic']==started+6,'no_timeline_errors':not report['errors'],'stop_callback_fired':bool(stops)}
    result={'passed':all(probes.values()),'probes':probes,'timeline':report,'worker_results':[first,crossing,third],'real_model_inference':False,'cad_trial_started':False}
    (root/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    Path(__file__).with_name('latest-timing-integration.txt').write_text(str(root)+'\n')
    print(json.dumps({'passed':result['passed'],'probes':probes,'report':str(root/'summary.json')}));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
