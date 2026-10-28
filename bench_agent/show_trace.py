"""Print one question's trace as a tree, merged across services.

    python -m bench_agent.show_trace traces.jsonl bench-api-traces.jsonl
    python -m bench_agent.show_trace traces.jsonl bench-api-traces.jsonl --trace <trace_id>

Both files use the same line format (bench_agent/tracing.py, bench-api/tracing.go).
Without --trace, it shows the most recent agent.answer.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SHOWN_ATTRS = ("question", "outcome", "tool_error", "db.query.text", "bench.sql.reject_reason",
               "db.response.returned_rows", "http.response.status_code")


def load(paths: list[Path]) -> list[dict]:
    spans = []
    for path in paths:
        if path.exists():
            spans += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return spans


def render(spans: list[dict], trace_id: str | None = None) -> str:
    if trace_id is None:
        roots = [s for s in spans if s["name"] == "agent.answer"]
        if not roots:
            return "no agent.answer span found"
        trace_id = max(roots, key=lambda s: s["start"])["trace_id"]
    spans = [s for s in spans if s["trace_id"] == trace_id]
    ids = {s["span_id"] for s in spans}
    children = defaultdict(list)
    for s in spans:
        children[s["parent_id"] if s["parent_id"] in ids else None].append(s)
    t0 = min(s["start"] for s in spans)
    lines = [f"trace {trace_id}  ({len(spans)} spans, "
             f"{len({s.get('service', '?') for s in spans})} services)"]

    def walk(span: dict, depth: int) -> None:
        attrs = {k: span["attrs"][k] for k in SHOWN_ATTRS if k in span["attrs"]}
        detail = "  " + " ".join(f"{k}={str(v)[:60]!r}" for k, v in attrs.items()) if attrs else ""
        error = f"  ERROR: {span['error'][:60]}" if span.get("error") else ""
        lines.append(f"{(span['start'] - t0) * 1000:8.1f}ms {span['duration_ms']:9.1f}ms  "
                     f"{'  ' * depth}{span['name']} [{span.get('service', '?')}]{detail}{error}")
        for child in sorted(children[span["span_id"]], key=lambda s: s["start"]):
            walk(child, depth + 1)

    for root in sorted(children[None], key=lambda s: s["start"]):
        walk(root, 0)
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--trace", help="trace id (default: the latest agent.answer)")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(load(a.files), a.trace))


if __name__ == "__main__":
    main()
