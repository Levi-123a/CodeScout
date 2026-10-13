"""Disposable browser QA: synthetic project/model, with picker boundary substituted."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codescout import server as server_module
from codescout.service import Service
from codescout.store import Store
from tests.helpers import finding


def main():
    workspace = Path(__file__).resolve().parents[1]
    data = workspace / ".codescout" / ("qa-" + secrets.token_hex(4))
    project = data / "sample-project"
    project.mkdir(parents=True)
    (project / "main.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    (project / "README.md").write_text("The value function returns 1.\n", encoding="utf-8")
    for index in range(4):
        (project / f"note{index}.txt").write_text("Project example.\n", encoding="utf-8")
    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if request.get("tools"):
                if request["messages"][-1]["role"] != "tool":
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "read-source", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "main.py", "start_line": 1, "end_line": 20})}}]}
                else:
                    message = {"role": "assistant", "content": json.dumps({"edits": [{"path": "main.py", "old": "return 2", "new": "return 1"}]})}
            else:
                context = json.loads(request["messages"][1]["content"])
                files = context.get("files", {})
                issues = [finding(line=2)] if "return 2" in files.get("main.py", "") and "returns 1" in files.get("README.md", "") else []
                message = {"role": "assistant", "content": json.dumps({"findings": issues})}
            payload = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    service = Service(Store(data / "state.db"))
    service.configure_model({"base_url": f"http://127.0.0.1:{model.server_port}/v1", "model": "qa-model", "api_key": "qa-key", "context_chars": 12000})
    server_module.choose_folder = lambda: str(project)
    token = secrets.token_urlsafe(32)
    server = server_module.create_server(service, 8775, token)
    print(f"http://127.0.0.1:{server.server_port}/#token={token}", flush=True)
    service.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
