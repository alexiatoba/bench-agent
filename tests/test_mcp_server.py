"""End-to-end test of the MCP server: start it over stdio like a real client would.
No LLM needed."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _session_calls():
    params = StdioServerParameters(command=sys.executable, args=["-m", "bench_agent.mcp_server"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        names = sorted(t.name for t in (await s.list_tools()).tools)
        ok = await s.call_tool("compare_versions", {"a": "cpu", "b": "v3_separable"})
        bad = await s.call_tool("run_sql", {"query": "DELETE FROM results"})
        count = await s.call_tool("run_sql", {"query": "SELECT COUNT(*) AS n FROM results"})
        return names, ok, bad, count


def test_mcp_server_end_to_end():
    names, ok, bad, count = asyncio.run(_session_calls())
    assert names == ["compare_versions", "get_findings", "get_schema", "run_sql"]
    assert not ok.isError and json.loads(ok.content[0].text)["speedup"] == 1040.5
    assert bad.isError and "read-only" in bad.content[0].text  # readable error for the LLM
    assert '"n": 6' in count.content[0].text  # the DELETE did nothing
