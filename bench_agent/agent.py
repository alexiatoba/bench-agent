"""The agent: an LLM that answers questions about the benchmark by calling tools.

    python -m bench_agent.agent "How much faster is v3 than the CPU?"
    python -m bench_agent.agent --v0 "same question"     # baseline without tools

v0 (GIVEN): one LLM call with no tools. It will guess numbers or hallucinate,
            which is exactly the point. Keep it as the baseline for your evals.
v1 (TODO, step 2): the tool-calling loop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from openai import OpenAI

from . import db, tools
from .tracing import new_trace_id, span

SYSTEM_PROMPT = """You answer questions about a CUDA Gaussian-blur benchmark.
Use the tools to look up every number; never guess. Times are in milliseconds.
Call get_schema before writing SQL: it lists the exact version names to filter on.
If a query returns no rows, check your filter before concluding the data is missing.
You cannot modify the data; if asked to, say so and still answer the rest of the question.
If the data cannot answer the question, say so. Answer in at most 3 sentences
and quote the numbers you used."""

MAX_STEPS = 6  # hard cap on model<->tool round trips (a guardrail too)
MAX_TOKENS = 4096  # per LLM call: stops runaway hidden "thinking" before the client timeout
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from .env into os.environ (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client() -> tuple[OpenAI, str]:
    load_env()
    base_url = os.environ.get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "meta/llama-3.3-70b-instruct")
    if not api_key:
        sys.exit("Set LLM_API_KEY (see .env.example)")
    # A stalled call (GPU throttled, provider overloaded) fails after 3 minutes instead of hanging.
    return OpenAI(base_url=base_url, api_key=api_key, timeout=180, max_retries=1), model


# --------------------------------------------------------------------------- v0 GIVEN
def answer_v0(question: str, client: OpenAI, model: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You answer questions about a CUDA Gaussian-blur benchmark."},
            {"role": "user", "content": question},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- v1 TODO
def answer(question: str, client: OpenAI, model: str, conn=None) -> dict:
    """Tool-calling loop. Return {"answer": str, "tool_calls": [names...], "steps": int}.

    The loop (step 2 in the README):
      1. messages = [system, user]
      2. call client.chat.completions.create(model, messages, tools=tools.TOOL_SPECS,
         temperature=0)
      3. if the reply has tool_calls:
           - append the assistant message (with its tool_calls) to messages
           - for each call: parse json.loads(call.function.arguments), run
             tools.call_tool(...), append {"role": "tool", "tool_call_id": call.id,
             "content": json.dumps(result)}
           - on tools.ToolError: send the error text back as the tool content
             (the model should correct itself), don't crash
           - go back to 2
         else: it's the final answer -> return it
      4. stop after MAX_STEPS and return a clear "gave up" answer

    Wrap the whole question in span("agent.answer", trace_id, question=...),
    each LLM call in span("llm.call", ...) and each tool call in
    span("tool." + name, ...), so traces.jsonl shows what happened.
    """
    conn = conn or db.connect()
    trace_id = new_trace_id()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    used: list[str] = []

    with span("agent.answer", trace_id, question=question, model=model) as root:
        for step in range(1, MAX_STEPS + 1):
            with span("llm.call", trace_id, step=step, n_messages=len(messages)) as attrs:
                resp = client.chat.completions.create(
                    model=model, messages=messages, tools=tools.TOOL_SPECS, temperature=0,
                    max_tokens=MAX_TOKENS,
                )
                msg = resp.choices[0].message
                attrs["finish_reason"] = resp.choices[0].finish_reason
                attrs["tool_calls"] = [c.function.name for c in msg.tool_calls or []]
                if resp.usage:
                    attrs["tokens_in"] = resp.usage.prompt_tokens
                    attrs["tokens_out"] = resp.usage.completion_tokens

            if not msg.tool_calls:
                root.update(steps=step, tool_calls=used, outcome="answered")
                return {"answer": msg.content or "", "tool_calls": used, "steps": step}

            # Echo the assistant turn back so each tool result can reference its call id.
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in msg.tool_calls
                ],
            })
            for call in msg.tool_calls:
                name = call.function.name
                used.append(name)
                with span("tool." + name, trace_id, arguments=call.function.arguments) as attrs:
                    try:
                        args = json.loads(call.function.arguments or "{}") or {}
                        content = json.dumps(tools.call_tool(conn, name, args), default=str)
                    except (tools.ToolError, json.JSONDecodeError) as e:
                        # Not a crash: hand the error to the model so it can fix its call.
                        attrs["tool_error"] = str(e)
                        content = json.dumps({"error": str(e)})
                    attrs["result"] = content[:500]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

        root.update(steps=MAX_STEPS, tool_calls=used, outcome="gave_up")
        return {
            "answer": f"I gave up after {MAX_STEPS} steps without reaching an answer.",
            "tool_calls": used,
            "steps": MAX_STEPS,
        }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--v0", action="store_true", help="baseline: no tools")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 (mangles "×")
    client, model = make_client()
    if a.v0:
        print(answer_v0(a.question, client, model))
    else:
        out = answer(a.question, client, model)
        print(out["answer"])
        print(f"\n[tools used: {out['tool_calls']}, steps: {out['steps']}]")


if __name__ == "__main__":
    main()
