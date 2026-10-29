#!/usr/bin/env python3
"""Fail-closed model transport adapter for the Deng CAD broker.

Expose two JSON tools, never raw JavaScript. Buffer and validate the entire
upstream response, then compile accepted calls to fixed Code Mode scripts.
No model endpoint is contacted by importing or testing this module.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BODY = 32 * 1024 * 1024
MAX_SOURCE = 256 * 1024
MODEL = "gpt-6.1-sol"
NAMESPACE = "deng_cad"
DEFAULT_TOOLS = {
    "run_python": {
        "backend": "mcp__deng_cad__run_python",
        "description": "Run Python source using the isolated CAD runtime. Files outside the supplied input and work areas, networking, and process creation are denied. Output is bounded and the controller enforces the timeout.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"source": {"type": "string", "maxLength": MAX_SOURCE},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60}},
            "required": ["source"]},
    },
    "view_image": {
        "backend": "mcp__deng_cad__view_image",
        "description": "Read an image from the permitted input/work areas through the isolated broker.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "maxLength": 4096}}, "required": ["path"]},
    },
}


class GateError(ValueError):
    pass


def strict_loads(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise GateError("Duplicate JSON object key")
            out[key] = value
        return out
    def bad_number(value):
        raise GateError("Non-finite JSON number")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_number)
    except (ValueError, TypeError) as exc:
        raise GateError("Invalid JSON: " + str(exc)) from exc


def validate(value, schema):
    """Small strict validator for owner-provided scalar/object broker schemas."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise GateError("Arguments must be an object")
        properties = schema.get("properties", {})
        if set(value) - set(properties):
            raise GateError("Unknown argument")
        if set(schema.get("required", [])) - set(value):
            raise GateError("Required argument missing")
        for key, item in value.items():
            validate(item, properties[key])
    elif kind == "string":
        if not isinstance(value, str) or len(value.encode()) > schema.get("maxLength", MAX_SOURCE) or "\x00" in value:
            raise GateError("Invalid/bounded string argument")
    elif kind in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GateError("Invalid numeric argument")
        if kind == "integer" and not isinstance(value, int):
            raise GateError("Integer argument required")
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise GateError("Numeric argument outside bounds")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise GateError("Boolean argument required")
    else:
        raise GateError("Unsupported owner schema type")
    if "enum" in schema and value not in schema["enum"]:
        raise GateError("Argument outside enum")


