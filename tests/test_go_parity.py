"""The Go service (bench-api/) must answer every tool call exactly like the Python tools:
same results, same error messages. Builds and starts the real binary; skipped without Go."""
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from bench_agent import db, tools
from bench_agent.tools import ToolError

ROOT = Path(__file__).resolve().parent.parent
GO = shutil.which("go") or next(
    (p for p in [Path("C:/Program Files/Go/bin/go.exe"), Path("/usr/local/go/bin/go")] if p.exists()), None)

CALLS = [
    ("get_schema", {}),
    ("get_findings", {}),
    ("get_findings", {"topic": "npp"}),
    ("get_findings", {"topic": None}),
    ("compare_versions", {"a": "cpu", "b": "v3_separable"}),
    ("compare_versions", {"a": "npp", "b": "v3_separable"}),
    ("compare_versions", {"a": "cpu", "b": "v9_magic"}),
    ("compare_versions", {"a": "cpu"}),
    ("run_sql", {"query": "SELECT version, time_ms FROM results ORDER BY time_ms"}),
    ("run_sql", {"query": "SELECT * FROM results WHERE version = 'cpu'"}),
    ("run_sql", {"query": "SELECT COUNT(*) AS n, AVG(time_ms) AS avg FROM results"}),
    ("run_sql", {"query": "SELECT speed FROM results"}),
    ("run_sql", {"query": "SELEC version FROM results"}),
    ("run_sql", {"sql": "SELECT 1"}),
    ("rm_rf", {}),
    # attacks: rejected by both, with the same message
    ("run_sql", {"query": "DELETE FROM results"}),
    ("run_sql", {"query": "/* harmless */ DELETE FROM results"}),
    ("run_sql", {"query": "SELECT 1; DROP TABLE results"}),
    ("run_sql", {"query": "WITH x AS (SELECT 1) DELETE FROM results"}),
    ("run_sql", {"query": "PRAGMA table_info(results)"}),
    ("run_sql", {"query": "ATTACH DATABASE 'x.db' AS x"}),
]


@pytest.fixture(scope="module")
def go_url(tmp_path_factory):
    if GO is None:
        pytest.skip("Go is not installed")
    exe = tmp_path_factory.mktemp("go") / "bench-api.exe"
    subprocess.run([str(GO), "build", "-o", str(exe), "."], cwd=ROOT / "bench-api", check=True)
    with socket.socket() as s:  # a free port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([str(exe), "-addr", f"127.0.0.1:{port}", "-data", str(ROOT / "data")])
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def _outcome(name, args):
    try:
        return "ok", tools.call_tool(db.connect(), name, args)
    except ToolError as e:
        return "error", str(e)


@pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{a}" for n, a in CALLS])
def test_go_matches_python(go_url, monkeypatch, name, args):
    monkeypatch.delenv("BENCH_API_URL", raising=False)
    local = _outcome(name, args)
    monkeypatch.setenv("BENCH_API_URL", go_url)
    remote = _outcome(name, args)
    assert remote == local


def test_service_down_is_a_tool_error(monkeypatch):
    monkeypatch.setenv("BENCH_API_URL", "http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(ToolError, match="unavailable"):
        tools.call_tool(db.connect(), "get_schema", {})
