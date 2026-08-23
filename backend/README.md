# StAgent Backend

The Python service powering **StAgent** — an enterprise-grade intelligent office automation platform that combines
LLM-powered agents, a visual workflow engine, a rule engine, a typed tool system and multi-channel delivery in a single
FastAPI application.

> For a product-level overview of the platform, see the [root README](../README.md).
> For deployment instructions, see [`../deploy/README.md`](../deploy/README.md).
> For contributor conventions, see [`../AGENTS.md`](../AGENTS.md).

---

## Desktop & thin install

- Default database is **SQLite** under `StAgent_HOME/StAgent.db` (or `DB_SQLITE_PATH`), no extra dependency, `workers=1`. Set `DATABASE_URL=postgresql+asyncpg://...` with `uv sync --extra postgres` for PostgreSQL.
- Heavy stacks ship as **optional extras**: `StAgent[browser]`, `StAgent[office]`, `StAgent[rag]`, `StAgent[ocr]`, `StAgent[paddle]` (legacy OCR). Admins can also install packs from **Settings → Plugins** (`/api/v1/extensions`).
- After editing `pyproject.toml`, run `uv lock` to refresh `uv.lock`.

---

## Architecture

The default deployment is a **single-process async monolith** (`StAgent/main.py`).
All modules — agent runtime, workflow engine, tool executor, LLM routing, session
management — run in one FastAPI process backed by SQLite. This is suitable for
single-machine and small-team use.

```
                 HTTP / WS / SSE
 clients ─────────────────────────▶  FastAPI (StAgent.main:app)
                                       ├─ auth · CORS · uploads · SSE streaming
                                       │
           ┌───────────────────────────┼──────────────────────────────┐
           ▼                           ▼                              ▼
    QueryEngine                  Workflow Engine               LLM Service
    (agent runtime)              (YAML + visual)               (tier routing)
           │                           │                              │
           ▼                           ▼                              ▼
    ToolExecutor ◄── ToolRegistry ──► Tool.<name> workflow nodes     Providers
    (80+ tools)      (single source   (auto-generated palette)       (DeepSeek,
                      of truth)                                       OpenAI,
                                                                      Anthropic,
                                                                      DashScope,
                                                                      Ollama, vLLM)
           │
           ▼
    SQLite (default) + local filesystem under StAgent_HOME
    Optional: PostgreSQL, Milvus (vector memory), MinIO (object storage)
```

An optional gateway entrypoint exists at `StAgent/apps/gateway/` for teams that
outgrow the monolith. See [`../AGENTS.md`](../AGENTS.md) for the modular-monolith
discussion and [`../docs/deployment/MODULAR_MONOLITH.md`](../docs/deployment/MODULAR_MONOLITH.md)
for the full topology guide.

---

## Table of contents

