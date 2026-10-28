package main

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/ncruces/go-sqlite3"
)

// run_sql's guardrail, ported from bench_agent/tools.py and made stricter. Layers:
//
//  1. text check      friendly messages only; not trusted (comments, casing, WITH ... DELETE)
//  2. one statement   whatever Prepare leaves in `tail` must be empty or comments
//  3. ReadOnly()      SQLite itself says whether the compiled statement writes
//  4. authorizer      allow only SELECT / READ / FUNCTION / RECURSIVE; deny load_extension
//  5. read-only file  the pool's connections are opened with OPEN_READONLY (db.go)
//  6. timeout         the query is interrupted after queryTimeout (runaway recursive CTE)
//
// Layer 1 only explains why; the tests run every attack with it bypassed as well.

const (
	maxRows      = 50
	queryTimeout = 2 * time.Second
)

var (
	writeWords         = regexp.MustCompile(`\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b`)
	commentsAndStrings = regexp.MustCompile(`(?s)--[^\n]*|/\*.*?(\*/|$)|'(?:[^']|'')*'|"(?:[^"]|"")*"`)
	deniedFunctions    = map[string]bool{"load_extension": true}
)

func runSQL(ctx context.Context, conn *sqlite3.Conn, query string) ([]Row, error) {
	ctx, end := sqlSpan(ctx, query)
	rows, err := checkThenExec(ctx, conn, query)
	end(len(rows), err)
	return rows, err
}

func checkThenExec(ctx context.Context, conn *sqlite3.Conn, query string) ([]Row, error) {
	if err := checkReadOnly(query); err != nil {
		return nil, err
	}
	return execGuarded(ctx, conn, query)
}

// checkReadOnly is layer 1: cheap text checks with helpful messages.
func checkReadOnly(query string) error {
	code := commentsAndStrings.ReplaceAllString(query, " ")
	code = strings.ToUpper(strings.TrimSpace(strings.TrimRight(strings.TrimSpace(code), ";")))
	if strings.Contains(code, ";") {
		return rejectf("text_check", "only one statement is allowed")
	}
	if !strings.HasPrefix(code, "SELECT") && !strings.HasPrefix(code, "WITH") {
		return rejectf("text_check", "only read-only SELECT (or WITH ... SELECT) queries are allowed")
	}
	if m := writeWords.FindString(code); m != "" {
		return rejectf("text_check", "%s is not allowed; this database is read-only", m)
	}
	return nil
}

// execGuarded is layers 2-6. It does not rely on checkReadOnly; tests call it directly.
func execGuarded(ctx context.Context, conn *sqlite3.Conn, query string) ([]Row, error) {
	if err := conn.SetAuthorizer(authorize); err != nil {
		return nil, err
	}
	defer conn.SetAuthorizer(nil) // internal queries (PRAGMA table_info) must keep working

	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()
	old := conn.SetInterrupt(ctx)
	defer conn.SetInterrupt(old)

	stmt, tail, err := conn.Prepare(query)
	if err != nil {
		return nil, sqlError(ctx, err)
	}
	if stmt == nil {
		return nil, rejectf("empty", "empty query")
	}
	defer stmt.Close()
	if err := checkTail(conn, tail); err != nil {
		return nil, err
	}
	if !stmt.ReadOnly() {
		return nil, rejectf("not_read_only", "only read-only queries are allowed")
	}
	rows, err := readRows(stmt, maxRows)
	if err != nil {
		return nil, sqlError(ctx, err)
	}
	return rows, nil
}

// checkTail rejects a second statement. Preparing the tail (never running it) is the
// only reliable way to tell "; DROP TABLE x" from a trailing comment or whitespace.
func checkTail(conn *sqlite3.Conn, tail string) error {
	if strings.TrimSpace(tail) == "" {
		return nil
	}
	next, _, err := conn.Prepare(tail)
	if next != nil {
		next.Close()
	}
	if next != nil || err != nil {
		return rejectf("multiple_statements", "only one statement is allowed")
	}
	return nil
}

func authorize(action sqlite3.AuthorizerActionCode, _, name4th, _, _ string) sqlite3.AuthorizerReturnCode {
	switch action {
	case sqlite3.AUTH_SELECT, sqlite3.AUTH_READ, sqlite3.AUTH_RECURSIVE:
		return sqlite3.AUTH_OK
	case sqlite3.AUTH_FUNCTION:
		if deniedFunctions[strings.ToLower(name4th)] {
			return sqlite3.AUTH_DENY
		}
		return sqlite3.AUTH_OK
	}
	return sqlite3.AUTH_DENY
}

// sqlError turns a SQLite error into a ToolError with the same text Python shows
// ("SQL error: no such column: speed"), so the model can fix its query.
func sqlError(ctx context.Context, err error) error {
	if errors.Is(err, sqlite3.INTERRUPT) || ctx.Err() != nil {
		return rejectf("timeout", "query too expensive; add a LIMIT or simplify it")
	}
	var se *sqlite3.Error
	if errors.As(err, &se) {
		msg := strings.TrimPrefix(se.Error(), se.Code().Error()+": ")
		reason := "sql_error" // a mistake the model can fix (bad column, syntax)
		// A denied write comes back as SQLITE_AUTH, a denied function as a plain
		// error saying "not authorized to use function": both are the authorizer.
		if se.Code() == sqlite3.AUTH || strings.HasPrefix(msg, "not authorized") {
			reason = "authorizer"
		}
		return rejectf(reason, "SQL error: %s", msg)
	}
	return rejectf("sql_error", "SQL error: %v", err)
}
