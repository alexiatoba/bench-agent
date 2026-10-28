package main

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/ncruces/go-sqlite3"
)

// The attack list from tests/test_tools.py, plus the ones the README write-up names.
var attacks = []string{
	"DELETE FROM results",
	"drop table results",
	"UPDATE results SET time_ms = 0",
	"INSERT INTO results (version) VALUES ('x')",
	"SELECT 1; DROP TABLE results",
	"PRAGMA table_info(results)",
	"ATTACH DATABASE 'x.db' AS x",
	"/* harmless */ DELETE FROM results",
	"CREATE TABLE t (a)",
	"WITH x AS (SELECT 1) DELETE FROM results",
	"dElEtE FROM results",
	"PRAGMA writable_schema = 1",
	"VACUUM INTO 'copy.db'",
	"SELECT load_extension('evil')",
	"SELECT 1; SELECT 2",
	"BEGIN; DELETE FROM results; COMMIT",
}

func testConn(t *testing.T) *sqlite3.Conn {
	t.Helper()
	path, err := buildDB("../data")
	if err != nil {
		t.Fatal(err)
	}
	p, err := openPool(path, 1)
	if err != nil {
		t.Fatal(err)
	}
	conn := <-p
	t.Cleanup(func() { conn.Close() })
	return conn
}

func rowCount(t *testing.T, conn *sqlite3.Conn) int64 {
	t.Helper()
	rows, err := query(conn, "SELECT COUNT(*) AS n FROM results")
	if err != nil {
		t.Fatal(err)
	}
	return rows[0].Get("n").(int64)
}

func TestAttacksAreRejected(t *testing.T) {
	conn := testConn(t)
	for _, q := range attacks {
		t.Run(q, func(t *testing.T) {
			_, err := runSQL(context.Background(), conn, q)
			var te *ToolError
			if err == nil || !errorsAs(err, &te) {
				t.Fatalf("expected ToolError, got %v", err)
			}
			if n := rowCount(t, conn); n != 6 {
				t.Fatalf("results has %d rows", n)
			}
		})
	}
}

// Layer 1 (the text check) is only for messages: the guard must hold without it.
func TestAttacksAreRejectedWithoutTextCheck(t *testing.T) {
	conn := testConn(t)
	for _, q := range attacks {
		t.Run(q, func(t *testing.T) {
			if _, err := execGuarded(context.Background(), conn, q); err == nil {
				t.Fatal("expected an error")
			}
			if n := rowCount(t, conn); n != 6 {
				t.Fatalf("results has %d rows", n)
			}
		})
	}
}

func TestSelectWorks(t *testing.T) {
	rows, err := runSQL(context.Background(), testConn(t),
		"SELECT version, time_ms FROM results WHERE version = 'v3_separable'")
	if err != nil {
		t.Fatal(err)
	}
	if got := mustJSON(t, rows); got != `[{"version":"v3_separable","time_ms":2.642}]` {
		t.Fatal(got)
	}
}

func TestWithSelectAndTrailingSemicolonWork(t *testing.T) {
	q := "WITH g AS (SELECT * FROM results WHERE version != 'cpu') SELECT MIN(time_ms) AS best FROM g; -- done"
	rows, err := runSQL(context.Background(), testConn(t), q)
	if err != nil {
		t.Fatal(err)
	}
	if got := mustJSON(t, rows); got != `[{"best":2.642}]` {
		t.Fatal(got)
	}
}

func TestFunctionsLikeReplaceStillWork(t *testing.T) {
	// A denylist on the word REPLACE would block this harmless string function.
	rows, err := runSQL(context.Background(), testConn(t),
		"SELECT replace(version, 'v', 'V') AS v FROM results WHERE version = 'v0_naive'")
	if err != nil || mustJSON(t, rows) != `[{"v":"V0_naiVe"}]` {
		t.Fatal(rows, err)
	}
}

func TestSQLErrorsMatchPython(t *testing.T) {
	_, err := runSQL(context.Background(), testConn(t), "SELECT speed FROM results")
	if err == nil || err.Error() != "SQL error: no such column: speed" {
		t.Fatalf("got %v", err)
	}
}

