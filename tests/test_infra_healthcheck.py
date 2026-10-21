import importlib.util
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("infra_docker_healthcheck", ROOT / "scripts" / "docker_healthcheck.py")
assert _spec and _spec.loader
healthcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(healthcheck)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/api/config" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_healthy_when_api_config_answers(server, monkeypatch):
    monkeypatch.setenv("ACTIVITY_PORT", str(server))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert healthcheck.main() == 0


def test_unhealthy_when_nothing_listens(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("ACTIVITY_PORT", str(port))
    assert healthcheck.main() == 1
