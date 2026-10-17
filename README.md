# Graph-Powered Multi-Agent PR Reviewer

**Hackathon:** Graph Hacks: Context for AI Agents (WeMakeDevs × FalkorDB)
**Track:** 1 – Agents That Act on Connected Data

When a pull request is opened on GitHub, four specialist LLM agents (Security, Quality, Tests, Docs) review it and post inline comments back to the PR. Instead of reading the diff alone, each agent receives **facts queried from a FalkorDB code graph**: which functions the diff changes, who calls them, how far the impact spreads, and which tests reach them.

**Why the graph is essential:** callers, blast radius and test coverage are multi-hop relationships (`CALLS*1..n`). Without FalkorDB the agents only see the raw diff.

---

## How it works

```
GitHub PR / push ──webhook──▶ ngrok ──▶ FastAPI (/webhook/github)
                                            │ queue
                                            ▼
                                        Worker thread
        1. fetch PR diff (GitHub API)
        2. first PR of a repo? build its code graph in FalkorDB (tree-sitter)
        3. diff ──Cypher──▶ FalkorDB ──▶ context per agent
        4. LangGraph fans out to 4 agents (Groq LLM) ──▶ aggregator
        5. post review + inline comments to the PR, save to SQLite for the dashboard
```

A `push` to the default branch rebuilds the graph, so it always reflects the base branch.

---

**Setup at a glance:** FalkorDB → credentials → `.env` → ngrok → run app → start tunnel → **create the webhook in your GitHub repo settings (Step 8)**.

## Prerequisites

| Need | Why |
|---|---|
| Python 3.11+ | App runtime |
| Docker Desktop | Runs FalkorDB locally |
| GitHub account + a **test repo you own** (with some Python/JS/TS code) | The repo the reviewer watches |
| GitHub token | Lets the app read the repo and post review comments |
| Groq API key (free) | LLM used by the agents |
| ngrok account (free) | Exposes your local server to GitHub (Step 5) |

---

## Setup

Run all commands from the project root (the folder that contains `src/`).

### 1. Install dependencies

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
macOS/Linux: `source venv/bin/activate` instead of `venv\Scripts\activate`.

### 2. Start FalkorDB

```bash
docker run -p 6379:6379 -p 3000:3000 --name falkordb falkordb/falkordb
```
Port `6379` is the database the app connects to; `http://localhost:3000` is FalkorDB's browser UI for inspecting the graph.

### 3. Get credentials

- **GitHub token** → GitHub → Settings → Developer settings → Personal access tokens.
  Fine-grained: select your test repo, permissions **Contents: Read** and **Pull requests: Read and write**. (Classic token: `repo` scope.)
  *Why:* the app downloads the repo to build the graph and posts reviews to the PR.
- **Groq API key** → https://console.groq.com/keys
- **Webhook secret** → any random string you invent, e.g. `my-secret-123`.
  *Why:* the app rejects any webhook whose signature doesn't match this secret, so only GitHub can trigger reviews.

### 4. Configure `.env`

Copy `_env.example` to `.env` in the project root and fill it in:

```env
GITHUB_TOKEN=ghp_...
GITHUB_WEBHOOK_SECRET=my-secret-123
GROQ_API_KEY=gsk_...
GROK_MODEL=llama-3.3-70b-versatile
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
FALKORDB_PASSWORD=
GRAPH_ENABLED=true
```
Leave `GRAPH_NAME` unset: the app then creates one graph per repo, named `owner__repo`.

### 5. Install and set up ngrok (`ngrok.exe`)

**Why this is required:** GitHub sends webhooks over the public internet and cannot reach `localhost`. ngrok gives your local server a temporary public HTTPS URL that GitHub can call. Without it, no PR event ever reaches the app.

1. Sign up (free) at https://ngrok.com and download ngrok for Windows from https://ngrok.com/download. Unzip it to get `ngrok.exe`.
2. Put `ngrok.exe` in a folder on your PATH, so the `ngrok` command works from any terminal. Open a new terminal afterwards and confirm with `ngrok version`.
3. Register your auth token (from the ngrok dashboard → *Your Authtoken*). This is a one-time step, and ngrok refuses to start tunnels without it:
   ```powershell
   ngrok config add-authtoken <YOUR_NGROK_TOKEN>
   ```

### 6. Start the app

