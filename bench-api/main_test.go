package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv, err := newServer("../data", 2)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.routes())
	t.Cleanup(ts.Close)
	return ts
}

func get(t *testing.T, ts *httptest.Server, path string) (int, string) {
	t.Helper()
	resp, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, string(body)
}

func TestSchemaListsTablesAndValues(t *testing.T) {
	code, body := get(t, newTestServer(t), "/schema")
	var s string
	if err := json.Unmarshal([]byte(body), &s); code != 200 || err != nil {
		t.Fatalf("status %d, body %s", code, body)
	}
	for _, want := range []string{
		"results(version, description, image_w, image_h, sigma, radius, time_ms, max_abs_error)",
		"findings(topic, finding)",
		"results.version values: ['cpu', 'v0_naive', 'v1_constant', 'v2_shared', 'v3_separable', 'npp']",
		"findings.topic values: ['profiling', 'npp', 'numerics', 'benchmarking', 'sanitizer']",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("schema missing %q:\n%s", want, s)
		}
	}
}

func TestFindingsFilter(t *testing.T) {
	code, body := get(t, newTestServer(t), "/findings?topic=npp")
	var rows []map[string]string
	if err := json.Unmarshal([]byte(body), &rows); code != 200 || err != nil || len(rows) == 0 {
		t.Fatalf("status %d, body %s", code, body)
	}
	for _, r := range rows {
		if r["topic"] != "npp" {
			t.Errorf("got topic %q", r["topic"])
		}
	}
}

func TestCompareCPUvsSeparable(t *testing.T) {
	code, body := get(t, newTestServer(t), "/compare?a=cpu&b=v3_separable")
	want := `{"a":"cpu","b":"v3_separable","time_a_ms":2749.0,"time_b_ms":2.642,"speedup":1040.5}`
	if code != 200 || strings.TrimSpace(body) != want {
		t.Fatalf("status %d\n got %s\nwant %s", code, body, want)
	}
}

func TestCompareUnknownVersionListsValidOnes(t *testing.T) {
	code, body := get(t, newTestServer(t), "/compare?a=cpu&b=v9_magic")
	if code != 400 || !strings.Contains(body, "unknown version 'v9_magic'") || !strings.Contains(body, "'v3_separable'") {
		t.Fatalf("status %d, body %s", code, body)
	}
}

func TestCompareMissingArgument(t *testing.T) {
	if code, body := get(t, newTestServer(t), "/compare?a=cpu"); code != 400 {
		t.Fatalf("status %d, body %s", code, body)
	}
}

func TestConnectionsAreReadOnly(t *testing.T) {
	path, err := buildDB("../data")
	if err != nil {
		t.Fatal(err)
	}
	p, err := openPool(path, 1)
	if err != nil {
		t.Fatal(err)
	}
	conn := <-p
	// Even trusted code on a pooled connection cannot write: the file is opened read-only.
	if err := conn.Exec("DELETE FROM results"); err == nil || !strings.Contains(err.Error(), "readonly") {
		t.Fatalf("expected a readonly error, got %v", err)
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func errorsAs(err error, target any) bool { return errors.As(err, target) }
