package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"sync"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

// OpenTelemetry tracing. The Python agent sends a W3C `traceparent` header with each
// tool call; otelhttp reads it, so the spans here join the agent's trace as children
// of its tool.<name> span: one question is one trace across both services.
//
// Exporters, like the Python side:
//
//	-traces file                 JSONL in the same line format as bench_agent/tracing.py
//	OTEL_EXPORTER_OTLP_ENDPOINT  OTLP over HTTP (e.g. Jaeger at http://localhost:4318)

var (
	attrReason = attribute.Key("bench.sql.reject_reason")
	attrRows   = attribute.Key("db.response.returned_rows")
)

func setupTracing(tracesPath string) (shutdown func(context.Context) error, err error) {
	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(resource.NewSchemaless(semconv.ServiceName("bench-api"))),
	}
	if tracesPath != "" {
		opts = append(opts, sdktrace.WithSyncer(&jsonlExporter{path: tracesPath}))
	}
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
		exp, err := otlptracehttp.New(context.Background()) // reads the env var
		if err != nil {
			return nil, err
		}
		opts = append(opts, sdktrace.WithBatcher(exp))
	}
	tp := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(tp)
	installPropagator()
	return tp.Shutdown, nil
}

// installPropagator makes otelhttp read (and our clients write) W3C traceparent headers.
func installPropagator() { otel.SetTextMapPropagator(propagation.TraceContext{}) }

// traced wraps the router: otelhttp starts a server span from the incoming traceparent,
// and once the mux has matched, the span is renamed to the route ("POST /sql").
func traced(next http.Handler) http.Handler {
	named := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
		if r.Pattern != "" {
			trace.SpanFromContext(r.Context()).SetName(r.Pattern)
		}
	})
	return otelhttp.NewHandler(named, "bench-api")
}

// sqlSpan records one guarded query as a child span; a rejection marks it as an error
// and carries the guard layer, the same label as bench_api_sql_rejected_total.
func sqlSpan(ctx context.Context, query string) (context.Context, func(rows int, err error)) {
	// Look the tracer up per use: a package-level otel.Tracer stays bound to the first
	// provider ever installed, which silently splits spans when tests install their own.
	ctx, span := otel.Tracer("bench-api").Start(ctx, "sqlite.query", trace.WithAttributes(
		semconv.DBSystemSqlite, semconv.DBQueryText(truncate(query, 500))))
	return ctx, func(rows int, err error) {
		if err != nil {
			if te, ok := err.(*ToolError); ok && te.reason != "" {
				span.SetAttributes(attrReason.String(te.reason))
			}
			span.SetStatus(codes.Error, err.Error())
		} else {
			span.SetAttributes(attrRows.Int(rows))
		}
		span.End()
	}
}

func truncate(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n]) + "…"
	}
	return s
}

// jsonlExporter writes each span as one line, matching bench_agent/tracing.py, so the
// two services' trace files can be merged (see bench_agent/show_trace.py).
type jsonlExporter struct {
	path string
	mu   sync.Mutex
}

func (e *jsonlExporter) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	f, err := os.OpenFile(e.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	for _, s := range spans {
		attrs := map[string]any{}
		for _, kv := range s.Attributes() {
			attrs[string(kv.Key)] = kv.Value.AsInterface()
		}
		var parent, errText any
		if s.Parent().IsValid() {
			parent = s.Parent().SpanID().String()
		}
		if s.Status().Code == codes.Error {
			errText = s.Status().Description
		}
		if err := enc.Encode(map[string]any{
			"service":     "bench-api",
			"trace_id":    s.SpanContext().TraceID().String(),
			"span_id":     s.SpanContext().SpanID().String(),
			"parent_id":   parent,
			"name":        s.Name(),
			"start":       float64(s.StartTime().UnixNano()) / 1e9,
			"duration_ms": float64(s.EndTime().Sub(s.StartTime()).Microseconds()) / 1000,
			"attrs":       attrs,
			"error":       errText,
		}); err != nil {
			return err
		}
	}
	return nil
}

func (e *jsonlExporter) Shutdown(context.Context) error { return nil }
