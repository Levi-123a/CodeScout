import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codescout.server import create_server
from codescout.service import Service
from codescout.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.server = create_server(Service(self.store), 0, "test-session-token")
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, auth=True, origin=None, method=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer test-session-token"
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read())

    def test_api_requires_token_and_blocks_cross_origin_even_with_token(self):
        for auth, origin in [(False, None), (True, "https://evil.example")]:
            with self.subTest(auth=auth, origin=origin), self.assertRaises(HTTPError) as error:
                self.request("/api/state", auth=auth, origin=origin)
            self.assertEqual(error.exception.code, 403)

    def test_projects_model_and_pause_roundtrip_without_key_echo(self):
        self.request("/api/model", {"base_url": "https://example.com/v1", "model": "x", "api_key": "secret"})
        project = self.request("/api/projects", {"path": str(self.root), "allow_send": True})
        self.request(f"/api/projects/{project['id']}/pause", {})
        state = self.request("/api/state")
        self.assertTrue(state["projects"][0]["paused"])
        self.assertNotIn("secret", json.dumps(state))
        self.assertTrue(state["model"]["has_key"])
        self.request(f"/api/projects/{project['id']}", method="DELETE")
        self.assertTrue(self.root.exists())

    def test_malformed_request_is_clear_client_error(self):
        request = Request(self.url + "/api/projects", data=b"not-json", headers={"Authorization": "Bearer test-session-token", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 400)

    def test_unknown_or_traversal_path_cannot_read_arbitrary_file(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/../store.py", timeout=3)
        self.assertEqual(error.exception.code, 404)

