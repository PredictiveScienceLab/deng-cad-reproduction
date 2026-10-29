import base64,http.client,json,os,tempfile,threading,time,unittest
from unittest import mock
from pathlib import Path
from live_gateway import LiveGateway,CachedChatGPTAuth,OfficialTransport
from tool_gate import Gate,response_sse

def request():
    return {'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh'},'stream':True,'input':[{'type':'message','role':'user','content':[{'type':'input_text','text':'Transport fixture'}]}],'tools':[{'type':'namespace','name':'functions','tools':[{'type':'custom','name':'exec'}]}]}
def answer(model='gpt-6.1-sol',output=None):
    return response_sse({'id':'synthetic','status':'completed','model':model,'output':output if output is not None else [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'FIXTURE_OK'}]}],'usage':{'input_tokens':0,'output_tokens':0}})
class GatewayTests(unittest.TestCase):
    def test_transport_without_live_authorization_never_reads_auth_or_connects(self):
        auth=mock.Mock()
        with mock.patch.dict(os.environ,{},clear=True),mock.patch('http.client.HTTPSConnection') as connection:
            with self.assertRaises(PermissionError):
                OfficialTransport(auth)(b'{}',{},time.monotonic()+10,threading.Event())
            auth.headers.assert_not_called();connection.assert_not_called()
    def test_auth_path_is_explicit(self):
        with mock.patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):CachedChatGPTAuth()
    def test_auth_path_cannot_be_candidate_readable(self):
        for root in ('@PROJECT@','@RUNTIME@'):
            with self.assertRaises(ValueError):CachedChatGPTAuth(Path(root))
    def test_auth_lifetime_uses_remaining_fixed_budget(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            payload=base64.urlsafe_b64encode(json.dumps({'exp':time.time()+200}).encode()).decode().rstrip('=')
            path=Path(d)/'synthetic-auth.json';path.write_text(json.dumps({'auth_mode':'chatgpt','tokens':{'access_token':'synthetic.'+payload+'.signature','account_id':'synthetic-account'}}))
            auth=CachedChatGPTAuth(path)
            self.assertIn('Authorization',auth.headers(100))
            with self.assertRaises(ValueError):auth.headers(300)
    def exchange(self,payload,transport,seconds=10):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as d:
            gateway=LiveGateway(Gate(),deadline=time.monotonic()+seconds,audit_path=Path(d)/'audit.jsonl',transport=transport)
            try:
                conn=http.client.HTTPConnection('127.0.0.1',gateway.server.server_port,timeout=10)
                conn.request('POST','/responses',body=json.dumps(payload),headers={'Content-Type':'application/json'})
                response=conn.getresponse();raw=response.read();return response.status,raw,gateway.sequence,list(gateway.errors)
            finally:gateway.close()
    def test_exact_model_and_effort_pass(self):
        observed=[]
        def transport(body,*_):observed.append(json.loads(body));return answer()
        code,raw,count,errors=self.exchange(request(),transport)
        self.assertEqual(code,200);self.assertEqual(count,1);self.assertFalse(errors)
        self.assertFalse(observed[0]['store']);self.assertIn(b'FIXTURE_OK',raw)
    def test_wrong_model_never_contacts_transport(self):
        q=request();q['model']='different'
        code,_,count,_=self.exchange(q,lambda *_:self.fail('Should not contact upstream'))
        self.assertEqual(code,502);self.assertEqual(count,0)
    def test_wrong_provider_model_is_not_dispatched(self):
        code,raw,_,_=self.exchange(request(),lambda *_:answer('different'))
        self.assertEqual(code,502);self.assertNotIn(b'FIXTURE_OK',raw)
    def test_late_response_is_not_dispatched(self):
        def delayed(*_):time.sleep(.12);return answer()
        code,raw,_,_=self.exchange(request(),delayed,.06)
        self.assertEqual(code,502);self.assertNotIn(b'FIXTURE_OK',raw)
    def test_forbidden_tool_does_not_cross_gate(self):
        forbidden=[{'type':'function_call','namespace':'collaboration','name':'spawn_agent','call_id':'forbidden','arguments':'{}'}]
        code,raw,_,_=self.exchange(request(),lambda *_:answer(output=forbidden))
        self.assertEqual(code,502);self.assertNotIn(b'spawn_agent',raw)
    def test_history_over_original_32mib_bound(self):
        q=request();q['input'][0]['content'].append({'type':'input_image','image_url':'data:image/png;base64,'+'A'*(33*1024*1024)})
        code,_,_,_=self.exchange(q,lambda *_:answer())
        self.assertEqual(code,200)
if __name__=='__main__':unittest.main()
