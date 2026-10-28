package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

const (
	callerTrace = "4bf92f3577b34da6a3ce929d0e0e4736"
	callerSpan  = "00f067aa0ba902b7"
)

func postSQL(t *testing.T, url, query string) {
	t.Helper()
	req, _ := http.NewRequest("POST", url+"/sql", strings.NewReader(mustJSON(t, map[string]string{"query": query})))
	req.Header.Set("traceparent", "00-"+callerTrace+"-"+callerSpan+"-01")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
}

func TestSpansJoinTheCallersTrace(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	otel.SetTracerProvider(tp)
	t.Cleanup(func() { _ = tp.Shutdown(t.Context()) })
	installPropagator()

	ts := newTestServer(t)
	postSQL(t, ts.URL, "SELECT version FROM results")
	postSQL(t, ts.URL, "SELECT 1; DROP TABLE results")

	// The server span ends after the response is sent, so wait for it.
	waitFor(t, func() bool { return len(exp.GetSpans()) >= 4 })
	spans := exp.GetSpans()
	if len(spans) != 4 { // per request: the HTTP server span and its sqlite.query child
		t.Fatalf("got %d spans: %v", len(spans), spans)
	}
	byName := map[string][]tracetest.SpanStub{}
	for _, s := range spans {
		if got := s.SpanContext.TraceID().String(); got != callerTrace {
			t.Errorf("%s: trace %s, want the caller's %s", s.Name, got, callerTrace)
		}
		byName[s.Name] = append(byName[s.Name], s)
	}
	servers, queries := byName["POST /sql"], byName["sqlite.query"]
	if len(servers) != 2 || len(queries) != 2 {
		t.Fatalf("span names: %v", byName)
	}
	for i, srv := range servers {
		if got := srv.Parent.SpanID().String(); got != callerSpan {
			t.Errorf("server span parent %s, want the caller's span %s", got, callerSpan)
		}
		if queries[i].Parent.SpanID() != srv.SpanContext.SpanID() {
			t.Errorf("sqlite.query is not a child of its server span")
		}
	}
	rejected := queries[1]
	if rejected.Status.Code != codes.Error {
		t.Errorf("rejected query should be an error span, got %v", rejected.Status)
	}
	for _, kv := range rejected.Attributes {
		if kv.Key == attrReason && kv.Value.AsString() != "text_check" {
			t.Errorf("reason %q", kv.Value.AsString())
		}
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if cond() {
			return
		}
	}
}

func TestJSONLExporterFormat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "traces.jsonl")
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(&jsonlExporter{path: path}))
	otel.SetTracerProvider(tp)
	t.Cleanup(func() { _ = tp.Shutdown(t.Context()) })
	installPropagator()

	postSQL(t, newTestServer(t).URL, "SELECT version FROM results")
	var lines []string
	waitFor(t, func() bool {
		data, _ := os.ReadFile(path)
		lines = strings.Split(strings.TrimSpace(string(data)), "\n")
		return len(lines) >= 2
	})
	if len(lines) != 2 {
		t.Fatalf("got %d lines", len(lines))
	}
	var server map[string]any
	if err := json.Unmarshal([]byte(lines[1]), &server); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"service", "trace_id", "span_id", "parent_id", "name", "start", "duration_ms", "attrs", "error"} {
		if _, ok := server[k]; !ok {
			t.Errorf("missing %q in %s", k, lines[1])
		}
	}
	if server["service"] != "bench-api" || server["parent_id"] != callerSpan || server["name"] != "POST /sql" {
		t.Errorf("unexpected span: %s", lines[1])
	}
}
