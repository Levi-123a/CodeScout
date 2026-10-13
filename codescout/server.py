"""Loopback-only HTTP interface with session authorization and same-origin checks."""

import hmac
import json
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .native import choose_folder

WEB_ROOT = Path(__file__).parent / "web"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}


class LoopbackServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits multiple listeners: requests can reach a server
    # with a different session token. Reserve the address before binding instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(service, port, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self, api):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.headers.get("Host") != expected_host or origin and origin != "http://" + expected_host:
                return False
            if not api:
                return True
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth, "Bearer " + token)

        def body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2 * 1024 * 1024 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请发送大小不超过 2 MiB 的 JSON 请求")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求必须为 JSON 对象")
            return data

        def dispatch(self):
            path = urlsplit(self.path).path
            if not self.authorized(path.startswith("/api/")):
                return self.send(403, {"error": "访问被拒绝，请使用服务启动时显示的本机链接"})
            if self.command == "GET" and path in ASSETS:
                filename, content_type = ASSETS[path]
                return self.send(200, (WEB_ROOT / filename).read_bytes(), content_type)
            if self.command == "GET" and path == "/api/state":
                return self.send(200, service.state())
            if self.command == "POST" and path == "/api/model":
                return self.send(200, service.configure_model(self.body()))
            if self.command == "POST" and path == "/api/projects":
                return self.send(201, service.add_project(self.body()))
            if self.command == "POST" and path == "/api/pick-folder":
                self.body()
                folder = choose_folder()
                return self.send(200, {"path": folder, "cancelled": folder is None})
            repair_route = re.fullmatch(r"/api/projects/([a-f0-9]{32})/repairs(?:/([a-f0-9]{32})/apply)?", path)
            if self.command == "POST" and repair_route:
                project_id, repair_id = repair_route.groups()
                data = self.body()
                if repair_id:
                    return self.send(202, service.queue_apply(project_id, repair_id))
                return self.send(202, service.queue_repair(project_id, data.get("finding_id")))
            match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(?:/(runs|pause|resume|retry|findings|config))?", path)
            if match:
                project_id, action = match.groups()
                service.store.project(project_id)
                if self.command == "GET" and action == "runs":
                    return self.send(200, service.store.runs(project_id))
                if self.command == "POST" and action in {"pause", "resume", "retry"}:
                    self.body()
                    return self.send(200, service.action(project_id, action))
                if self.command == "POST" and action == "findings":
                    data = self.body()
                    service.store.set_finding_status(project_id, data.get("id"), data.get("status"))
                    return self.send(200, {"ok": True})
                if self.command == "POST" and action == "config":
                    return self.send(200, service.update_options(project_id, self.body()))
                if self.command == "DELETE" and action is None:
                    service.remove_project(project_id)
                    return self.send(200, {"ok": True})
            return self.send(404, {"error": "页面或接口不存在"})

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                self.dispatch()
            except json.JSONDecodeError:
                self.send(400, {"error": "请求必须为有效 JSON"})
            except ValueError as error:
                self.send(400, {"error": str(error)})
            except (TypeError, KeyError):
                self.send(400, {"error": "输入无效，请检查字段、路径和模型配置"})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self.send(500, {"error": "服务暂时无法处理请求"})

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    return LoopbackServer(("127.0.0.1", port), Handler)
