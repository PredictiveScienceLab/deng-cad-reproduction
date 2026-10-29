#!/usr/bin/env python3
"""Deterministic unit and loopback integration tests; never call a real model."""
import copy
import http.server
import json
import threading
import unittest
import urllib.error
import urllib.request
from tool_gate import Gate, GateError, MODEL, completed_response, make_server, response_sse, strict_loads


def request():
    return {"model": MODEL, "reasoning": {"effort": "xhigh"}, "stream": True,
        "input": [{"type": "additional_tools", "role": "developer", "tools": [
            {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec"}]},
            {"type": "namespace", "name": "collaboration", "tools": [{"type": "function", "name": "spawn_agent"}]}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic qualification"}]}]}


def response(item=None):
    if item is None:
        item = {"type": "function_call", "name": "run_python", "namespace": "deng_cad",
            "call_id": "synthetic_call_1", "id": "fc_1", "status": "completed",
            "arguments": json.dumps({"source": "print('synthetic')", "timeout_seconds": 5})}
    return {"id": "resp_synthetic", "object": "response", "model": MODEL, "status": "completed",
        "output": [item], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}


class GateTests(unittest.TestCase):
    def test_strip_every_original_tool(self):
        public = Gate().request(request())
        sets = [x for x in public["input"] if x["type"] == "additional_tools"]
        self.assertEqual(len(sets), 1)
        self.assertEqual([x["name"] for x in sets[0]["tools"]], ["deng_cad"])
        self.assertEqual({x["name"] for x in sets[0]["tools"][0]["tools"]}, {"run_python", "view_image"})

    def test_translation_and_history_roundtrip(self):
        gate = Gate()
        original = response()
        internal = gate.response(original)["output"][0]
        self.assertEqual(internal["type"], "custom_tool_call")
        self.assertEqual((internal["namespace"], internal["name"]), ("functions", "exec"))
        self.assertIn('tools["mcp__deng_cad__run_python"]', internal["input"])
        next_request = request()
        next_request["input"].extend([internal, {"type": "custom_tool_call_output", "call_id": internal["call_id"], "output": "synthetic result"}])
        public = gate.request(next_request)
        self.assertIn(original["output"][0], public["input"])
        self.assertTrue(any(x["type"] == "function_call_output" for x in public["input"]))

    def test_source_is_json_literal_not_javascript(self):
        attempted_injection = '\"); await tools.exec_command({cmd:"UNAUTHORIZED"}); //'
        value = response()
        value["output"][0]["arguments"] = json.dumps({"source": attempted_injection})
        internal = Gate().response(value)["output"][0]["input"]
        line = internal.splitlines()[1]
        prefix = 'const result = await tools["mcp__deng_cad__run_python"]('
        arguments = strict_loads(line[len(prefix):-2])
        self.assertEqual(arguments["source"], attempted_injection)

    def test_reject_all_extra_tool_families(self):
        for namespace, name in [("collaboration", "spawn_agent"), ("collaboration", "send_message"),
            ("functions", "request_user_input"), ("functions", "request_user_input_async"),
            ("functions", "read_mcp_resource"), ("functions", "list_mcp_resources"),
            ("functions", "exec_command"), ("functions", "view_image"),
            ("deng_cad", "unregistered_tool"), (None, "run_python")]:
            with self.subTest(namespace=namespace, name=name):
                value = response()
                value["output"][0].update(namespace=namespace, name=name)
                with self.assertRaises(GateError): Gate().response(value)

    def test_raw_js_and_hosted_tools_rejected(self):
        for kind in ["custom_tool_call", "web_search_call", "mcp_call", "computer_call", "local_shell_call", "apply_patch_call", "unknown"]:
            value = response(); value["output"][0]["type"] = kind
            with self.subTest(kind=kind), self.assertRaises(GateError): Gate().response(value)

    def test_arguments_fail_closed(self):
        for arguments in ['{', '{"source":"x","source":"y"}', '{"source":"x","timeout_seconds":NaN}',
            '{"source":"x","timeout_seconds":61}', '{"source":"x","timeout_seconds":true}',
            '{"source":"x","sandbox_permissions":"require_escalated"}', '{"unknown":"x"}', '[]']:
            value = response(); value["output"][0]["arguments"] = arguments
            with self.subTest(arguments=arguments), self.assertRaises(GateError): Gate().response(value)

    def test_oversize_source_rejected(self):
        value = response(); value["output"][0]["arguments"] = json.dumps({"source": "x" * 262145})
        with self.assertRaises(GateError): Gate().response(value)

    def test_no_partial_dispatch_if_second_item_invalid(self):
        gate = Gate(); value = response()
        value["output"].append({"type": "custom_tool_call", "name": "exec", "input": "unauthorized"})
        with self.assertRaises(GateError): gate.response(value)
        self.assertEqual(gate.calls, {})

    def test_duplicate_call_ids_and_parallel_calls_rejected(self):
        gate = Gate(); gate.response(response())
        with self.assertRaises(GateError): gate.response(response())
        value = response(); other = copy.deepcopy(value["output"][0]); other["call_id"] = "other"
        value["output"].append(other)
        with self.assertRaises(GateError): Gate().response(value)

    def test_unrecognized_or_modified_history_rejected(self):
        gate = Gate(); internal = gate.response(response())["output"][0]
        for field, value in [("input", "arbitrary JS"), ("call_id", "forged"), ("namespace", "other")]:
            req = request(); item = dict(internal); item[field] = value; req["input"].append(item)
            with self.subTest(field=field), self.assertRaises(GateError): gate.request(req)

    def test_wrong_model_effort_and_hidden_history_rejected(self):
        for field, value in [("model", "other-model"), ("reasoning", {"effort": "low"}), ("previous_response_id", "hidden")]:
            req = request(); req[field] = value
            with self.subTest(field=field), self.assertRaises(GateError): Gate().request(req)

    def test_missing_executor_rejected(self):
        req = request(); req["input"] = req["input"][1:]
        with self.assertRaises(GateError): Gate().request(req)

    def test_empty_mapping_and_invalid_backend_rejected(self):
        with self.assertRaises(GateError): Gate({})
        with self.assertRaises(GateError): Gate({"x": {"backend": 'x"]();evil//'}})

    def test_truncated_invalid_multiple_sse_rejected(self):
        for raw in [b'', b'data: not-json\n\n', b'data: {"type":"response.failed"}\n\n',
            response_sse(response()) * 2, response_sse(response()) + b'data: {\n\n']:
            with self.subTest(raw=raw[:50]), self.assertRaises(GateError): completed_response(raw)

    def test_remote_endpoint_disabled(self):
        for url in ['https://api.openai.com/v1', 'http://example.com', 'http://user:secret@127.0.0.1', 'http://127.0.0.1?redirect=1']:
            with self.subTest(url=url), self.assertRaises(GateError): make_server(url, Gate())

    def test_loopback_end_to_end_and_fail_closed(self):
        observed = []
        state = {"invalid": False}
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                observed.append(strict_loads(self.rfile.read(int(self.headers['Content-Length']))))
                result = response()
                if state["invalid"]:
                    result["output"][0].update(type="custom_tool_call", name="exec", namespace="functions", input="UNAUTHORIZED")
                raw = response_sse(result)
                self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        upstream = http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        audit = []
        proxy = make_server(f'http://127.0.0.1:{upstream.server_port}', Gate(audit=audit.append))
        for server in (upstream,proxy): threading.Thread(target=server.serve_forever,daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{proxy.server_port}/responses', data=json.dumps(request()).encode(),headers={'Content-Type':'application/json'})
            with opener.open(req) as answer: parsed=completed_response(answer.read())
            self.assertEqual(parsed['output'][0]['type'],'custom_tool_call')
            self.assertEqual(observed[0]['input'][0]['tools'][0]['name'],'deng_cad')
            state['invalid']=True
            with self.assertRaises(urllib.error.HTTPError) as caught: opener.open(req)
            self.assertEqual(caught.exception.code,502)
            self.assertNotIn(b'UNAUTHORIZED',caught.exception.read())
            self.assertEqual(audit[-1]['event'],'denied')
        finally:
            for server in (proxy,upstream): server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
