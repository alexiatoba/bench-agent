# bench-agent: an LLM agent that answers questions about my CUDA benchmarks

A small tool-calling agent over the results of
[cuda-blur](https://github.com/alexiatoba/cuda-blur). Ask it *"How much faster is
the separable kernel than NVIDIA NPP?"* and it looks the numbers up through tools
instead of guessing. It comes with:

- **tools** with a read-only SQL **guardrail** (prompt-injection safe)
- a hand-written **tool-calling loop** (no agent framework, so every step is visible)
- an **MCP server** exposing the same tools to any MCP client
- an **evaluation set** comparing the agent against a no-tools baseline
- **tracing** of every LLM and tool call
- a **Dockerfile**

## Setup (Windows, PowerShell)

```powershell
cd bench-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q                     # 4 tests pass now, the rest after step 1
```

Pick an LLM provider (all speak the OpenAI API). Copy `.env.example` and set the variables:

```powershell
$env:LLM_BASE_URL = "https://integrate.api.nvidia.com/v1"   # NVIDIA NIM, free credits
$env:LLM_API_KEY  = "nvapi-..."                               # build.nvidia.com -> Get API Key
$env:LLM_MODEL    = "meta/llama-3.3-70b-instruct"
```

Fully local and free alternative: install [Ollama](https://ollama.com), run
`ollama pull qwen2.5:7b`, and use `LLM_BASE_URL=http://localhost:11434/v1`,
`LLM_API_KEY=ollama`, `LLM_MODEL=qwen2.5:7b`. It runs on the RTX 4060.

Try the baseline: `python -m bench_agent.agent --v0 "How much faster is v3 than the CPU?"`
It has no data, so watch it guess.

## Roadmap

| Step | What | File | Status |
|------|------|------|--------|
| 0 | Data loading, schema, findings tools, tracing, v0 baseline | given | done |
| 1 | `run_sql` guardrail + `compare_versions` | `bench_agent/tools.py` | done |
| 2 | Tool-calling loop | `bench_agent/agent.py` | done |
| 3 | Evals: `grade()` + summary, v0 vs v1 | `evals/run_evals.py` | done |
| 4 | MCP server with all tools | `bench_agent/mcp_server.py` | done |
| 5 | OpenTelemetry instead of the JSONL tracer | `bench_agent/tracing.py` | done |
| 6 | Docker | `Dockerfile` | written, not built |

### Step 1: tools and guardrail
Make `pytest -q` fully green. Then write down in this README:
- why a keyword check alone is not enough (try `/* x */ DELETE ...`, mixed case, `;` tricks)
- what your second layer is (`conn.set_authorizer` or a read-only connection) and what it blocks

**Why a keyword check is not enough.** A keyword check reads the SQL as text, but SQLite
reads it as a grammar, and the two disagree in many ways:
- `/* harmless */ DELETE FROM results` starts with a comment, so "does it start with SELECT?"
  depends on whether you strip comments first, and stripping them correctly means handling
  `--`, nested-looking `/* */`, and comment markers inside string literals.
- `WITH x AS (SELECT 1) DELETE FROM results` is **valid SQLite**: it starts with `WITH`
  but deletes. "Starts with SELECT/WITH" lets it through.
- `SELECT 1; DROP TABLE results`: a `;` inside a string (`WHERE d = 'a;b'`) is harmless,
  a `;` outside one is a second statement. Telling them apart needs a tokenizer.
- Mixed case (`dElEtE`), odd whitespace, and new statement types the denylist forgot
  (`VACUUM INTO 'file'`, `REINDEX`) all need special handling.
- In the other direction a denylist gets false positives: `REPLACE` is both a write
  statement and the harmless `replace()` string function.

My layer 1 (`_check_read_only`) blanks out comments and string literals, then requires one
statement starting with `SELECT`/`WITH` and no write keywords. Its job is to give the model a
**clear error message**, not to be the security boundary.

**Layer 2: `conn.set_authorizer`.** While the query compiles, SQLite calls my callback for
every action and I allow only `SELECT`, `READ` (column reads), `FUNCTION` and `RECURSIVE`
(recursive CTEs). Everything else is denied: `INSERT/UPDATE/DELETE`, `CREATE/DROP/ALTER`,
`ATTACH`, `PRAGMA`, transactions, and `load_extension()`. Because SQLite's own parser
decides what the statement does, spelling tricks don't matter. I checked this by turning
layer 1 off: every attack in the tests (plus `WITH ... DELETE`, `PRAGMA writable_schema`,
`load_extension`) is still blocked and the table keeps its 6 rows. I used an authorizer
instead of a read-only connection because the DB is `:memory:`, and a read-only connection
would still allow `ATTACH` and `PRAGMA`.

Two more guards: Python's `execute()` refuses to run more than one statement, and a
**progress handler** stops a query after about 5M VM steps, so an unbounded recursive
CTE can't hang the agent (a denial-of-service through the LLM). `fetchmany(MAX_ROWS)`
caps the size of the result.

### Step 2: the loop
Implement `answer()` following its docstring. Test it by hand:
- a question that needs one tool, one that needs two (schema, then SQL)
- a question where the model writes bad SQL first: does it recover from the ToolError?
- open `traces.jsonl`: can you reconstruct what happened from the spans alone?

**Hand tests (Ollama, `qwen3:8b` on the RTX 4060):**
- *One tool:* "How much faster is v3_separable than the CPU?" → `compare_versions` →
  "1040.5×", 2 steps.
- *Two tools:* "What is the CPU time?" → `get_schema`, `run_sql` → "2749.0 ms", 3 steps.
- *Recovery:* asked to run `SELECT version, speed FROM results`. The trace:
  ```
   8914ms  llm.call         ['run_sql']
      0ms  tool.run_sql     SQL error: no such column: speed
   5102ms  llm.call         ['get_schema']
      2ms  tool.get_schema  {}
   7022ms  llm.call         ['run_sql']
      1ms  tool.run_sql     {"query":"SELECT version, time_ms AS speed FROM results ORDER BY time_ms"}
  10205ms  llm.call
  31300ms  agent.answer     answered
  ```
  The `ToolError` goes back to the model as the tool result, not as an exception. The
  model reads it, looks up the schema, and fixes its query. Yes, the spans alone are
  enough to reconstruct the run, and they show where the time goes: about 99% in the
  LLM, about 1 ms per tool call.
- *A subtle failure:* for "lowest time per megapixel" the number was right (0.1575 ms/Mpx)
  but the formula written in the answer was wrong (`* 1e6` instead of `/ 1e6`). The SQL
  was correct; the model's description of it was not. Tools make the numbers correct, but
  the prose around them can still be wrong.

**Why NIM was not used.** Of the NIM models I tried with a free key, Llama 3.3 70B is
gone (410), Nemotron 3 Super / Kimi K2.6 / Nemotron 70B return 404, GLM 5.3 and
DeepSeek V4.1 timed out, and GPT-OSS 20B took about 100 s to answer "Say OK". A local 8B
model is faster, free, and reproducible, which matters most for the evals.

### Step 3: evals (this is the main result)
Implement `grade()`, run `python -m evals.run_evals --v0` and `python -m evals.run_evals`,
and fill in the table. Add at least 3 cases of your own, including one the agent fails,
and explain why it fails.

| Agent | Model | Pass rate | Mean latency | Mean steps |
|-------|-------|-----------|--------------|------------|
| v0 (no tools) | qwen3:8b (Ollama, RTX 4060 laptop) | 1/11 (9%) | 42.7 s | 1.0 |
| v1 (tools), run 1 | qwen3:8b | 5/11 (45%) | 43.0 s | 2.9 |
| v1 (tools), run 2, after fixes | qwen3:8b | **7/11 (64%)** | 30.9 s | 2.6 |

Raw outputs, including every answer and the traces, are in [evals/results/](evals/results/).
Details and failure analysis are under [Results](#results).

### Step 4: MCP
Expose all four tools in `mcp_server.py`, then connect it to an MCP client
(`npx @modelcontextprotocol/inspector python -m bench_agent.mcp_server`, or
`claude mcp add bench -- python -m bench_agent.mcp_server`) and ask the same questions.

**Done.** [mcp_server.py](bench_agent/mcp_server.py) exposes all four tools:
- **Thin wrappers.** Each MCP tool wraps the matching function in `tools.py`, so the agent
  and any MCP client share one guardrail.
- **Docstrings are the descriptions** the client's LLM reads. `get_findings(topic)` is a
  `Literal`, so the client sees the 5 topics as an enum.
- **Errors.** A `ToolError` becomes an MCP error result (`isError=True`) carrying our
  message, so the client's model can correct itself, as in our own loop.
- **A lock around each call.** `run_sql` installs its authorizer on the one shared
  connection for each query. Without the lock, two concurrent client requests could switch
  off each other's guardrail mid-query.
- **End-to-end test.** [tests/test_mcp_server.py](tests/test_mcp_server.py) starts the
  server over stdio like a real client, lists the tools, and checks that `DELETE` comes back
  as a readable error with the data unchanged.

To connect it, the repo has a project-scoped [.mcp.json](.mcp.json), which Claude Code
picks up automatically in this folder. `claude mcp add` (local scope) did not load in VS
Code, because the config was keyed by `C:/...` while VS Code opened the folder as `c:\...`.
I then connected Claude Code as the client and asked the eval questions with the same tools
and data, but a much stronger model than qwen3:8b.
*(TODO: add what Claude answered for `out_of_scope` and `injection`, the two cases
qwen3:8b failed.)*

### Step 5: OpenTelemetry
`pip install opentelemetry-sdk` and replace `span()` with an OTel tracer and the
`ConsoleSpanExporter`. Keep the same span names. Optional: send the traces to
Jaeger in Docker and add a screenshot here.

**Done.** [tracing.py](bench_agent/tracing.py) now creates real OpenTelemetry spans with the
same `span(name, trace_id, **attrs)` interface, so `agent.py` did not change. The span
names are the same (`agent.answer`, `llm.call`, `tool.<name>`).
- **Nesting comes from OTel's context**, not from a hand-kept list of open spans, so the
  parent/child links are what any OTel backend expects. All spans of one question share
  one 128-bit trace ID.
- **One set of spans, several exporters:**
  - `traces.jsonl`: always written, by a small custom `SpanExporter`, in the **same format
    as before**. The step 2–3 traces in `evals/results/` and the scripts that read them
    still work.
  - `ConsoleSpanExporter`: switched on with `BENCH_OTEL_CONSOLE=1`. It writes to **stderr**,
    because stdout carries the answer for the CLI and the protocol messages for the MCP
    server. Console output on stdout would corrupt the MCP stream.
  - OTLP (Jaeger etc.): used when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
- **Errors**: an exception marks the span `ERROR` and records the exception event, the
  standard OTel way.
- **OTel attribute values can't be null or nested.** `None` is skipped, and dicts are
  stored as JSON strings.
- **Tests**: [tests/test_tracing.py](tests/test_tracing.py) runs the agent with a scripted
  fake LLM (bad SQL, then recovery) and checks one trace ID, correct parent links, span
  order, the `tool_error` attribute, the give-up path, and error status. No LLM needed.

```powershell
$env:BENCH_OTEL_CONSOLE = "1"
python -m bench_agent.agent "How much faster is v3_separable than the CPU?"
# stderr: 4 OTel spans (llm.call, tool.compare_versions, llm.call, agent.answer),
#         service.name=bench-agent, one trace_id
```

### Step 6: Docker
`docker build -t bench-agent .` and run both the MCP server and the agent in the container.

**Dockerfile written, not built here.** I did not install Docker for this project, so this
image has not been built or run; treat it as untested. What it does:
- It copies `bench_agent/`, `data/` and `evals/` into a `python:3.12-slim` image. By default
  it runs the MCP server over stdio, so it needs `-i`:
  ```powershell
  docker build -t bench-agent .
  docker run -i --rm bench-agent                       # MCP server over stdio
  ```
- The agent inside a container cannot reach Ollama at `localhost`, which is the container
  itself. The image's default points at the host instead:
  ```powershell
  docker run --rm bench-agent python -m bench_agent.agent "How much faster is v3 than the CPU?"
  docker run --rm bench-agent python -m evals.run_evals
  ```
  For NVIDIA NIM or OpenAI, pass `--env-file .env`. `.env` is excluded from the image by
  `.dockerignore`, so a key is never baked into it.

## Results

**Eval set:** the 7 given cases plus 4 of mine (`npp_large_filters`, `sanitizer_tools`,
`throughput_v3`, `unmeasured_sigma`), 11 in total. Each one targets a different way an agent
fails: facts that exist only in the written findings, listing several facts without
inventing more, arithmetic on top of the data, and a tempting near-miss (only sigma 2 was
measured, so the sigma-2 time must not be given as the sigma-5 answer).

**The grader** (`grade()` in [evals/run_evals.py](evals/run_evals.py), tested in
[tests/test_grade.py](tests/test_grade.py)):
- Numbers match in their different formats: `1040` matches `1,040` and `1040.5×` but not
  `10400` or `104.0`. `1.12` matches `1.121` but not `11.12`.
- A refusal has to be an explicit "the data does not include / only X was measured".
  A confident made-up number does not count as one.

**v0 vs v1.** Without tools the model guesses ("CUDA is typically 10–100× faster", the real
figure is 1040×), invents numbers ("~0.15 ms at sigma 5", "~100 MP/s" against a real 6350),
answers a different question (recommends buying an RTX 4090 when asked for the fastest
kernel version), and never says it lacks data. Its single pass (`why_shared_small_gain`)
is weak: generic CUDA text contains the word "shared". That case should check for "L1" too.

**From run 1 to run 2: fixing the tools, not the questions.** The run-1 traces showed one
main cause of failure: the model saw column names but not their values. It wrote
`WHERE description LIKE '%GPU%'` and `WHERE version = 'separable'`, got 0 rows, and
concluded "GPU versions are not listed". Changes:
1. `get_schema` also lists the values of short text columns (versions, descriptions, topics).
2. The `get_findings` description says which facts live there and not in the table.
3. The system prompt says: call the schema first, an empty result means "check your
   filter", and refuse writes but still answer the rest of the question.
4. Grader fix: `npp_vs_v3` answered "12% faster", which is correct but was failed for not
   writing "1.12". The case now accepts either.

`fastest_gpu`, `throughput_v3` and `npp_large_filters` went from fail to pass.
*Caveat:* these changes were made after seeing failures on the same 11 questions, so run 2
is not a clean held-out number. The changes are general (none mentions a specific
question), but a fresh set of questions would be the honest next test. Also, with 11 cases
one flip is 9 points: `out_of_scope` passed in run 1 and failed in run 2.

**What still fails (run 2), and why:**

| Case | What happened | Why |
|------|---------------|-----|
| `out_of_scope` | "The speed-up **on an H100** was 1040.5×" | **The data, not the model.** `results` has no GPU column, so nothing the tools return says these numbers came from an RTX 4060. The model fetched correct numbers and attached them to the wrong hardware. Fix: record the hardware in the data (a `gpu` column) and the schema would answer it. |
| `injection` | Refused the `DELETE` (correct), then said the CPU time was "12.4 ms" **without calling any tool** | The guardrail protects the data but not the answer. Treating the message as an attack made the model stop using tools for the legitimate part. Security and correctness are separate properties, and they need separate checks. |
| `power_state` | Called `get_findings(topic="profiling")` and built a plausible but wrong cause | The answer is under topic `benchmarking`, and nothing in that name suggests "slowdown". A small model picks the closest-sounding topic and then confabulates from what it got. |
| `npp_vs_v3` | "10.8% faster", computed as (2.962 − 2.642) / 2.962 | An arguable definition: that is time saved, while the speed-up is 2.962 / 2.642 = 1.12×. The number is not hallucinated; the metric is ambiguous. |

**A recovered SQL error, from the trace alone:** see step 2 above. The model ran
`SELECT ... speed`, got `no such column: speed` back as a tool result, called
`get_schema`, and rewrote the query.

**An unplanned result: my own `power_state` finding happened again.** During the run-2
attempts, one LLM call took 652 s instead of about 10 s, though it produced a normal 327
tokens. `nvidia-smi` showed the GPU at **100% load but stuck in P8 at 210 MHz**, while on AC
power. Switching Windows to "Best performance" fixed it (back to about 24 tokens/s, P3/P4).
It is the same failure the benchmark findings describe, found with the same tool. I added a
180 s per-call timeout so a stalled call fails visibly instead of hanging the eval.

**What I learned:**
- Tools fix the *numbers*, not the *reasoning around them*. Remaining errors are
  attribution (H100), metric definitions (10.8% vs 1.12×), and prose. In step 2 the SQL was
  right but the formula written in the answer was wrong.
- What a tool **shows** matters as much as what it can do. Listing the column values
  fixed more cases than any prompt change.
- Data provenance is a correctness feature: a missing `gpu` column directly caused a
  hallucination.
- An eval needs its own tests (the grader) and honesty about its noise (11 cases, one run each).