def compiled_script(backend, arguments):
    """Only owner-controlled source plus JSON literals enters the Code Mode VM."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", backend):
        raise GateError("Invalid backend identifier")
    return ('// @exec: {"yield_time_ms": 120000, "max_output_tokens": 10000}\n'
        + 'const result = await tools[' + json.dumps(backend) + ']('
        + json.dumps(arguments, ensure_ascii=True, allow_nan=False) + ');\n'
        + r'''let images = 0;
if (result && Array.isArray(result.content)) {
  for (const part of result.content) {
    if (part.type === "image") { image(part); images++; }
  }
}
function clean(value) {
  if (typeof value === "string" && value.startsWith("data:")) return "[image payload]";
  if (Array.isArray(value)) return value.map(clean);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = key === "data" && value.type === "image" ? "[image payload]" : clean(item);
    }
    return out;
  }
  return value;
}
text(JSON.stringify({result: clean(result), images}));
''')


class Gate:
    def __init__(self, tools=None, audit=None):
        self.tools = copy.deepcopy(DEFAULT_TOOLS if tools is None else tools)
        self.audit = audit or (lambda event: None)
        self.calls = {}
        self.lock = threading.Lock()
        for name, spec in self.tools.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise GateError("Invalid public tool name")
            compiled_script(spec["backend"], {})
        if not self.tools:
            raise GateError("Empty tool mapping")

    def definitions(self):
        return [{"type": "namespace", "name": NAMESPACE,
            "description": "The isolated CAD broker; these are the complete permitted tools.",
            "tools": [{"type": "function", "name": name, "description": spec["description"],
                "parameters": spec["parameters"], "strict": False} for name, spec in self.tools.items()]}]

    def request(self, original):
        if not isinstance(original, dict) or original.get("model") != MODEL or original.get("reasoning", {}).get("effort") != "xhigh":
            raise GateError("Only the requested model and effort are allowed")
        if original.get("stream") is not True or not isinstance(original.get("input"), list):
            raise GateError("Expected streaming Responses input list")
        if original.get("previous_response_id"):
            raise GateError("Server-side hidden history is not permitted")
        # The core must offer the Code Mode executor that the gate targets.
        offered = list(original.get("tools", []))
        for item in original["input"]:
            if item.get("type") == "additional_tools":
                offered.extend(item.get("tools", []))
        outer_exec = any(t.get("name") == "functions" and t.get("type") == "namespace"
            and any(x.get("name") == "exec" and x.get("type") == "custom" for x in t.get("tools", [])) for t in offered)
        if not outer_exec:
            raise GateError("Expected Code Mode executor is unavailable")
        out = copy.deepcopy(original)
        out.pop("tools", None)
        rewritten = []
        for item in out["input"]:
            kind = item.get("type")
            if kind == "additional_tools":
                continue
            if kind == "custom_tool_call":
                saved = self.calls.get(item.get("call_id"))
                if saved is None or any(item.get(k) != saved["internal"].get(k) for k in ("name", "namespace", "input", "call_id")):
                    raise GateError("Unrecognized/modified tool call in CLI history")
                item = copy.deepcopy(saved["external"])
            elif kind == "custom_tool_call_output":
                if item.get("call_id") not in self.calls:
                    raise GateError("Unrecognized tool output in CLI history")
                item["type"] = "function_call_output"
            elif kind in {"function_call", "function_call_output"}:
                raise GateError("Unmediated function call in CLI history")
            elif kind not in {"message", "reasoning", "compaction"}:
                raise GateError("Unknown input item type: " + str(kind))
            rewritten.append(item)
        rewritten.insert(0, {"type": "additional_tools", "role": "developer", "tools": self.definitions()})
        rewritten.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text":
            "The harness exposes only the deng_cad JSON tools listed above. Use run_python for CAD construction and file work, and view_image to inspect drawings/renders. Every other tool, delegation, and raw JavaScript tool call is disabled by the controller."}]})
        out["input"] = rewritten
        out["parallel_tool_calls"] = False
        out["tool_choice"] = "auto"
        self.audit({"event": "request", "model": MODEL, "effort": "xhigh", "public_tools": sorted(self.tools)})
        return out

    def response(self, response):
        if not isinstance(response, dict) or response.get("status") != "completed" or not isinstance(response.get("output"), list):
            raise GateError("Only a complete response may cross the gate")
        if response.get("model", MODEL) != MODEL:
            raise GateError("Upstream model changed")
        out = copy.deepcopy(response)
        new = []
        tool_count = 0
        # Validate every item before recording or returning any item.
        for item in out["output"]:
            kind = item.get("type")
            if kind == "function_call":
                tool_count += 1
                name = item.get("name")
                if item.get("namespace") != NAMESPACE or name not in self.tools or item.get("async"):
                    raise GateError("Tool call is not allowlisted")
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id or len(call_id) > 256 or call_id in self.calls:
                    raise GateError("Invalid/reused call id")
                arguments = strict_loads(item.get("arguments"))
                validate(arguments, self.tools[name]["parameters"])
                internal = {"type": "custom_tool_call", "id": item.get("id", "gate_" + call_id),
                    "status": "completed", "call_id": call_id, "namespace": "functions", "name": "exec",
                    "input": compiled_script(self.tools[name]["backend"], arguments)}
                new.append((call_id, copy.deepcopy(item), internal))
            elif kind == "message":
                if item.get("role") != "assistant" or not isinstance(item.get("content"), list) or any(
                    c.get("type") not in {"output_text", "refusal"} for c in item["content"]):
                    raise GateError("Unknown assistant content")
            elif kind == "reasoning":
                pass
            else:
                raise GateError("Output item type is not allowlisted: " + str(kind))
        if tool_count > 1:
            raise GateError("Parallel tool calls are disabled")
        for call_id, external, internal in new:
            self.calls[call_id] = {"external": external, "internal": internal}
            out["output"] = [internal if x.get("call_id") == call_id else x for x in out["output"]]
            self.audit({"event": "allowed_call", "call_id": call_id, "public_name": external["name"],
                "backend": self.tools[external["name"]]["backend"], "compiled_sha256": hashlib.sha256(internal["input"].encode()).hexdigest()})
        return out


def completed_response(raw):
    if len(raw) > MAX_BODY:
        raise GateError("Response body exceeds limit")
    completions = []
    for frame in re.split(r"\r?\n\r?\n", raw.decode("utf-8")):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        event = strict_loads(data)
        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
            raise GateError("Upstream response failed")
        if event.get("type") == "response.completed":
            completions.append(event.get("response"))
    if len(completions) != 1:
        raise GateError("Expected exactly one complete response")
    return completions[0]


def response_sse(response):
    initial = dict(response, status="in_progress", output=[])
    events = [{"type": "response.created", "response": initial}]
    for index, item in enumerate(response["output"]):
        events.append({"type": "response.output_item.added", "output_index": index, "item": item})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": response})
    return "".join("event: " + x["type"] + "\ndata: " + json.dumps(dict(x, sequence_number=i)) + "\n\n"
                   for i, x in enumerate(events)).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GateError("Upstream redirects are disabled")


def make_server(upstream, gate, port=0, allow_remote=False):
    url = urllib.parse.urlsplit(upstream)
    local = url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
    remote = allow_remote and url.scheme == "https" and url.hostname in {"api.openai.com", "chatgpt.com"}
    if not (local or remote) or url.username or url.password or url.query or url.fragment:
        raise GateError("Upstream must be loopback, or explicitly authorized official HTTPS")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_error(405)

        def do_POST(self):
            # A lock makes the call-id history and controller state sequential.
            with gate.lock:
                try:
                    if self.path != "/responses" or self.client_address[0] != "127.0.0.1":
                        raise GateError("Only loopback POST /responses is accepted")
                    if self.headers.get("Content-Encoding", "identity") != "identity":
                        raise GateError("Compressed requests are not accepted")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BODY:
                        raise GateError("Request body exceeds limit")
                    request = gate.request(strict_loads(self.rfile.read(size)))
                    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
                    for key in ("Authorization", "ChatGPT-Account-ID", "OpenAI-Beta", "OpenAI-Organization", "OpenAI-Project"):
                        if key in self.headers:
                            headers[key] = self.headers[key]
                    req = urllib.request.Request(upstream.rstrip("/") + "/responses",
                        data=json.dumps(request, allow_nan=False).encode(), headers=headers, method="POST")
                    with opener.open(req, timeout=180) as result:
                        raw = result.read(MAX_BODY + 1)
                    transformed = gate.response(completed_response(raw))
                    data = response_sse(transformed)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                except Exception as exc:
                    # No upstream bytes have been sent to Codex on this path.
                    gate.audit({"event": "denied", "reason": type(exc).__name__ + ": " + str(exc)})
                    data = json.dumps({"error": {"type": "tool_gate_rejection", "message": "The tool gate rejected the request/response; no tool dispatch was forwarded."}}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    def audit(event):
        with args.audit.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
    gate = Gate(strict_loads(args.mapping.read_text()) if args.mapping else None, audit)
    server = make_server(args.upstream, gate, args.port, args.allow_remote)
    args.ready.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}",
        "public_tools": sorted(gate.tools), "mapping": {k:v["backend"] for k,v in gate.tools.items()}}) + "\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
