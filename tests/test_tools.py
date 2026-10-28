"""Unit tests for the tools. No LLM, no API key needed: `pytest -q`.

The get_* tests pass out of the box. The run_sql / compare_versions tests fail
until you finish step 1.
"""
import pytest

from bench_agent import db, tools
from bench_agent.tools import ToolError


@pytest.fixture()
def conn():
    return db.connect()


# --------------------------------------------------------------------------- given
def test_schema_lists_tables(conn):
    s = tools.get_schema(conn)
    assert "results(" in s and "findings(" in s


def test_findings_filter(conn):
    rows = tools.get_findings(conn, "npp")
    assert rows and all(r["topic"] == "npp" for r in rows)


def test_unknown_tool_is_tool_error(conn):
    with pytest.raises(ToolError):
        tools.call_tool(conn, "rm_rf", {})


def test_bad_arguments_are_tool_error(conn):
    with pytest.raises(ToolError):
        tools.call_tool(conn, "get_findings", {"nonsense": 1})


# --------------------------------------------------------------------------- run_sql
def test_select_works(conn):
    rows = tools.run_sql(conn, "SELECT version, time_ms FROM results WHERE version = 'v3_separable'")
    assert rows == [{"version": "v3_separable", "time_ms": 2.642}]


def test_with_select_works(conn):
    q = "WITH g AS (SELECT * FROM results WHERE version != 'cpu') SELECT MIN(time_ms) AS best FROM g"
    assert tools.run_sql(conn, q) == [{"best": 2.642}]


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM results",
        "drop table results",
        "UPDATE results SET time_ms = 0",
        "INSERT INTO results (version) VALUES ('x')",
        "SELECT 1; DROP TABLE results",
        "PRAGMA table_info(results)",
        "ATTACH DATABASE 'x.db' AS x",
        "/* harmless */ DELETE FROM results",
        "CREATE TABLE t (a)",
    ],
)
def test_writes_and_tricks_are_rejected(conn, query):
    with pytest.raises(ToolError):
        tools.run_sql(conn, query)
    # and the data is untouched
    assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 6


def test_sql_errors_become_tool_errors(conn):
    with pytest.raises(ToolError, match="no such column"):
        tools.run_sql(conn, "SELECT speed FROM results")


def test_row_cap(conn):
    q = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 500) SELECT i FROM n"
    assert len(tools.run_sql(conn, q)) == tools.MAX_ROWS


# --------------------------------------------------------------------------- compare_versions
def test_compare_cpu_vs_separable(conn):
    r = tools.compare_versions(conn, "cpu", "v3_separable")
    assert r["speedup"] == pytest.approx(1040.5, abs=0.01)
    assert r["time_a_ms"] == 2749.0 and r["time_b_ms"] == 2.642


def test_compare_unknown_version_lists_valid_ones(conn):
    with pytest.raises(ToolError, match="v3_separable"):
        tools.compare_versions(conn, "cpu", "v9_magic")
