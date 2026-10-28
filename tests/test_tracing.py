"""The agent loop with a scripted fake LLM: checks the loop and the OTel spans. No LLM needed."""
import json
from types import SimpleNamespace as NS

from bench_agent import agent, tracing


def _reply(content=None, calls=()):
    tc = [NS(id=f"c{i}", function=NS(name=n, arguments=a)) for i, (n, a) in enumerate(calls)] or None
    return NS(choices=[NS(message=NS(content=content, tool_calls=tc),
                          finish_reason="tool_calls" if tc else "stop")],
              usage=NS(prompt_tokens=10, completion_tokens=5))


def _fake_client(script):
    return NS(chat=NS(completions=NS(create=lambda **kw: script.pop(0))))


def test_recovers_from_bad_sql_and_traces_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "TRACE_FILE", tmp_path / "traces.jsonl")
    script = [
        _reply(calls=[("run_sql", json.dumps({"query": "SELECT speed FROM results"}))]),
        _reply(calls=[("run_sql", json.dumps({"query": "SELECT time_ms FROM results WHERE version='cpu'"}))]),
        _reply("The CPU takes 2749.0 ms."),
    ]
    out = agent.answer("CPU time?", _fake_client(script), "fake")
    assert out == {"answer": "The CPU takes 2749.0 ms.", "tool_calls": ["run_sql", "run_sql"], "steps": 3}

    spans = [json.loads(line) for line in tracing.TRACE_FILE.read_text().splitlines()]
    root = next(s for s in spans if s["name"] == "agent.answer")
    assert {s["trace_id"] for s in spans} == {root["trace_id"]}  # one trace
    assert all(s["parent_id"] == root["span_id"] for s in spans if s is not root)
    assert [s["name"] for s in spans if s is not root] == [
        "llm.call", "tool.run_sql", "llm.call", "tool.run_sql", "llm.call"]
    first_sql = next(s for s in spans if s["name"] == "tool.run_sql")
    assert "no such column: speed" in first_sql["attrs"]["tool_error"]
    assert root["attrs"]["outcome"] == "answered" and root["attrs"]["tool_calls"] == ["run_sql", "run_sql"]


def test_gives_up_after_max_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "TRACE_FILE", tmp_path / "traces.jsonl")
    client = NS(chat=NS(completions=NS(create=lambda **kw: _reply(calls=[("get_schema", "{}")]))))
    out = agent.answer("loop", client, "fake")
    assert out["steps"] == agent.MAX_STEPS and "gave up" in out["answer"]


def test_exception_marks_span_as_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "TRACE_FILE", tmp_path / "traces.jsonl")

    def boom(**kw):
        raise ConnectionError("LLM down")

    try:
        agent.answer("q", NS(chat=NS(completions=NS(create=boom))), "fake")
    except ConnectionError:
        pass
    spans = [json.loads(line) for line in tracing.TRACE_FILE.read_text().splitlines()]
    assert all(s["error"] == "ConnectionError: LLM down" for s in spans)  # llm.call and agent.answer
