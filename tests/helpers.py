import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def model_server(response=None, status=200, raw=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = raw if raw is not None else json.dumps({"choices": [{"message": {"content": json.dumps({"findings": response or []})}}]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "test-model", "api_key": "top-secret", "timeout": 3, "context_chars": 60000, "version": 1}, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def finding(path="main.py", line=1, evidence="return 2"):
    return {"title": "说明与实现不同步", "path": path, "line": line, "category": "documentation", "severity": "medium", "reason": "说明返回 1，实现返回 2", "evidence": evidence, "rule": "注释须与实现同步", "suggestion": "更新说明", "confidence": "medium", "related": [{"path": "README.md", "line": 1, "evidence": "returns 1"}]}

