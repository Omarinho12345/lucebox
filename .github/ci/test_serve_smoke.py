"""Tests for .github/ci/serve_smoke.sh."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

SERVE_SMOKE = Path(__file__).resolve().with_name("serve_smoke.sh")

_STUB_SERVER = """\
#!{python}
import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

args = sys.argv[1:]
host = args[args.index("--host") + 1]
port = int(args[args.index("--port") + 1])
Path(args[args.index("--port-file") + 1]).write_text(str(port))

COUNT_TO_30 = ", ".join(str(i) for i in range(1, 31))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({{"data": []}})
        elif self.path == "/status/json":
            self._send(
                {{
                    "perf_history": [
                        {{"decode_tok_s": 50.0, "spec_decode": True, "accept_rate": 0.5}}
                    ]
                }}
            )
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._send({{"choices": [{{"message": {{"content": COUNT_TO_30}}}}]}})


signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
ThreadingHTTPServer((host, port), Handler).serve_forever()
"""


def _make_stub_server(tmp_path: Path) -> tuple[Path, Path, Path]:
    stub = tmp_path / "fake_luce_server.py"
    stub.write_text(_STUB_SERVER.format(python=sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"GGUF")
    port_file = tmp_path / "bound_port.txt"
    return stub, model, port_file


def _run_serve_smoke(
    tmp_path: Path,
    *,
    at_once: int,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stub, model, port_file = _make_stub_server(tmp_path)
    env = os.environ.copy()
    env.pop("SERVE_SMOKE_PORT", None)
    env.pop("SERVE_SMOKE_PROFILE", None)
    env["RUNNER_TEMP"] = str(tmp_path)
    if extra_env:
        env.update(extra_env)
    res = subprocess.run(
        [
            "bash",
            str(SERVE_SMOKE),
            "test-model",
            str(at_once),
            str(stub),
            str(model),
            "--port-file",
            str(port_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return res, port_file


def test_picks_free_port_when_unset(tmp_path: Path) -> None:
    """When SERVE_SMOKE_PORT is unset, pick an unused port instead of colliding on 8098."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            blocker.bind(("127.0.0.1", 8098))
            blocker.listen(1)
        except OSError:
            # 8098 is already held on this host, which exercises the same path.
            pass

        res, port_file = _run_serve_smoke(tmp_path, at_once=2)
    finally:
        blocker.close()

    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    bound_port = int(port_file.read_text().strip())
    assert bound_port != 8098
    assert "2 at once: 2 of 2 counted to 30" in res.stdout


def test_respects_explicit_serve_smoke_port(tmp_path: Path) -> None:
    """An explicit SERVE_SMOKE_PORT override is passed through unchanged."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        explicit_port = s.getsockname()[1]

    res, port_file = _run_serve_smoke(
        tmp_path,
        at_once=1,
        extra_env={"SERVE_SMOKE_PORT": str(explicit_port)},
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    assert int(port_file.read_text().strip()) == explicit_port
