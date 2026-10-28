package main

import (
	"fmt"
	"math"
	"strings"

	"github.com/ncruces/go-sqlite3"
)

// The same tools as bench_agent/tools.py, with the same outputs and error messages,
// so the LLM cannot tell which backend answered.

const maxListedValues = 12

// ToolError is bad input from the model; its message goes back to the model (HTTP 400).
// reason is set for run_sql rejections and becomes a metric label (metrics.go).
type ToolError struct{ msg, reason string }

func (e *ToolError) Error() string { return e.msg }

func toolErrorf(format string, a ...any) error { return &ToolError{msg: fmt.Sprintf(format, a...)} }

func rejectf(reason, format string, a ...any) error {
	return &ToolError{msg: fmt.Sprintf(format, a...), reason: reason}
}

func getSchema(conn *sqlite3.Conn) (string, error) {
	tables, err := query(conn, "SELECT name FROM sqlite_master WHERE type='table'")
	if err != nil {
		return "", err
	}
	var lines, values []string
	for _, t := range tables {
		name := t.Get("name").(string)
		info, err := query(conn, fmt.Sprintf(`PRAGMA table_info("%s")`, name))
		if err != nil {
			return "", err
		}
		cols := make([]string, len(info))
		for i, c := range info {
			cols[i] = c.Get("name").(string)
		}
		lines = append(lines, fmt.Sprintf("%s(%s)", name, strings.Join(cols, ", ")))

		// Like Python: list the values of short categorical text columns.
		for _, col := range cols {
			rows, err := query(conn, fmt.Sprintf(
				`SELECT DISTINCT "%s" AS v FROM "%s" WHERE typeof("%s") = 'text' LIMIT %d`,
				col, name, col, maxListedValues+1))
			if err != nil {
				return "", err
			}
			if len(rows) == 0 || len(rows) > maxListedValues {
				continue
			}
			vals := make([]string, len(rows))
			short := true
			for i, r := range rows {
				vals[i] = r.Get("v").(string)
				short = short && len([]rune(vals[i])) <= 80
			}
			if short {
				values = append(values, fmt.Sprintf("%s.%s values: %s", name, col, pyList(vals)))
			}
		}
	}
	return strings.Join(append(lines, values...), "\n"), nil
}

func getFindings(conn *sqlite3.Conn, topic string) ([]Row, error) {
	if topic != "" {
		return query(conn, "SELECT topic, finding FROM findings WHERE topic = ?", topic)
	}
	return query(conn, "SELECT topic, finding FROM findings")
}

func compareVersions(conn *sqlite3.Conn, a, b string) (Row, error) {
	times := map[string]any{}
	for _, v := range []string{a, b} {
		rows, err := query(conn, "SELECT time_ms FROM results WHERE version = ?", v)
		if err != nil {
			return Row{}, err
		}
		if len(rows) == 0 {
			valid, err := query(conn, "SELECT DISTINCT version FROM results")
			if err != nil {
				return Row{}, err
			}
			names := make([]string, len(valid))
			for i, r := range valid {
				names[i] = r.Get("version").(string)
			}
			return Row{}, toolErrorf("unknown version %s; valid versions: %s", pyStr(v), pyList(names))
		}
		if len(rows) > 1 {
			return Row{}, toolErrorf("%s has %d configurations; use run_sql to compare a specific one", pyStr(v), len(rows))
		}
		times[v] = rows[0].Get("time_ms")
	}
	ta, tb := toFloat(times[a]), toFloat(times[b])
	if tb == 0 {
		return Row{}, toolErrorf("%s has time 0; speed-up is undefined", pyStr(b))
	}
	return Row{
		cols: []string{"a", "b", "time_a_ms", "time_b_ms", "speedup"},
		vals: []any{a, b, times[a], times[b], math.Round(ta/tb*100) / 100},
	}, nil
}

func toFloat(v any) float64 {
	switch x := v.(type) {
	case int64:
		return float64(x)
	case float64:
		return x
	}
	return math.NaN()
}

// pyStr and pyList format like Python's repr(), so messages match tools.py exactly.
func pyStr(s string) string {
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		return `"` + s + `"`
	}
	return "'" + strings.ReplaceAll(strings.ReplaceAll(s, `\`, `\\`), "'", `\'`) + "'"
}

func pyList(items []string) string {
	q := make([]string, len(items))
	for i, s := range items {
		q[i] = pyStr(s)
	}
	return "[" + strings.Join(q, ", ") + "]"
}
