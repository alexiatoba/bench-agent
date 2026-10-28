// bench-api: the bench-agent tools as an HTTP service.
//
//	go run . -addr :8080 -data ../data
//
//	GET /healthz
//	GET /schema
//	GET /findings?topic=npp
//	GET /compare?a=cpu&b=v3_separable
//	POST /sql      {"query": "SELECT ..."}   read-only, guarded (guard.go)
//	GET /metrics   Prometheus metrics (metrics.go)
//
// Success: 200 with the same JSON the Python tool returns. Bad input from the model:
// 400 {"error": "..."}, which the agent hands back to the model so it can retry.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/ncruces/go-sqlite3"
)

type server struct {
	db      pool
	metrics *metrics
}

const maxQueryBytes = 64 << 10 // a model-written query has no business being larger

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataDir := flag.String("data", "../data", "directory with results.csv and findings.csv")
	conns := flag.Int("conns", 4, "read-only SQLite connections")
	flag.Parse()

	srv, err := newServer(*dataDir, *conns)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("bench-api listening on %s", *addr)
	hs := &http.Server{Addr: *addr, Handler: srv.routes(), ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(hs.ListenAndServe())
}

func newServer(dataDir string, conns int) (*server, error) {
	path, err := buildDB(dataDir)
	if err != nil {
		return nil, err
	}
	db, err := openPool(path, conns)
	if err != nil {
		return nil, err
	}
	return &server{db: db, metrics: newMetrics()}, nil
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", s.metrics.handler())
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /schema", s.handle(func(c *sqlite3.Conn, r *http.Request) (any, error) {
		return getSchema(c)
	}))
	mux.HandleFunc("GET /findings", s.handle(func(c *sqlite3.Conn, r *http.Request) (any, error) {
		return getFindings(c, r.URL.Query().Get("topic"))
	}))
	mux.HandleFunc("GET /compare", s.handle(func(c *sqlite3.Conn, r *http.Request) (any, error) {
		q := r.URL.Query()
		if q.Get("a") == "" || q.Get("b") == "" {
			return nil, toolErrorf("bad arguments for compare_versions: both a and b are required")
		}
		return compareVersions(c, q.Get("a"), q.Get("b"))
	}))
	mux.HandleFunc("POST /sql", s.handle(func(c *sqlite3.Conn, r *http.Request) (any, error) {
		var body struct {
			Query *string `json:"query"`
		}
		dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, maxQueryBytes))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&body); err != nil || body.Query == nil {
			return nil, toolErrorf(`bad arguments for run_sql: expected {"query": "..."}`)
		}
		return runSQL(r.Context(), c, *body.Query)
	}))
	return s.metrics.middleware(mux)
}

// handle borrows a connection, runs the tool and maps errors to status codes.
func (s *server) handle(tool func(*sqlite3.Conn, *http.Request) (any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var out any
		err := s.db.with(r.Context(), func(c *sqlite3.Conn) (err error) {
			out, err = tool(c, r)
			return err
		})
		var te *ToolError
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, out)
		case errors.As(err, &te):
			s.metrics.observeToolError(err)
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": te.Error()})
		default:
			log.Printf("%s %s: %v", r.Method, r.URL, err)
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
