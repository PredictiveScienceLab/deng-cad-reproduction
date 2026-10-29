"""External socket owner for BrokerV3; reuse the frozen relay unchanged."""
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import tempfile
import threading
import time
from broker_v3 import BrokerV3, serve_stream


class BrokerServerV3:
    def __init__(self, deadline, audit_path=None, *, check_reports, _service_factory=None):
        self.broker = BrokerV3(deadline, audit_path, check_reports=check_reports,
                               _service_factory=_service_factory)
        self.token = secrets.token_hex(32)
        self.directory = Path(tempfile.mkdtemp(prefix='deng-broker-v3-', dir='/private/tmp'))
        self.path = self.directory/'s'
        self.connections = set()
        self.connection_lock = threading.Lock()
        self.closing = threading.Event()
        owner = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with owner.connection_lock:
                    if owner.closing.is_set(): return
                    owner.connections.add(self.request)
                try:
                    self.request.settimeout(max(.01, min(5, owner.broker.trial_deadline-time.monotonic())))
                    auth = json.loads(self.rfile.readline(256))
                    if not isinstance(auth, dict) or not isinstance(auth.get('token'), str): return
                    if not hmac.compare_digest(auth['token'], owner.token): return
                    self.wfile.write(b'OK\n'); self.wfile.flush()
                    self.request.settimeout(max(.01, owner.broker.trial_deadline-time.monotonic()+.25))
                    serve_stream(owner.broker, self.rfile, self.wfile)
                except (OSError, ValueError):
                    pass
                finally:
                    with owner.connection_lock: owner.connections.discard(self.request)
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = False
            block_on_close = True
        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def environment(self):
        return {'DENG_MCP_SOCKET': str(self.path), 'DENG_MCP_TOKEN': self.token}

    def close(self):
        self.closing.set()
        self.broker.cancel_event.set()
        self.server.shutdown()
        with self.connection_lock: connections = list(self.connections)
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.server.server_close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)
        self.directory.rmdir()
