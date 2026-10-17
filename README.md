# PR Reviewer Multi-Agent System

A multi-agent pull request review system built for the [WeMakeDevs × FalkorDB Hackathon](https://www.wemakedevs.org/hackathons/falkordb/rules). Four specialist AI agents (Security, Quality, Tests, Docs) analyze every PR in parallel using LangGraph, grounded by a FalkorDB code graph for repository-wide context.

---

## Architecture

```
GitHub Webhook → FastAPI → WebhookHandler → In-Memory Queue
                                                    │
                    ┌───────────────────────────────┘
                    ▼
            Background Worker
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   Fetch Diff  Ensure Graph   Run Review
        │           │           │
        └───────────┴───────────┘
                    │
            ┌───────┴───────┐
            ▼               ▼
    FalkorDB Context    LangGraph Orchestrator
    ( callers, tests,   ├─ Security Agent
      dependencies )    ├─ Quality Agent
                        ├─ Tests Agent
                        └─ Docs Agent
                                    │
                                    ▼
                            Aggregator (dedupe + gate)
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
            Inline GitHub Review            SQLite Persistence
            (file/line comments)            (dashboard history)
```

---

## What Got Fixed

### 1. Graph → Reviewer Wiring (Critical)
**Problem:** `main.py` passed `graph_store=graph.store` to `GroqReviewer`, but the constructor expected `retriever` (a `GraphContextRetriever`). The app would crash on startup.

**Fix:** `GroqReviewer` now accepts `graph_store: GraphStore | None`. It calls `graph_store.context(repo, diff)` internally, which creates the right `GraphContextRetriever` per repository graph. The `_build_context` method now properly:
- Calls `retrieve(diff)` with the correct single-argument signature
- Converts `GraphContext` → string via `render_context()`
- Builds per-agent context slices using `ROLE_PRESETS`

### 2. Per-Agent Context Slices
**Problem:** All 4 agents received the same generic context. Security didn't need test coverage info; Tests didn't need import graphs.

**Fix:** Each agent now gets a tailored context slice:
- **Security:** callees + references (what the new code calls)
- **Quality:** callees + references + callers (API contract impact)
- **Tests:** tests + callers (coverage gap analysis)
- **Docs:** callers + importers (who is affected by the change)

### 3. Inline GitHub Review Comments
**Problem:** Reviews posted only as generic PR comments. Findings had `file` and `line` fields but were never surfaced at the actual code locations.

**Fix:** `GitHubClient.post_review()` now uses the Pull Request Review API to post findings as inline line-level comments directly on the diff. Each comment appears at the exact file and line the agent flagged.

### 4. Review Persistence & Dashboard API
**Problem:** Reviews vanished after posting. No history, no analytics, no way to build a UI.

**Fix:** Added SQLite persistence (`adapters/persistence/db.py`) and REST endpoints:
- `GET /api/stats` — aggregate metrics
- `GET /api/reviews` — list reviews
- `GET /api/reviews/{id}` — review detail with findings
- `GET /api/repos` — connected repositories

### 5. Dashboard UI Starter
Added a complete static dashboard (`static/dashboard.html`) served at `/dashboard`. Built with Vue 3 (ESM CDN) + Tailwind CSS (CDN). Zero build step.

---

## Quick Start

### 1. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your keys
```

Required:
- `GITHUB_TOKEN` — personal access token with `repo` scope
- `GITHUB_WEBHOOK_SECRET` — any random string (used to verify webhooks)
- `GROQ_API_KEY` — from [console.groq.com](https://console.groq.com)

Optional:
- `FALKORDB_HOST` / `FALKORDB_PORT` — if running FalkorDB separately
- `GRAPH_ENABLED=false` — to run without FalkorDB (diff-only reviews)

### 3. Run FalkorDB (optional but recommended)

```bash
docker run -p 6379:6379 -it --rm falkordb/falkordb:latest
```

### 4. Start the Server

```bash
uvicorn src.pr_reviewer.main:app --reload --port 8000
```

### 5. Expose to GitHub (for webhooks)

Use ngrok or Cloudflare Tunnel:

```bash
ngrok http 8000
```

### 6. Configure GitHub Webhook

In your repo → Settings → Webhooks → Add webhook:
- **Payload URL:** `https://your-ngrok-url.ngrok.io/webhook/github`
- **Content type:** `application/json`
- **Secret:** same as `GITHUB_WEBHOOK_SECRET`
- **Events:** Pull requests, Pushes

### 7. Open the Dashboard

Visit `http://localhost:8000/dashboard/dashboard.html`

---

## Pre-Building a Code Graph (Optional)

For the first PR on a repo, the system auto-builds the graph. But you can pre-build it:

```bash
python -m src.pr_reviewer.graph.build_graph https://github.com/owner/repo code_graph
```

Then set `GRAPH_NAME=code_graph` in `.env` to use that graph for all repos.

---

## Project Structure

```
src/pr_reviewer/
├── adapters/
│   ├── github/          # GitHub API client, webhook verifier, payload parsers
│   ├── llm/             # GroqReviewer (LangGraph + multi-agent)
│   ├── persistence/     # SQLite review history
│   └── queue/           # In-memory job queue
├── agents/              # 4 specialist agents (Security, Quality, Tests, Docs)
├── api/                 # FastAPI routers (webhook + dashboard)
├── domain/              # Core models, contracts, interfaces (no framework deps)
├── graph/               # FalkorDB code graph builder, retriever, renderer
├── orchestrator/        # LangGraph state, aggregator, graph topology
├── workers/             # Background queue processor
├── main.py              # Composition root (wires everything together)
└── config.py            # Settings from environment
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | — | GitHub PAT for fetching diffs and posting reviews |
| `GITHUB_WEBHOOK_SECRET` | Yes | — | Secret for verifying webhook signatures |
| `GROQ_API_KEY` | Yes | — | Groq API key for LLM inference |
| `GROK_MODEL` | No | `llama-3.3-70b-versatile` | Model ID on Groq |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB Redis host |
| `FALKORDB_PORT` | No | `6379` | FalkorDB Redis port |
| `FALKORDB_PASSWORD` | No | — | FalkorDB password |
| `GRAPH_ENABLED` | No | `true` | Set to `false` to disable FalkorDB |
| `GRAPH_NAME` | No | — | Fixed graph name for all repos |

---

## Demo Checklist for Judges

- [ ] Open a PR with a deliberate bug (e.g., SQL injection, missing test)
- [ ] Show the webhook delivery in GitHub → Recent Deliveries
- [ ] Open the dashboard and refresh to see the new review appear
- [ ] Click into the review detail — point out:
  - [ ] 4 agent tabs with findings
  - [ ] Inline file/line comments posted back to GitHub
  - [ ] FalkorDB context (callers, tests, blast radius)
- [ ] Open the GitHub PR and show the AI review comments directly on the code
- [ ] Show the human-in-the-loop gate (low-confidence reviews marked "Needs Human Review")

---

## License

MIT
