"""Expose the same tools over MCP (Model Context Protocol).  (step 4)

    python -m bench_agent.mcp_server          # stdio server

Then any MCP client can use your tools without your agent code, e.g. Claude
Desktop / Claude Code (`claude mcp add bench -- python -m bench_agent.mcp_server`)
or the MCP Inspector (`npx @modelcontextprotocol/inspector python -m bench_agent.mcp_server`).

The point: tools are written once and reused by any agent. That is what NVIDIA's
"connect LLMs to tools and internal services" means in practice.
"""
from __future__ import annotations

import threading
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as MCPToolError

from . import db, tools

mcp = FastMCP("cuda-blur-bench")
conn = db.connect()
# One shared connection, and run_sql installs an authorizer on it per query: two
# concurrent calls must not interleave and switch each other's authorizer off.
_lock = threading.Lock()


def _call(name: str, **args) -> Any:
    """Run a tool through tools.call_tool (local SQLite, or the Go service when
    BENCH_API_URL is set); a ToolError becomes an MCP error result (isError=True)
    whose text the client's LLM reads, so it can fix its call just like in our own loop."""
    with _lock:
        try:
            return tools.call_tool(conn, name, args)
        except tools.ToolError as e:
            raise MCPToolError(str(e)) from e


@mcp.tool()
def get_schema() -> str:
    """List the database tables and their columns, plus the exact values of the
    version, description and topic columns. Call this before writing SQL."""
    return _call("get_schema")


@mcp.tool()
def get_findings(
    topic: Literal["profiling", "npp", "numerics", "benchmarking", "sanitizer"] | None = None,
) -> list[dict[str, Any]]:
    """Written findings from profiling and testing the CUDA blur kernels: why things
    were fast or slow, NPP behaviour beyond the measured radius, accuracy, slowdowns,
    sanitizer results. These facts are NOT in the results table. Omit the topic
    unless you are sure which one applies."""
    return _call("get_findings", topic=topic)


@mcp.tool()
def run_sql(query: str) -> list[dict[str, Any]]:
    """Run ONE read-only SQLite SELECT (or WITH ... SELECT) query over the benchmark
    tables and return at most 50 rows. Writes, schema changes, ATTACH and PRAGMA are
    rejected. Times are in milliseconds (column time_ms)."""
    return _call("run_sql", query=query)


@mcp.tool()
def compare_versions(a: str, b: str) -> dict[str, Any]:
    """Speed-up of version b over version a, e.g. a='cpu', b='v3_separable'.
    speedup = time_a / time_b, so > 1 means b is faster. An unknown version name
    returns an error listing the valid ones."""
    return _call("compare_versions", a=a, b=b)


if __name__ == "__main__":
    mcp.run()
