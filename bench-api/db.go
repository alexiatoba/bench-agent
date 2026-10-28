package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/ncruces/go-sqlite3"
)

// buildDB loads results.csv and findings.csv into a fresh SQLite file in a temp dir,
// mirroring bench_agent/db.py: the schema comes from the CSV header and values are
// coerced like Python's _coerce ("" -> NULL, then int, then float, else text).
func buildDB(dataDir string) (string, error) {
	dir, err := os.MkdirTemp("", "bench-api-*")
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, "bench.db")
	conn, err := sqlite3.Open(path)
	if err != nil {
		return "", err
	}
	defer conn.Close()
	for _, table := range []string{"results", "findings"} {
		if err := loadCSV(conn, table, filepath.Join(dataDir, table+".csv")); err != nil {
			return "", err
		}
	}
	return path, nil
}

func loadCSV(conn *sqlite3.Conn, table, path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	records, err := csv.NewReader(f).ReadAll()
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if len(records) < 2 {
		return fmt.Errorf("%s is empty", path)
	}
	cols := records[0]
	if err := conn.Exec(fmt.Sprintf(`CREATE TABLE "%s" (%s)`, table, strings.Join(cols, ", "))); err != nil {
		return err
	}
	stmt, _, err := conn.Prepare(fmt.Sprintf(`INSERT INTO "%s" VALUES (%s)`,
		table, strings.TrimSuffix(strings.Repeat("?, ", len(cols)), ", ")))
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, rec := range records[1:] {
		for i, v := range rec {
			if err := bindCoerced(stmt, i+1, v); err != nil {
				return err
			}
		}
		if err := stmt.Exec(); err != nil {
			return err
		}
	}
	return nil
}

func bindCoerced(stmt *sqlite3.Stmt, param int, v string) error {
	if v == "" {
		return stmt.BindNull(param)
	}
	if i, err := strconv.ParseInt(v, 10, 64); err == nil {
		return stmt.BindInt64(param, i)
	}
	if f, err := strconv.ParseFloat(v, 64); err == nil {
		return stmt.BindFloat(param, f)
	}
	return stmt.BindText(param, v)
}

// pool hands out read-only connections; a sqlite3.Conn must not be shared between
// goroutines, and HTTP handlers run concurrently.
type pool chan *sqlite3.Conn

func openPool(path string, n int) (pool, error) {
	p := make(pool, n)
	for range n {
		conn, err := sqlite3.OpenFlags(path, sqlite3.OPEN_READONLY)
		if err != nil {
			return nil, err
		}
		p <- conn
	}
	return p, nil
}

func (p pool) with(ctx context.Context, fn func(*sqlite3.Conn) error) error {
	select {
	case conn := <-p:
		defer func() { p <- conn }()
		return fn(conn)
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Row keeps column order in JSON (a Go map would sort the keys), so responses match
// the Python tools' dicts field for field.
type Row struct {
	cols []string
	vals []any
}

func (r Row) MarshalJSON() ([]byte, error) {
	var b strings.Builder
	b.WriteByte('{')
	for i, c := range r.cols {
		if i > 0 {
			b.WriteByte(',')
		}
		k, _ := json.Marshal(c)
		v, err := json.Marshal(r.vals[i])
		if err != nil {
			return nil, err
		}
		// Go writes the float 2749.0 as "2749"; Python as "2749.0". Keep the float visible.
		if f, ok := r.vals[i].(float64); ok && f == math.Trunc(f) && !strings.ContainsAny(string(v), ".e") {
			v = append(v, ".0"...)
		}
		b.Write(k)
		b.WriteByte(':')
		b.Write(v)
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}

func (r Row) Get(col string) any {
	for i, c := range r.cols {
		if c == col {
			return r.vals[i]
		}
	}
	return nil
}

// readRows steps a prepared statement and collects up to limit rows (limit < 0: all).
func readRows(stmt *sqlite3.Stmt, limit int) ([]Row, error) {
	n := stmt.ColumnCount()
	cols := make([]string, n)
	for i := range n {
		cols[i] = stmt.ColumnName(i)
	}
	rows := []Row{}
	for (limit < 0 || len(rows) < limit) && stmt.Step() {
		vals := make([]any, n)
		for i := range n {
			switch stmt.ColumnType(i) {
			case sqlite3.INTEGER:
				vals[i] = stmt.ColumnInt64(i)
			case sqlite3.FLOAT:
				vals[i] = stmt.ColumnFloat(i)
			case sqlite3.TEXT:
				vals[i] = stmt.ColumnText(i)
			case sqlite3.NULL:
				vals[i] = nil
			default:
				vals[i] = fmt.Sprintf("<%d-byte blob>", len(stmt.ColumnRawBlob(i)))
			}
		}
		rows = append(rows, Row{cols, vals})
	}
	return rows, stmt.Err()
}

// query runs trusted, internal SQL (never model-written) with optional text args.
func query(conn *sqlite3.Conn, sql string, args ...string) ([]Row, error) {
	stmt, _, err := conn.Prepare(sql)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	for i, a := range args {
		if err := stmt.BindText(i+1, a); err != nil {
			return nil, err
		}
	}
	return readRows(stmt, -1)
}
