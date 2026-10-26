"""Unit tests for the deployment helpers that do not need Docker."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from deploy.docker_stack import DockerStack
from deploy.watch import Watcher
from gateway import CommandGateway

PROM = (
    "service_error_rate 12.5\nservice_latency_p95_ms 40\nservice_request_rate 8\n"
    "process_resident_memory_mb 55\nprocess_cpu_percent 3\n"
)


def _server() -> tuple[HTTPServer, int]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(PROM.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_watcher_reads_metrics_and_marks_a_dead_service_as_down():
    srv, up = _server()
    try:
        w = Watcher({"api": up, "db": _closed_port()}, interval=0.01)
        w.poll()
        assert w.metrics["api"]["error_rate"] == [12.5] and w.metrics["api"]["memory_mb"] == [55.0]
        assert w.metrics["db"]["error_rate"] == [100.0] and w.metrics["db"]["request_rate"] == [0.0]
        assert w.samples == 1
    finally:
        srv.shutdown()


def test_docker_stack_exposes_all_four_services_to_the_gateway(tmp_path):
    stack = DockerStack(log_dir=tmp_path)
    assert set(stack.services) == {"api-gateway", "orders-service", "inventory-service", "orders-db"}
    gw = CommandGateway(stack)
    # services come from the stack, not only from its published ports
    (tmp_path / "orders-service.log").write_text("AttributeError\n")
    stack.refresh_logs = lambda: None  # no docker here
    assert gw.run("grep -c AttributeError orders-service.log").output.strip() == "1"
    assert gw.run("svc restart orders-db").status == "needs_approval"


def test_aws_scripts_have_valid_shell_syntax_and_no_secrets():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "deploy" / "aws"
    for script in root.glob("*.sh"):
        assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0, script.name
        text = script.read_text()
        assert "AKIA" not in text and "BEGIN" not in text  # no access keys or private keys
