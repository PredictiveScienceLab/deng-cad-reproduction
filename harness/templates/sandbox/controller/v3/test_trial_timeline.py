import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, '@PROJECT@/sandbox/controller')
from controller import snapshot_output
from trial_timeline import TrialTimeline, TrialClosed, TrialNotStarted, bounded_snapshot_preflight


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def set(self, value): self.value = value


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='deng-timeline-', dir='/private/tmp')
        self.root = Path(self.tmp.name)
        self.work = self.root/'work'; self.output = self.work/'output'
        self.output.mkdir(parents=True)
        self.file = self.output/'part.txt'; self.file.write_text('initial')
        self.clock = Clock(); self.stops = []
        self.lock = threading.RLock(); self.cancel = threading.Event()
        self.timeline = self.make()

    def make(self, **kwargs):
        values = dict(started_at=100, checkpoint_seconds=35, total_seconds=60,
                      worker_lock=self.lock, cancel_event=self.cancel,
                      snapshot_root=self.root/'snapshots', snapshot=self.snapshot,
                      stop_callback=self.stops.append, clock=self.clock,
                      quiesce_timeout_seconds=.1)
        values.update(kwargs)
        return TrialTimeline(**values)

    def tearDown(self):
        # snapshot_output deliberately makes directories/files read-only.
        for p in self.root.rglob('*'):
            if p.is_dir(): p.chmod(0o700)
            elif p.is_file(): p.chmod(0o600)
        self.tmp.cleanup()

    def snapshot(self, destination):
        bounded_snapshot_preflight(self.output)
        return snapshot_output(self.output, destination, candidate_workspace=self.work)

    def contents(self, record):
        return (Path(record['snapshot']['path'])/'part.txt').read_text()

    def success(self, value, completed_at):
        with self.timeline.operation('run_python') as lease:
            self.file.write_text(value)
            self.clock.set(completed_at)
            lease.complete({'launched':True,'worker_reaped':True,'success':True})

    def test_in_time_operation_is_checkpoint(self):
        self.success('ready', 110)
        self.clock.set(135); self.timeline.poll()
        report=self.timeline.report()
        self.assertEqual(self.contents(report['checkpoint']), 'ready')
        self.assertFalse(self.cancel.is_set())
        self.assertIsNone(report['final'])

    def test_failed_but_reaped_in_time_worker_preserves_saved_files(self):
        with self.timeline.operation('run_python') as lease:
            self.file.write_text('saved-before-error'); self.clock.set(112)
            lease.complete({'launched':True,'worker_reaped':True,'success':False})
        report=self.timeline.finish()
        self.assertEqual(self.contents(report['final']),'saved-before-error')
        self.assertTrue(report['operations'][-1]['committed'])
        self.assertFalse(report['operations'][-1]['worker_success'])

    def test_crossing_worker_late_writes_are_excluded(self):
        self.success('accepted', 110)
        with self.timeline.operation('run_python') as lease:
            self.assertEqual(lease.deadline, 135)
            self.file.write_text('before-and-after-deadline')
            self.clock.set(136)
            lease.complete({'launched':True,'worker_reaped':True,'success':True})
        report=self.timeline.report()
        self.assertEqual(self.contents(report['checkpoint']), 'accepted')
        self.assertFalse(report['operations'][-1]['committed'])

    def test_second_phase_baseline_can_include_earlier_interrupted_work(self):
        with self.timeline.operation('run_python') as lease:
            self.file.write_text('partial35'); self.clock.set(136)
            lease.complete({'launched':True,'worker_reaped':True,'success':False})
        with self.timeline.operation('run_python') as lease:
            self.assertEqual(lease.deadline, 160)
            self.file.write_text('late60'); self.clock.set(161)
            lease.complete({'launched':True,'worker_reaped':True,'success':True})
        report=self.timeline.report()
        self.assertEqual(self.contents(report['checkpoint']), 'initial')
        self.assertEqual(self.contents(report['final']), 'partial35')
        self.assertTrue(self.cancel.is_set())

    def test_copy_after_deadline_may_represent_proven_earlier_state(self):
        normal=self.timeline.snapshot
        count=0
        def delayed(destination):
            nonlocal count
            count+=1
            result=normal(destination)
            if count==2: self.clock.set(137)
            return result
        self.timeline.snapshot=delayed
        self.success('finished134',134)
        checkpoint=self.timeline.report()['checkpoint']
        self.assertEqual(self.contents(checkpoint),'finished134')
        self.assertEqual(checkpoint['snapshot']['state_at_monotonic'],134)
        self.assertEqual(checkpoint['snapshot']['copy_completed_at_monotonic'],137)

    def test_no_worker_ever_initial_state_valid_at_checkpoint_and_stop(self):
        self.clock.set(160); self.timeline.poll()
        report=self.timeline.report()
        self.assertEqual(self.contents(report['checkpoint']),'initial')
        self.assertEqual(self.contents(report['final']),'initial')
        self.assertEqual(self.stops,['deadline'])

    def test_deadline_admission_denied_without_monitor(self):
        self.clock.set(161)
        with self.assertRaises(TrialClosed):
            with self.timeline.operation('run_python'):
                self.fail('Late operation was admitted')
        self.assertTrue(self.timeline.report()['final']['valid'])

    def test_baseline_copy_time_counts_and_does_not_reset_budget(self):
        normal=self.timeline.snapshot
        def slow(destination):
            result=normal(destination); self.clock.set(136); return result
        self.timeline.snapshot=slow
        with self.timeline.operation('check_geometry') as lease:
            self.assertEqual(lease.deadline,160)
            lease.complete(quiescent=True)
        self.assertEqual(self.timeline.deadline,160)
        self.assertEqual(self.contents(self.timeline.report()['checkpoint']),'initial')

    def test_baseline_copy_can_exhaust_budget_before_dispatch(self):
        normal=self.timeline.snapshot
        def slow(destination):
            result=normal(destination); self.clock.set(161); return result
        self.timeline.snapshot=slow
        with self.assertRaises(TrialClosed):
            with self.timeline.operation('run_python'):
                self.fail('Budget-exhausted operation ran')

    def test_early_finish_seals_both_results_without_waiting(self):
        self.success('finished',112); self.clock.set(113)
        report=self.timeline.finish()
        self.assertEqual(self.contents(report['final']),'finished')
        self.assertEqual(report['checkpoint']['reason'],'early_finish_sealed_result')
        self.assertEqual(report['terminal_at_monotonic'],113)
        self.assertEqual(self.stops,['agent_finished'])
        with self.assertRaises(TrialClosed):
            with self.timeline.operation('check_geometry'): pass

    def test_missing_completion_fails_closed(self):
        with self.timeline.operation('run_python'):
            self.file.write_text('unproven')
        report=self.timeline.report()
        self.assertEqual(report['terminal_reason'],'quiescence_failed')
        self.assertEqual(self.contents(report['final']),'initial')
        self.assertTrue(report['errors'])

    def test_failed_reaping_fails_closed(self):
        with self.timeline.operation('check_geometry') as lease:
            self.file.write_text('unproven'); lease.complete(quiescent=False)
        self.assertEqual(self.timeline.report()['terminal_reason'],'quiescence_failed')

    def test_snapshot_failure_closes_trial(self):
        def broken(destination): raise OSError('synthetic disk failure')
        self.timeline.snapshot=broken
        with self.assertRaises(OSError):
            with self.timeline.operation('run_python'): pass
        self.assertEqual(self.timeline.report()['terminal_reason'],'snapshot_failed')
        report=self.timeline.finish()
        self.assertFalse(report['final']['valid'])
        self.assertEqual(report['snapshot_count'],1)

    def test_early_finish_excludes_cancellation_grace_writes(self):
        entered=threading.Event(); release=threading.Event()
        def worker():
            with self.timeline.operation('run_python') as lease:
                self.file.write_text('inflight'); entered.set(); release.wait(1)
                self.clock.set(114); self.file.write_text('late-after-agent-exit')
                lease.complete({'launched':True,'worker_reaped':True,'success':True})
        thread=threading.Thread(target=worker); thread.start(); self.assertTrue(entered.wait(1))
        self.clock.set(113)
        def release_when_cancelled():
            self.cancel.wait(1); release.set()
        releaser=threading.Thread(target=release_when_cancelled); releaser.start()
        report=self.timeline.finish()
        thread.join(1); releaser.join(1)
        self.assertEqual(self.contents(report['final']),'initial')
        self.assertEqual(report['final']['cutoff_monotonic'],113)

    def test_future_start_is_not_admitted(self):
        self.clock.set(99)
        with self.assertRaises(TrialNotStarted):
            with self.timeline.operation('run_python'): pass

    def test_stop_callback_failure_is_reported(self):
        def fail(reason): raise OSError('synthetic stop failure')
        self.timeline.stop_callback=fail; self.clock.set(160); self.timeline.poll()
        self.assertEqual(self.timeline.report()['errors'][0]['where'],'stop_callback')

    def test_shared_reentrant_lock_supports_nested_broker_lock(self):
        with self.timeline.operation('check_geometry') as lease:
            with self.lock:
                self.file.write_text('check-quiesced')
            lease.complete(quiescent=True)
        self.timeline.finish()
        self.assertEqual(self.contents(self.timeline.report()['final']),'check-quiesced')

    def test_accelerated_monitor_cancels_crossing_worker_before_copy(self):
        start=time.monotonic(); events=[]
        timeline=self.make(started_at=start,checkpoint_seconds=.08,total_seconds=.22,
                           clock=time.monotonic,stop_callback=events.append,poll_seconds=.002)
        timeline.start()
        with timeline.operation('run_python') as lease:
            self.file.write_text('inflight')
            self.assertTrue(self.cancel.wait(.2))
            # Simulate writes in the cancellation/reap grace interval.
            self.file.write_text('late-after-cancel')
            time.sleep(.01)
            lease.complete({'launched':True,'worker_reaped':True,'success':False})
        self.assertEqual(self.contents(timeline.report()['checkpoint']),'initial')
        self.assertFalse(self.cancel.is_set())
        with timeline.operation('check_geometry') as lease:
            self.assertTrue(self.cancel.wait(.3))
            lease.complete(quiescent=True)
        report=timeline.finish()
        self.assertEqual(report['terminal_reason'],'deadline')
        self.assertEqual(events,['deadline'])
        self.assertEqual(self.contents(report['final']),'late-after-cancel')
        self.assertFalse(report['errors'])

    def test_quiescence_timeout_uses_baseline_and_does_not_copy_live_output(self):
        entered=threading.Event(); release=threading.Event()
        def worker():
            with self.timeline.operation('run_python') as lease:
                self.file.write_text('live-unreaped'); entered.set(); release.wait(1)
                lease.complete(quiescent=True)
        thread=threading.Thread(target=worker); thread.start(); self.assertTrue(entered.wait(1))
        self.clock.set(135); self.timeline.poll()
        report=self.timeline.report()
        self.assertEqual(report['terminal_reason'],'quiescence_failed')
        self.assertEqual(self.contents(report['final']),'initial')
        release.set(); thread.join(1); self.assertFalse(thread.is_alive())

    def test_bounded_preflight_counts_files_and_directories(self):
        (self.output/'nested').mkdir(); (self.output/'nested'/'file.txt').write_text('abc')
        result=bounded_snapshot_preflight(self.output)
        self.assertEqual(result,{'files':2,'directories':1,'depth':1,'bytes':10})

    def test_bounded_preflight_rejects_excess_empty_directories(self):
        for index in range(257): (self.output/f'dir-{index:03d}').mkdir()
        with self.assertRaisesRegex(ValueError,'directory count/depth'):
            bounded_snapshot_preflight(self.output)

    def test_bounded_preflight_rejects_excess_depth(self):
        parent=self.output
        for _ in range(33):
            parent=parent/'d'; parent.mkdir()
        with self.assertRaisesRegex(ValueError,'directory count/depth'):
            bounded_snapshot_preflight(self.output)

    def test_bounded_preflight_preserves_file_and_byte_limits(self):
        for limits in ({'max_files':0},{'max_file_bytes':6},{'max_total_bytes':6}):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError,'file count/byte'):
                bounded_snapshot_preflight(self.output,**limits)

    def test_bounded_preflight_rejects_symlink_and_fifo(self):
        link=self.output/'linked'; link.symlink_to(self.file)
        with self.assertRaisesRegex(ValueError,'symlink or special'):
            bounded_snapshot_preflight(self.output)
        link.unlink()
        fifo=self.output/'pipe'; os.mkfifo(fifo)
        try:
            with self.assertRaisesRegex(ValueError,'symlink or special'):
                bounded_snapshot_preflight(self.output)
        finally:
            fifo.unlink()

    def test_bounded_preflight_rejects_symlink_root(self):
        link=self.work/'alias'; link.symlink_to(self.output)
        with self.assertRaisesRegex(ValueError,'ordinary directory'):
            bounded_snapshot_preflight(link)
        link.unlink()


if __name__=='__main__':
    unittest.main(verbosity=2)
