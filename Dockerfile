# Step 6: run the MCP server (or the agent) in a container.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bench_agent bench_agent
COPY evals evals
COPY data data
# Inside a container "localhost" is the container itself; reach Ollama on the host instead.
# Override with --env-file .env (NIM/OpenAI) or -e LLM_BASE_URL=...
ENV LLM_BASE_URL=http://host.docker.internal:11434/v1 \
    LLM_API_KEY=ollama \
    LLM_MODEL=qwen3:8b \
    PYTHONUNBUFFERED=1
# Default: MCP server over stdio (docker run -i). Override with e.g.
#   docker run --rm bench-agent python -m bench_agent.agent "question"
CMD ["python", "-m", "bench_agent.mcp_server"]
