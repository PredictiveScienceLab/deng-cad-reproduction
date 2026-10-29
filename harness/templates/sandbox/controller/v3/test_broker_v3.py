import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import broker_v3
from broker_v3 import BrokerV3, _public_feedback, serve_stream
from controller import CheckQuota
from execution_limits import execution_scope, isolated_wrapper, evaluation_wrapper, worker_wrapper
from tool_gate_v3 import GateV3
from tool_gate import GateError, MODEL


def feedback(number):
    return {'check_number':number, 'checks_remaining':4-number, 'integrity_status':'pass',
        'rebuild_status':'pass', 'integrity_checks':[{'name':'assembly_consistency','status':'pass'},
            {'name':'/protected/rubric-S1D1','status':'pass'}],
        'protected_path':'/protected/secret', 'rubric':{'S1D1':100}}


class DummyService:
    def __init__(self, workspace, reports, *, worker_lock, deadline):
        self.worker_lock = worker_lock
        self.quota = CheckQuota(Path(reports)/'quota.json', workspace)
        self.fail = False
        self.entered = threading.Event()
        self.executed = 0
    def check(self):
        with self.worker_lock:
            number = self.quota.reserve()
            self.entered.set()
            self.executed += 1
            if self.fail:
                self.quota._finish(number, 'error', 'RuntimeError')
                raise RuntimeError('/protected/rubric.json S1D1=100 secret traceback')
            self.quota._finish(number, 'completed')
            return feedback(number)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.broker = BrokerV3(time.monotonic()+60, check_reports=self.root/'checks', _service_factory=DummyService)
    def tearDown(self): self.temp.cleanup()
    def value(self, result): return json.loads(result['content'][0]['text'])

    def test_three_tools_and_empty_check_schema(self):
        self.assertEqual([item['name'] for item in broker_v3.TOOLS], ['run_python','view_image','check_geometry'])
        self.assertIs(self.broker.lock, self.broker.checker.worker_lock)
        with self.assertRaises(ValueError): self.broker.call('check_geometry', {'path':'/protected'})
        self.assertEqual(len(self.broker.checker.quota.inspect()['checks']),0)

    def test_feedback_whitelist_hides_paths_targets_and_unknown_names(self):
        result = self.broker.call('check_geometry', {})
        self.assertFalse(result['isError'])
        raw = json.dumps(result)
        for forbidden in ('/protected','S1D1','rubric','100'):
            self.assertNotIn(forbidden,raw)
        self.assertEqual(self.value(result)['integrity_checks'], [{'name':'assembly_consistency','status':'pass'}])

    def test_errors_consume_quota_without_protected_text(self):
        self.broker.checker.fail = True
        results = [self.broker.call('check_geometry', {}) for _ in range(5)]
        self.assertEqual([self.value(r)['code'] for r in results], ['checker_error']*4+['quota_exhausted'])
        self.assertEqual([self.value(r)['checks_remaining'] for r in results],[3,2,1,0,0])
        self.assertNotIn('/protected',json.dumps(results))
        self.assertNotIn('S1D1',json.dumps(results))
        self.assertEqual(self.broker.checker.executed,4)

    def test_concurrent_calls_cannot_exceed_four(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.broker.call('check_geometry',{}))) for _ in range(9)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3);self.assertFalse(thread.is_alive())
        self.assertEqual(sum(not r['isError'] for r in results),4)
        self.assertEqual(self.broker.checker.executed,4)

    def test_check_waits_for_python_worker_reaping(self):
        entered,release=threading.Event(),threading.Event()
        def worker(*args,**kwargs):
            entered.set();release.wait(3)
            return {'success':True,'launched':True,'worker_reaped':True,'status':'completed'}
        with patch('broker.run_worker',side_effect=worker):
            first=threading.Thread(target=lambda:self.broker.call('run_python',{'source':'pass'}))
            second=threading.Thread(target=lambda:self.broker.call('check_geometry',{}))
            first.start();self.assertTrue(entered.wait(1));second.start()
            self.assertFalse(self.broker.checker.entered.wait(.05))
            release.set();first.join(2);second.join(2)
        self.assertTrue(self.broker.checker.entered.is_set())

    def test_cancel_and_expired_deadline_do_not_reserve(self):
        self.broker.cancel_event.set()
        self.assertEqual(self.value(self.broker.call('check_geometry',{}))['code'],'deadline_reached')
        self.assertEqual(len(self.broker.checker.quota.inspect()['checks']),0)
        self.broker.cancel_event.clear();self.broker.trial_deadline=time.monotonic()-1
        self.assertEqual(self.value(self.broker.call('check_geometry',{}))['code'],'deadline_reached')

    def test_mcp_exception_is_generic(self):
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'anything'}}
        output=io.BytesIO()
        with patch.object(self.broker,'call',side_effect=RuntimeError('/protected/source.py')):
            serve_stream(self.broker,io.BytesIO((json.dumps(request)+'\n').encode()),output)
        self.assertNotIn('/protected',output.getvalue().decode())
        self.assertEqual(json.loads(output.getvalue())['error']['message'],'Broker request rejected.')

    def test_gate_allows_only_empty_geometry_check_arguments(self):
        gate=GateV3()
        response={'status':'completed','output':[{'type':'function_call','name':'check_geometry','namespace':'deng_cad','call_id':'x','arguments':'{}'}]}
        compiled=gate.response(response)['output'][0]['input']
        self.assertIn('mcp__deng_cad__check_geometry',compiled)
        for arguments in ('{"path":"x"}','[]','null'):
            response['output'][0].update(call_id='bad',arguments=arguments)
            with self.assertRaises(GateError):gate.response(response)

    def test_roundtrip_function_output_omits_custom_id_preserving_binding(self):
        gate=GateV3()
        response={'status':'completed','output':[{'type':'function_call','name':'run_python',
            'namespace':'deng_cad','call_id':'call_test','arguments':'{"source":"print(1)"}'}]}
        internal=gate.response(response)['output'][0]
        output=[{'type':'input_text','text':'Worker completed successfully'}]
        original={'model':MODEL,'reasoning':{'effort':'xhigh'},'stream':True,
            'tools':[{'type':'namespace','name':'functions','tools':[{'type':'custom','name':'exec'}]}],
            'input':[internal,{'type':'custom_tool_call_output','id':'ctco_generated',
                'call_id':'call_test','output':output}]}
        rewritten=gate.request(original)
        results=[item for item in rewritten['input'] if item.get('type')=='function_call_output']
        self.assertEqual(len(results),1)
        self.assertNotIn('id',results[0])
        self.assertEqual(results[0]['call_id'],'call_test')
        self.assertEqual(results[0]['output'],output)
        self.assertEqual(original['input'][1]['id'],'ctco_generated')