Terminal 1:
```powershell
uvicorn src.pr_reviewer.main:app --port 8000
```
Check `http://localhost:8000/health` → should show `"graph_enabled": true`.
(If it shows `false`, FalkorDB isn't running – redo Step 2.)

### 7. Start the tunnel

Terminal 2:
```
ngrok http 8000
```
Copy the `https://….ngrok-free.app` URL from the `Forwarding` line. Keep this terminal open.
*Note:* the free URL changes every time ngrok restarts; if so, update the webhook (Step 8).

### 8. Create the webhook in GitHub settings

**Why this is required:** the webhook is what tells the app a PR was opened. GitHub only sends events to repositories that have a webhook configured, so nothing is reviewed until you create it. It is set per repository, not per account.

1. Open your test repo on github.com.
2. Click **Settings** (top tab of the repo) → **Webhooks** (left sidebar) → **Add webhook**.
3. Fill in the form:

| Field | Value |
|---|---|
| Payload URL | `https://<your-ngrok-url>/webhook/github` |
| Content type | `application/json` |
| Secret | the exact `GITHUB_WEBHOOK_SECRET` from `.env` |
| Events | *Let me select individual events* → tick **Pull requests** and **Pushes** only |

*Why these events:* **Pull requests** triggers a review; **Pushes** keeps the graph in sync with the default branch.
4. Click **Add webhook**. Open it again and check **Recent Deliveries**: the initial ping should show a green ✓ (requires Steps 6 and 7 to be running). Every later PR event appears here too, so you can see exactly what GitHub sent and how the app responded.

---

## Try it

1. In your test repo, create a branch, change a function that other code calls (add a bug or a weak input check for a good demo), and **open a pull request**.
2. Watch Terminal 1. On the first PR the app builds the repo's graph (the `[worker]` logs show progress), then runs the agents.
3. Within ~1 minute the PR shows a review with **inline comments** from the agents.
4. Open the dashboard: **http://localhost:8000/dashboard** – review history, findings, the graph context each agent received, and a graph explorer.
5. See the graph itself: open **http://localhost:3000**, choose graph `owner__repo` and run:
   ```cypher
   MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 100
   ```

Pushing more commits to the PR branch triggers a new review (unchanged diffs are skipped).

---

## Graph data model

```
(:File)-[:DEFINES]->(:Function)
(:Function)-[:CALLS]->(:Function)
(:File)-[:IMPORTS]->(:File)
```

| Node | Properties |
|---|---|
| `File` | `path`, `name`, `language`, `is_test`, `content` |
| `Function` | `id` (`path::Class.method`), `name`, `file`, `language`, `start_line`, `end_line`, `code` |

Built by `src/pr_reviewer/graph/build_graph.py` (tree-sitter, language-independent). Indexes: `File(path)`, `Function(id)`, `Function(file)`, `Function(name)`.

## Cypher queries the product depends on

All live in `src/pr_reviewer/graph/queries.py` and are run by `graph/retriever.py` on every review.

| # | Purpose | Query |
|---|---|---|
| 1 | Diff → changed functions | `UNWIND $ranges AS r MATCH (c:Function {file: r.file}) WHERE c.start_line <= r.hi AND c.end_line >= r.lo RETURN DISTINCT c` |
| 2 | Fan-in | `MATCH (c:Function)-[:CALLS]->(f:Function {id:$id}) RETURN count(DISTINCT c)` |
| 3 | Blast-radius size | `MATCH (c:Function)-[:CALLS*1..N]->(f:Function {id:$id}) MATCH (cf:File)-[:DEFINES]->(c) WHERE NOT cf.is_test RETURN count(DISTINCT c)` |
| 4 | Blast radius (callers, nearest first) | `MATCH p=(c:Function)-[:CALLS*1..N]->(f:Function {id:$id}) MATCH (cf:File)-[:DEFINES]->(c) WHERE NOT cf.is_test RETURN c, min(length(p)) AS dist ORDER BY dist LIMIT $limit` |
| 5 | Dependencies | `MATCH (f:Function {id:$id})-[:CALLS]->(c:Function) RETURN c LIMIT $limit` |
| 6 | Test coverage | `MATCH (tf:File)-[:DEFINES]->(c:Function)-[:CALLS*1..N]->(f:Function {id:$id}) WHERE tf.is_test RETURN DISTINCT c LIMIT $limit` |
| 7 | Importers of a changed file | `MATCH (a:File)-[:IMPORTS]->(b:File {path:$path}) RETURN a.path, a.is_test LIMIT $limit` |
| 8 | Symbols used by new code | `UNWIND $names AS n MATCH (c:Function {name:n}) RETURN n AS ref, c LIMIT $limit` |

`N` = hop depth (1–6). Every context item given to an agent traces back to a graph edge, so each finding is explainable.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Webhook delivery shows 401 | Secret in GitHub ≠ `GITHUB_WEBHOOK_SECRET` in `.env`. Restart the app after editing `.env`. |
| Webhook delivery shows connection error/404 | ngrok URL changed or ngrok is not running; update the Payload URL (must end in `/webhook/github`). |
| `/health` shows `graph_enabled: false` | FalkorDB container isn't running on port 6379. |
| Review not posted | Token lacks **Pull requests: Read and write** or doesn't cover the repo. Check Terminal 1 for `[worker]` logs. |
| `RuntimeError: GITHUB_WEBHOOK_SECRET is not set` | `.env` missing or not in the project root. |
| Groq rate-limit errors | Free tier limit; wait a minute and push another commit. |