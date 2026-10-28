"""Tracing with OpenTelemetry: every LLM call and tool call becomes an OTel span.  (step 5)

Same interface as the old JSONL tracer, so agent.py did not change:

    with span("llm.call", trace_id, step=1) as attrs:
        attrs["tokens_in"] = 664          # add attributes while the span is open

Exporters (all fed by the same spans):
  traces.jsonl         always; same line format as before, so old traces and scripts still work
  ConsoleSpanExporter  BENCH_OTEL_CONSOLE=1; prints OTel JSON to stderr (stdout is the
                       answer for the CLI and the protocol channel for the MCP server)
  OTLP (e.g. Jaeger)   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318; needs
                       `pip install opentelemetry-exporter-otlp-proto-http`

Trace context is OTel's: nesting comes from the active span, not from a hand-kept stack,
and the trace_id argument is kept only as an attribute (app.trace_id) for compatibility.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import uuid
from collections.abc import MutableMapping, Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode

TRACE_FILE = Path("traces.jsonl")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonlSpanExporter(SpanExporter):
    """Writes each finished span as one line of traces.jsonl, in the original format."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            for s in spans:
                error = None
                if s.status.status_code is StatusCode.ERROR:
                    error = s.status.description or "error"
                f.write(json.dumps({
                    "trace_id": format(s.context.trace_id, "032x"),
                    "span_id": format(s.context.span_id, "016x"),
                    "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                    "name": s.name,
                    "start": s.start_time / 1e9,
                    "duration_ms": round((s.end_time - s.start_time) / 1e6, 2),
                    "attrs": {k: v for k, v in s.attributes.items() if k != "app.trace_id"},
                    "error": error,
                }, default=str) + "\n")
        return SpanExportResult.SUCCESS


def _make_provider() -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "bench-agent"}))
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter()))
    if os.environ.get("BENCH_OTEL_CONSOLE") == "1":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads the env var
    return provider


_provider = _make_provider()
_tracer = _provider.get_tracer("bench_agent")


def _otel_value(value):
    """OTel attributes must be str/bool/int/float or lists of them."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, (str, bool, int, float)) for v in value):
        return list(value)
    return json.dumps(value, default=str)


class _Attrs(MutableMapping):
    """Dict the caller writes into; every write also lands on the OTel span."""

    def __init__(self, otel_span):
        self._span, self._data = otel_span, {}

    def __setitem__(self, key, value):
        self._data[key] = value
        if value is not None:  # OTel has no null attribute
            self._span.set_attribute(key, _otel_value(value))

    def __getitem__(self, key):
        return self._data[key]

    def __delitem__(self, key):
        del self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


@contextlib.contextmanager
def span(name: str, trace_id: str, **attrs):
    with _tracer.start_as_current_span(name, record_exception=True, set_status_on_exception=False) as s:
        s.set_attribute("app.trace_id", trace_id)
        record = _Attrs(s)
        record.update(attrs)
        try:
            yield record  # caller can add attributes while the span is open
        except Exception as e:
            s.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"))
            raise
