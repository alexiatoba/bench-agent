package main

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Prometheus metrics, served at GET /metrics.
//
//	bench_api_requests_total{route,status}        traffic and error rate per tool
//	bench_api_request_duration_seconds{route}     latency histogram per tool
//	bench_api_sql_rejected_total{reason}          which guard layer rejected a query
//
// A rise in sql_rejected_total{reason="not_read_only"|"authorizer"|"text_check"} means
// model-written SQL tried to write: worth an alert (prompt injection), unlike sql_error.
type metrics struct {
	registry    *prometheus.Registry
	requests    *prometheus.CounterVec
	duration    *prometheus.HistogramVec
	sqlRejected *prometheus.CounterVec
}

func newMetrics() *metrics {
	m := &metrics{
		registry: prometheus.NewRegistry(),
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "bench_api_requests_total", Help: "HTTP requests by route and status code.",
		}, []string{"route", "status"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "bench_api_request_duration_seconds",
			Help:    "Request latency by route.",
			Buckets: []float64{.0005, .001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5},
		}, []string{"route"}),
		sqlRejected: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "bench_api_sql_rejected_total", Help: "run_sql queries rejected, by guard layer.",
		}, []string{"reason"}),
	}
	m.registry.MustRegister(m.requests, m.duration, m.sqlRejected,
		prometheus.NewGoCollector(), prometheus.NewProcessCollector(prometheus.ProcessCollectorOpts{}))
	return m
}

func (m *metrics) handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

// observeToolError counts run_sql rejections; other tool errors carry no reason.
func (m *metrics) observeToolError(err error) {
	var te *ToolError
	if errors.As(err, &te) && te.reason != "" {
		m.sqlRejected.WithLabelValues(te.reason).Inc()
	}
}

// middleware records every request. The route label is the mux pattern ("POST /sql"),
// not the raw URL, so query strings can't blow up the number of time series.
func (m *metrics) middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		route := r.Pattern
		if route == "" {
			route = "unmatched"
		}
		m.requests.WithLabelValues(route, strconv.Itoa(rec.status)).Inc()
		m.duration.WithLabelValues(route).Observe(time.Since(start).Seconds())
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}