- [Highlights](#highlights)
- [Tech stack](#tech-stack)
- [Directory layout](#directory-layout)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the server](#running-the-server)
- [CLI](#cli)
- [Database & migrations](#database--migrations)
- [Testing](#testing)
- [Lint, format & type-check](#lint-format--type-check)
- [Extending the platform](#extending-the-platform)
- [Local Desktop State](#local-desktop-state)
- [Observability](#observability)
- [Troubleshooting](#troubleshooting)

---

## Highlights

- **Async FastAPI** application with 20+ versioned routers under `StAgent/api/v1/` (auth, chat, tasks, tools, rules, cron, skills, channels, templates, webhooks, MCP, files/folders/documents, metrics, health, ...). The workflow engine owns its own namespace at `/api/v1/workflow/*` (CRUD, prompts, WebSocket progress, node admin). Incubating v2 endpoints live under `StAgent/api/v2/`.
- **QueryEngine session orchestrator** (`StAgent/agent/query_engine.py`) — session-scoped state, streaming `submit_message()` async generator, DI-friendly `QueryDeps` seam, explicit `Terminal` / `Continue` transitions. The legacy `AgentController` is a compat shim over `QueryEngine`.
- **Hybrid agent runtime** in `StAgent/agent/` — a ReAct controller, a Plan-and-Execute planner, a tool executor, sub-agent spawning (`fork_subagent`, `ScriptAgentTool`), and abort-safe runs.
- **Tool system** (`StAgent/tools/`) with 80+ tools across 15 categories, aliases, semantic input validation, concurrency classes (`concurrent_safe` / `read_only` / `destructive`), result-size budgets, live activity strings and interrupt behaviour.
- **Layered prompt management** (`StAgent/prompts/`) — 8-layer `PromptBuilder`, file-system `PromptRegistry`, per-layer + global budget enforcement, SHA-256 fingerprinting, provider-aware rendering (Anthropic `cache_control`).
- **Workflow engine** (`StAgent/workflow/`) — single canonical document schema, typed IO/Schema node contracts, per-node caches, in-memory prompt queue, and a dedicated FastAPI surface at `/api/v1/workflow/*` with WebSocket progress streams.
- **Rule engine** (`StAgent/rules/`) supporting 8 primitives: `compare`, `date_range`, `threshold`, `contains_all`, `date_diff`, `regex_match`, `cross_validate`, `llm_judge`.
- **Context assembly** (`StAgent/context/`) — 12 context sources (knowledge, file attachments, web search, mission state, rules, skills, channels, settings, SQL, stock data, project docs, user prompts).
- **Multi-provider LLM routing** with tier-based failover, streaming, cost tracking, and tool calls support.
- **Typed tool system** with auto-generated workflow nodes, semantic validation, and live activity strings.
- **Enterprise deployment options**: PostgreSQL, Milvus (vector memory), MinIO (object storage), Redis (caching), Prometheus (metrics).

---

## Tech stack

- **Python 3.11+** with `uv` package manager
- **FastAPI** with Uvicorn (HTTP) + Gunicorn (production)
- **SQLModel** + SQLAlchemy 2.0 (async ORM)
- **SQLite** (default) + PostgreSQL support
- **React 19** + Vite (frontend)
- **ReactFlow** (visual workflow editor)
- **OpenAI SDK** + Anthropic SDK + DeepSeek SDK
- **Playwright** (web automation)
- **PyMuPDF** (PDF processing)
- **scikit-learn** + **sentence-transformers** (RAG/ML)
- **Prometheus** + **structlog** (observability)

---

## Directory layout

```
StAgent/
├── agent/              # QueryEngine, ReAct controller, subagent spawning
├── api/                # FastAPI routers (v1, v2)
├── cli/                # CLI commands (run, chat, init, etc.)
├── config/             # Settings, providers, service configuration
├── context/            # Context sources (knowledge, files, web, etc.)
├── db/                 # Database models, repositories, migrations
├── file/               # File storage, upload/download, attachment context
├── knowledge/          # Vector search, evaluation, evidence retrieval
├── library/            # Skill library, registry, retrieval
├── llm/                # LLM service, routing, cost tracking
├── memory/             # Embeddings, vector storage, memory management
├── mission/            # Mission planning, execution, verification
├── prompts/            # Prompt building, rendering, registry
├── rules/              # Rule engine, validation, execution
├── runtime/            # Tool execution, deployment topology
├── skills/             # Built-in skills, skill registry
├── tools/              # 80+ tools across 15 categories
├── workflow/           # Workflow engine, node definitions, execution
├── utils/              # Logging, metrics, utilities
└── main.py             # FastAPI application entry point
```

---

## Requirements

- Python 3.11+
- `uv` package manager
- Node.js 20+ (for frontend development)
- Optional: PostgreSQL, Redis, Milvus, MinIO (for production deployments)

---

## Installation

```bash
# Clone the repository
git clone https://github.com/WeiY11/StAgent.git
cd StAgent

# Install Python dependencies
cd backend
uv sync

# Install frontend dependencies (optional for backend-only development)
cd ../frontend
npm install
```

---

## Configuration

Environment variables are loaded from `.env` in the project root or from `StAgent_HOME/.env`.

Key configuration options:

- `DATABASE_URL` — Database connection string (default: SQLite)
- `StAgent_HOME` — Home directory for local state (default: `~/.StAgent`)
- `StAgent_LOG_LEVEL` — Logging level (default: `INFO`)
- `StAgent_DEBUG` — Enable debug mode (default: `false`)
- `PROVIDERS_CONFIG_PATH` — Path to providers configuration (default: `~/.StAgent/providers.yaml`)

---

## Running the server

```bash
# Development mode (auto-reload)
cd backend
uv run StAgent run

# Production mode
cd backend
uv run StAgent run --workers 4

# Or using uvicorn directly
uvicorn StAgent.main:app --host 0.0.0.0 --port 7860
```

The API will be available at `http://localhost:7860`.
OpenAPI documentation: `http://localhost:7860/docs`

---

## CLI

```bash
# Interactive chat
uv run StAgent chat

# Initialize local configuration
uv run StAgent init

# Manage providers
uv run StAgent models
uv run StAgent providers

# Run migrations
uv run StAgent migrate upgrade head

# Health check
uv run StAgent doctor
```

---

## Database & migrations

```bash
# Generate migration
uv run StAgent migrate revision --autogenerate -m "description"

# Apply migrations
uv run StAgent migrate upgrade head

# Rollback migration
uv run StAgent migrate downgrade -1
```

---

## Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/test_query_engine.py -v

# Run with coverage
uv run pytest tests/ --cov=StAgent --cov-report=html
```

---

## Lint, format & type-check

```bash
# Lint
uv run ruff check StAgent/

# Format
uv run ruff format StAgent/

# Type check
uv run mypy StAgent/
```

---

## Extending the platform

See [`../AGENTS.md`](../AGENTS.md) for contributor conventions and extension guidelines.

---

## Local Desktop State

The desktop application stores state under `StAgent_HOME` (default: `~/.StAgent`):

- `StAgent.db` — SQLite database
- `providers.yaml` — LLM provider configuration
- `skills/` — Installed skills
- `uploads/` — Uploaded files
- `knowledge/` — Knowledge base
- `logs/` — Application logs

---

## Observability

- **Structured logging** with `structlog`
- **Prometheus metrics** at `/metrics`
- **Health checks** at `/health`
- **Request tracing** in logs (enable with `StAgent_DEBUG=true`)

---

## Troubleshooting

### Build failures

If you encounter encoding errors during build:

```bash
# Recreate virtual environment
cd backend
rm -rf .venv
uv sync
```

### Database locks

```bash
# Reset database (WARNING: deletes all data)
rm -f ~/.StAgent/StAgent.db
uv run StAgent migrate upgrade head
```

### Permission errors on Windows

```bash
# Close all Python processes
taskkill /F /IM python.exe

# Retry installation
uv sync
```

---

For more information, see the [main README](../README.md) and [deployment guide](../deploy/README.md).