class LimitTests(unittest.TestCase):
    def test_deadline_clamps_but_does_not_extend_shorter_deadline(self):
        function=isolated_wrapper(lambda **kw:kw,'test')
        with execution_scope(100,threading.Event()) as scope:
            self.assertEqual(function(deadline=200)['deadline'],100)
            self.assertEqual(function(deadline=90)['deadline'],90)
            self.assertEqual(function(deadline=None)['deadline'],100)
        self.assertEqual(function(deadline=200)['deadline'],200)
        self.assertEqual(len(scope.clamps),3)

    def test_context_does_not_leak_to_other_threads(self):
        function=isolated_wrapper(lambda **kw:kw,'test');observed=[]
        with execution_scope(100,threading.Event()):
            thread=threading.Thread(target=lambda:observed.append(function(deadline=200)['deadline']))
            thread.start();thread.join()
        self.assertEqual(observed,[200])

    def test_evaluator_cap_is_105_seconds_only_in_scope(self):
        function=evaluation_wrapper(lambda *args,**kw:kw)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            (Path(directory)/'output').mkdir()
            with execution_scope(100,threading.Event()):
                self.assertEqual(function(directory,seconds=180)['seconds'],105)
                self.assertEqual(function(directory,seconds=20)['seconds'],20)
        self.assertEqual(function(seconds=180)['seconds'],180)

    def test_preflight_rejects_excess_empty_directories_before_evaluation(self):
        called=[]
        function=evaluation_wrapper(lambda *args,**kw:called.append(True))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            output=Path(directory)/'output';output.mkdir()
            for number in range(257):(output/str(number)).mkdir()
            with execution_scope(100,threading.Event()):
                with self.assertRaises(ValueError):function(directory,seconds=180)
        self.assertEqual(called,[])

    def test_supervisor_gets_cancellation_deadline_and_reaping_is_recorded(self):
        seen={};cancel=threading.Event()
        def fake(**kw):
            seen.update(kw);return {'launched':True,'worker_reaped':True,'status':'completed'}
        function=worker_wrapper(fake)
        with execution_scope(100,cancel) as scope:
            function(absolute_deadline_monotonic=200)
        self.assertIs(seen['cancel_event'],cancel)
        self.assertEqual(seen['absolute_deadline_monotonic'],100)
        self.assertTrue(scope.quiescent)

    def test_unreaped_or_exception_worker_cannot_prove_quiescence(self):
        for fake in (lambda **kw:{'launched':True,'worker_reaped':False}, lambda **kw:(_ for _ in ()).throw(RuntimeError('unknown'))):
            with execution_scope(100,threading.Event()) as scope:
                try:worker_wrapper(fake)(absolute_deadline_monotonic=200)
                except RuntimeError:pass
            self.assertFalse(scope.quiescent)


if __name__=='__main__':unittest.main(verbosity=2)
