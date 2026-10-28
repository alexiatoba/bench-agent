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


GO_TRACES: dict[str, Path] = {}  # service url -> its -traces file


@pytest.fixture(scope="module")
def go_url(tmp_path_factory):
    if GO is None:
        pytest.skip("Go is not installed")
    exe = tmp_path_factory.mktemp("go") / "bench-api.exe"
    subprocess.run([str(GO), "build", "-o", str(exe), "."], cwd=ROOT / "bench-api", check=True)
    with socket.socket() as s:  # a free port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    traces = exe.with_name("bench-api-traces.jsonl")
    proc = subprocess.Popen([str(exe), "-addr", f"127.0.0.1:{port}", "-data", str(ROOT / "data"),
                             "-traces", str(traces)])
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    GO_TRACES[url] = traces
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


def test_one_trace_across_python_and_go(go_url, monkeypatch, tmp_path):
    """The agent's traceparent reaches Go: Go's spans are children of the agent's tool spans."""
    import json
    from types import SimpleNamespace as NS

    from bench_agent import agent, show_trace, tracing

    def reply(content=None, calls=()):
        tc = [NS(id=f"c{i}", function=NS(name=n, arguments=a)) for i, (n, a) in enumerate(calls)] or None
        return NS(choices=[NS(message=NS(content=content, tool_calls=tc),
                              finish_reason="tool_calls" if tc else "stop")], usage=None)

    script = [
        reply(calls=[("run_sql", json.dumps({"query": "SELECT speed FROM results"}))]),
        reply(calls=[("run_sql", json.dumps({"query": "SELECT time_ms FROM results WHERE version='cpu'"}))]),
        reply("The CPU takes 2749.0 ms."),
    ]
    monkeypatch.setattr(tracing, "TRACE_FILE", tmp_path / "traces.jsonl")
    monkeypatch.setenv("BENCH_API_URL", go_url)
    client = NS(chat=NS(completions=NS(create=lambda **kw: script.pop(0))))
    assert agent.answer("CPU time?", client, "fake")["answer"] == "The CPU takes 2749.0 ms."

    py = show_trace.load([tracing.TRACE_FILE])
    trace_id = next(s["trace_id"] for s in py if s["name"] == "agent.answer")
    tool_spans = [s for s in py if s["name"] == "tool.run_sql"]

    def go_spans():
        return [s for s in show_trace.load([GO_TRACES[go_url]]) if s["trace_id"] == trace_id]

    for _ in range(100):  # Go writes a server span after its response is sent
        if len(go_spans()) == 4:
            break
        time.sleep(0.05)
    go = go_spans()
    servers = sorted((s for s in go if s["name"] == "POST /sql"), key=lambda s: s["start"])
    queries = {s["parent_id"]: s for s in go if s["name"] == "sqlite.query"}
    assert len(servers) == 2 and len(queries) == 2
    for tool_span, server in zip(tool_spans, servers):
        assert server["parent_id"] == tool_span["span_id"]  # Go joined the agent's span
        assert server["service"] == "bench-api" and tool_span["service"] == "bench-agent"
    first_query = queries[servers[0]["span_id"]]
    assert first_query["error"] == "SQL error: no such column: speed"
    assert first_query["attrs"]["bench.sql.reject_reason"] == "sql_error"

    tree = show_trace.render(py + go, trace_id)
    assert "2 services" in tree and "sqlite.query [bench-api]" in tree
