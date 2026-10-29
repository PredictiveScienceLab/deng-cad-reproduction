import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from check_service import QualityCheckService
from controller import QuotaExceeded

class CheckServiceTests(unittest.TestCase):
    def test_four_calls_persist_and_no_grading_targets_leak(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'work';work.mkdir();lock=threading.Lock()
            service=QualityCheckService(work,root/'reports',worker_lock=lock,deadline=time.monotonic()+60)
            fake={'analysis':{'integrity':{'status':'pass','checks':[{'name':'solid_count','status':'pass','private_measurement':815.240}]}},'gates':[{'id':'GATE-REBUILD','status':'pass'}],'score':{'secret':'HIDDEN_RUBRIC'}}
            with patch('check_service.evaluate',return_value=fake) as callback:
                for number in range(1,5):
                    result=service.check();self.assertEqual(result['check_number'],number)
                    self.assertNotIn('HIDDEN_RUBRIC',json.dumps(result));self.assertNotIn('815.24',json.dumps(result))
                restored=QualityCheckService(work,root/'reports',worker_lock=lock,deadline=time.monotonic()+60)
                with self.assertRaises(QuotaExceeded):restored.check()
                self.assertEqual(callback.call_count,4)
    def test_failure_consumes_reservation(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'work';work.mkdir()
            service=QualityCheckService(work,root/'reports',worker_lock=threading.Lock(),deadline=time.monotonic()+60)
            with patch('check_service.evaluate',side_effect=RuntimeError('synthetic failure')):
                with self.assertRaises(RuntimeError):service.check()
            self.assertEqual(len(service.quota.inspect()['checks']),1)
            self.assertEqual(service.quota.inspect()['checks'][0]['status'],'error')
    def test_expired_global_deadline_prevents_evaluation(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            root=Path(d);work=root/'work';work.mkdir()
            service=QualityCheckService(work,root/'reports',worker_lock=threading.Lock(),deadline=time.monotonic()-1)
            with patch('check_service.evaluate') as callback:
                with self.assertRaises(TimeoutError):service.check()
                callback.assert_not_called()

if __name__=='__main__':unittest.main()
