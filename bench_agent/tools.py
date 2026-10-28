"""Tools the agent can call.

Each tool is a plain Python function plus a JSON schema (TOOL_SPECS) that tells
the LLM what the tool does and which arguments it takes. Keep tools small,
deterministic and testable: the LLM decides WHEN to call them, the tools decide
WHAT is true.

Status:
  get_schema         GIVEN
  get_findings       GIVEN
  run_sql            TODO (step 1): read-only guardrail
  compare_versions   TODO (step 1): speed-up between two versions
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import db

MAX_ROWS = 50
MAX_VM_STEPS = 5_000_000  # abort runaway queries (e.g. an unbounded recursive CTE)

# Layer 1 (keyword check): mostly there to give the model a clear error message.
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b"
)
_COMMENTS_AND_STRINGS = re.compile(r"--[^\n]*|/\*.*?(\*/|$)|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.S)

# Layer 2 (authorizer): SQLite itself asks us about every action it compiles.
# Only these are allowed; anything else (writes, schema changes, ATTACH, PRAGMA,
# transactions) is denied, however the SQL text is spelled.
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension"}


class ToolError(Exception):
    """Raised for bad tool input. The message is sent back to the LLM so it can retry."""


# --------------------------------------------------------------------------- GIVEN
MAX_LISTED_VALUES = 12


def get_schema(conn: sqlite3.Connection) -> str:
    """Tables and columns, plus the values of small text columns.

    Without the values, the model guesses filters like version = 'separable' or
    description LIKE '%GPU%', gets zero rows, and concludes the data is missing.
    """
    lines = [db.schema(conn)]
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for col in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
            values = conn.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" WHERE typeof("{col}") = \'text\' '
                f"LIMIT {MAX_LISTED_VALUES + 1}"
            ).fetchall()
            # Only short categorical columns (version, description, topic), not free text.
            if values and len(values) <= MAX_LISTED_VALUES and all(len(v[0]) <= 80 for v in values):
                lines.append(f"{table}.{col} values: {[v[0] for v in values]}")
    return "\n".join(lines)


def get_findings(conn: sqlite3.Connection, topic: str | None = None) -> list[dict[str, Any]]:
    if topic:
        rows = conn.execute("SELECT topic, finding FROM findings WHERE topic = ?", (topic,))
    else:
        rows = conn.execute("SELECT topic, finding FROM findings")
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- TODO
def run_sql(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    """Run a READ-ONLY SQL query and return at most MAX_ROWS rows as dicts.

    This is your guardrail: the LLM writes the SQL, so assume it can be wrong or
    malicious (prompt injection). Requirements (see tests/test_tools.py):
      - allow a single SELECT (or WITH ... SELECT) statement only
      - reject anything that writes or changes schema: INSERT, UPDATE, DELETE,
        DROP, ALTER, CREATE, ATTACH, PRAGMA, ... -> raise ToolError
      - reject multiple statements ("SELECT 1; DROP TABLE results") -> ToolError
      - turn sqlite3 errors (bad column, syntax) into ToolError with the message,
        so the model can fix its query and try again
      - cap the result at MAX_ROWS rows

    Hints: keyword checks alone are easy to fool (comments, casing). A sturdier
    second layer is `conn.set_authorizer(...)` or opening the DB read-only.
    Do both and explain why in the README.
    """
    _check_read_only(query)

    def authorizer(action, arg1, arg2, db_name, trigger):
        if action not in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and arg2 and arg2.lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    steps = 0

    def progress():
        nonlocal steps
        steps += 1000
        return steps > MAX_VM_STEPS  # non-zero return interrupts the query

    conn.set_authorizer(authorizer)
    conn.set_progress_handler(progress, 1000)
    try:
        cur = conn.execute(query)
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, row)) for row in cur.fetchmany(MAX_ROWS)]
    except sqlite3.Error as e:
        if steps > MAX_VM_STEPS:
            raise ToolError("query too expensive; add a LIMIT or simplify it") from e
        raise ToolError(f"SQL error: {e}") from e
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)


def _check_read_only(query: str) -> None:
    """Layer 1: cheap text checks with helpful messages. Not trusted on its own."""
    # Blank out comments and string literals so "/* x */ DELETE" or "'; DROP'" can't hide.
    code = _COMMENTS_AND_STRINGS.sub(" ", query).strip().rstrip(";").strip().upper()
    if ";" in code:
        raise ToolError("only one statement is allowed")
    if not (code.startswith("SELECT") or code.startswith("WITH")):
        raise ToolError("only read-only SELECT (or WITH ... SELECT) queries are allowed")
    if m := _WRITE_WORDS.search(code):
        raise ToolError(f"{m.group(1)} is not allowed; this database is read-only")


def compare_versions(conn: sqlite3.Connection, a: str, b: str) -> dict[str, Any]:
    """Return {"a": a, "b": b, "time_a_ms": ..., "time_b_ms": ..., "speedup": time_a / time_b}.

    "speedup" > 1 means b is faster than a. Round speedup to 2 decimals.
    Raise ToolError if a version does not exist, and list the valid versions in
    the message so the model can recover.
    """
    times = {}
    for v in (a, b):
        rows = conn.execute("SELECT time_ms FROM results WHERE version = ?", (v,)).fetchall()
        if not rows:
            valid = [r[0] for r in conn.execute("SELECT DISTINCT version FROM results")]
            raise ToolError(f"unknown version {v!r}; valid versions: {valid}")
        if len(rows) > 1:
            # Several configs (sizes/sigmas) per version: picking one silently could
            # compare different configs, so let the model filter with run_sql instead.
            raise ToolError(f"{v!r} has {len(rows)} configurations; use run_sql to compare a specific one")
        times[v] = rows[0][0]
    return {
        "a": a,
        "b": b,
        "time_a_ms": times[a],
        "time_b_ms": times[b],
        "speedup": round(times[a] / times[b], 2),
    }


# --------------------------------------------------------------------------- registry
TOOLS = {
    "get_schema": get_schema,
    "get_findings": get_findings,
    "run_sql": run_sql,
    "compare_versions": compare_versions,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "List the database tables and their columns. Call this before writing SQL.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_findings",
            "description": "Written findings from profiling and testing the kernels: why things were fast or "
                           "slow, NPP behaviour beyond the measured radius, accuracy, slowdowns, sanitizer "
                           "results. Facts here are NOT in the results table. Omit the topic unless sure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": ["profiling", "npp", "numerics", "benchmarking", "sanitizer"],
                        "description": "Optional topic filter.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Run one read-only SQLite SELECT query over the benchmark tables.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_versions",
            "description": "Speed-up of version b over version a (e.g. a='cpu', b='v3_separable').",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a", "b"],
            },
        },
    },
]


def call_tool(conn: sqlite3.Connection, name: str, args: dict[str, Any]) -> Any:
    """Dispatch a tool call coming from the model.

    With BENCH_API_URL set (e.g. http://localhost:8080), the call goes to the Go
    service in bench-api/ instead of the local SQLite connection. Same results,
    same error messages, so the agent and the evals don't change.
    """
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}; available: {sorted(TOOLS)}")
    try:
        # Validate the arguments against the local signature in both modes, so a bad call
        # from the model gives the same message whichever backend answers.
        inspect.signature(TOOLS[name]).bind(conn, **args)
    except TypeError as e:  # wrong / missing arguments from the model
        raise ToolError(f"bad arguments for {name}: {e}") from e
    if api_url := os.environ.get("BENCH_API_URL"):
        return _call_remote(api_url.rstrip("/"), name, args)
    return TOOLS[name](conn, **args)


# --------------------------------------------------------------------------- remote (Go)
def _call_remote(base: str, name: str, args: dict[str, Any]) -> Any:
    if name == "run_sql":
        req = urllib.request.Request(
            f"{base}/sql", data=json.dumps({"query": args["query"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
    else:
        path, params = {
            "get_schema": ("/schema", {}),
            "get_findings": ("/findings", {"topic": args.get("topic") or ""}),
            "compare_versions": ("/compare", {"a": args.get("a"), "b": args.get("b")}),
        }[name]
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        req = urllib.request.Request(f"{base}{path}" + (f"?{query}" if query else ""))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 400:  # the Go ToolError: the model can fix its call
            raise ToolError(json.load(e)["error"]) from e
        raise ToolError(f"tool service error (HTTP {e.code}); try again later") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"tool service unavailable at {base}; the data cannot be queried") from e
