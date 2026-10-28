"""Evaluate agent quality on evals/questions.yaml  (step 3).

    python -m evals.run_evals            # v1 agent (tools)
    python -m evals.run_evals --v0       # baseline, no tools

Prints a table: case, pass/fail, which checks failed, tools used, steps, latency.
Write the final summary (v0 vs v1 pass rate, average latency and steps) into the
README results table. That comparison is the core result of this project.

GIVEN: loading cases, running the agent, timing.
TODO:  grade() and the summary.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import yaml

from bench_agent import agent, db, tools

CASES = Path(__file__).with_name("questions.yaml")
NUMBER = re.compile(r"^\d+(\.\d+)?$")
# Phrases that mean "the data does not cover this". Kept deliberately explicit so a
# confident made-up number never counts as a refusal.
REFUSAL = re.compile(
    r"(not|no|n't|cannot|unable to)\b[^.]{0,60}\b(data|dataset|available|include[sd]?|cover(s|ed)?|"
    r"measured|benchmark(ed|s)?|tested|information|results?|recorded)"
    r"|\bonly\b[^.]{0,40}\b(measured|tested|benchmarked|available|includes?|contains?|covers?)",
    re.I,
)


def contains_number(answer: str, number: str) -> bool:
    """'1040' matches '1,040' and '1040.5' but not '10400' or '21040'.
    '1.12' also matches '1.121' (more precision is fine) but not '11.12'."""
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", answer)  # drop thousands separators
    tail = r"(?!\d)" if "." not in number else ""
    return re.search(rf"(?<![\d.]){re.escape(number)}{tail}", text) is not None


def grade(case: dict, answer: str, tool_calls: list[str], conn, v0: bool = False) -> list[str]:
    """Return a list of failed checks (empty list = pass).

    Checks to implement:
      - every string in must_contain appears in the answer (case-insensitive;
        careful: "1040" should also match "1,040" or "1040.5")
      - nothing in must_not_contain appears
      - expect_tool (if set) is in tool_calls  (skip this check for --v0)
      - refusal: the answer says the data does not cover it (decide how to detect)
      - data_unchanged: results still has 6 rows
    """
    failed = []
    low = answer.lower()
    for item in case.get("must_contain") or []:
        options = [str(s) for s in item] if isinstance(item, list) else [str(item)]  # list = any of
        if not any(contains_number(answer, s) if NUMBER.match(s) else s.lower() in low for s in options):
            failed.append(f"missing {options[0] if len(options) == 1 else options}")
    for s in case.get("must_not_contain") or []:
        if str(s).lower() in low:
            failed.append(f"contains {s!r}")
    if case.get("expect_tool") and not v0 and case["expect_tool"] not in tool_calls:
        failed.append(f"did not call {case['expect_tool']}")
    if case.get("refusal") and not REFUSAL.search(answer):
        failed.append("did not say the data doesn't cover it")
    if case.get("data_unchanged"):
        # Through call_tool, so it checks whichever backend the agent used (local or Go).
        n = tools.call_tool(conn, "run_sql", {"query": "SELECT COUNT(*) AS n FROM results"})[0]["n"]
        if n != 6:
            failed.append(f"results has {n} rows, expected 6")
    return failed


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--v0", action="store_true")
    p.add_argument("--show", action="store_true", help="print every answer")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # live progress when piped

    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    client, model = agent.make_client()
    # Warm-up so the first case's latency doesn't include loading the model onto the GPU.
    client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=1)
    rows = []
    for case in cases:
        conn = db.connect()
        t0 = time.perf_counter()
        try:
            if args.v0:
                answer, tool_calls, steps = agent.answer_v0(case["question"], client, model), [], 1
            else:
                out = agent.answer(case["question"], client, model, conn=conn)
                answer, tool_calls, steps = out["answer"], out["tool_calls"], out["steps"]
            failed = None
        except Exception as e:  # one stuck case (LLM timeout, service down) must not end the run
            answer, tool_calls, steps = "", [], 0
            failed = [f"crashed: {type(e).__name__}: {e}"]
        latency = time.perf_counter() - t0
        failed = failed or grade(case, answer, tool_calls, conn, v0=args.v0)
        rows.append((case["id"], not failed, failed, tool_calls, steps, latency))
        print(f"{'PASS' if not failed else 'FAIL'}  {case['id']:<24} {latency:5.1f}s  {failed or ''}")
        if args.show or failed:
            print(f"      tools={tool_calls} steps={steps}\n      {answer.strip()[:400]!r}")

    n = len(rows)
    passed = sum(ok for _, ok, *_ in rows)
    mean_latency = sum(r[5] for r in rows) / n
    mean_steps = sum(r[4] for r in rows) / n
    name = "v0 (no tools)" if args.v0 else "v1 (tools)"
    print(f"\n{name}: {passed}/{n} passed ({passed / n:.0%}), "
          f"mean latency {mean_latency:.1f}s, mean steps {mean_steps:.1f}")
    print(f"| {name} | {model} | {passed}/{n} ({passed / n:.0%}) | {mean_latency:.1f} s | {mean_steps:.1f} |")


if __name__ == "__main__":
    main()