func TestRowCap(t *testing.T) {
	q := "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 500) SELECT i FROM n"
	rows, err := runSQL(context.Background(), testConn(t), q)
	if err != nil || len(rows) != maxRows {
		t.Fatalf("got %d rows, %v", len(rows), err)
	}
}

func TestRunawayQueryIsInterrupted(t *testing.T) {
	q := "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT COUNT(*) FROM n"
	start := time.Now()
	_, err := runSQL(context.Background(), testConn(t), q)
	if err == nil || !strings.Contains(err.Error(), "too expensive") {
		t.Fatalf("got %v", err)
	}
	if d := time.Since(start); d > queryTimeout+time.Second {
		t.Fatalf("took %v", d)
	}
}

func TestInternalQueriesWorkAfterGuardedQuery(t *testing.T) {
	// The authorizer is removed afterwards, so getSchema's PRAGMA keeps working.
	conn := testConn(t)
	_, _ = runSQL(context.Background(), conn, "SELECT 1")
	if _, err := getSchema(conn); err != nil {
		t.Fatal(err)
	}
}

func TestSQLEndpoint(t *testing.T) {
	ts := newTestServer(t)
	post := func(body string) (int, string) {
		resp, err := http.Post(ts.URL+"/sql", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, strings.TrimSpace(string(b))
	}
	if code, body := post(`{"query": "SELECT time_ms FROM results WHERE version = 'cpu'"}`); code != 200 || body != `[{"time_ms":2749.0}]` {
		t.Errorf("select: %d %s", code, body)
	}
	if code, body := post(`{"query": "DELETE FROM results"}`); code != 400 || !strings.Contains(body, "read-only") {
		t.Errorf("delete: %d %s", code, body)
	}
	if code, _ := post(`{"sql": "SELECT 1"}`); code != 400 {
		t.Errorf("wrong field: %d", code)
	}
	if code, _ := post(`not json`); code != 400 {
		t.Errorf("bad json: %d", code)
	}
}

func TestMetricsCountRejectionsByLayer(t *testing.T) {
	ts := newTestServer(t)
	for _, q := range []string{
		"DELETE FROM results",                      // text_check
		"WITH x AS (SELECT 1) DELETE FROM results", // text_check (DELETE keyword)
		"SELECT speed FROM results",                // sql_error: the model can fix it
		"SELECT version FROM results",              // fine
	} {
		resp, err := http.Post(ts.URL+"/sql", "application/json",
			strings.NewReader(mustJSON(t, map[string]string{"query": q})))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
	}
	_, body := get(t, ts, "/metrics")
	for _, want := range []string{
		`bench_api_sql_rejected_total{reason="text_check"} 2`,
		`bench_api_sql_rejected_total{reason="sql_error"} 1`,
		`bench_api_requests_total{route="POST /sql",status="200"} 1`,
		`bench_api_requests_total{route="POST /sql",status="400"} 3`,
		`bench_api_request_duration_seconds_count{route="POST /sql"} 4`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

func TestLoadExtensionDoesNotExist(t *testing.T) {
	// The wasm SQLite build has no load_extension at all, which is stronger than the
	// authorizer denying it (the deny stays in place for other builds).
	_, err := execGuarded(context.Background(), testConn(t), "SELECT load_extension('evil')")
	if err == nil || err.Error() != "SQL error: no such function: load_extension" {
		t.Fatalf("got %v", err)
	}
}

func TestAuthorizerAndReadOnlyReasons(t *testing.T) {
	// Bypass the text check to see which deeper layer fires.
	conn := testConn(t)
	reasonOf := func(q string) string {
		_, err := execGuarded(context.Background(), conn, q)
		var te *ToolError
		if !errorsAs(err, &te) {
			t.Fatalf("%q: expected ToolError, got %v", q, err)
		}
		return te.reason
	}
	for q, want := range map[string]string{
		"DELETE FROM results":          "authorizer",
		"SELECT 1; DROP TABLE results": "multiple_statements",
	} {
		if got := reasonOf(q); got != want {
			t.Errorf("%q: reason %q, want %q", q, got, want)
		}
	}
}
