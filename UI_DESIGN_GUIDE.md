# PR Reviewer Dashboard — UI Design Guide

## Do you even need a UI?

**Short answer: yes, for the hackathon demo.** The GitHub webhook flow works without any UI — developers just open PRs and comments appear. But for demoing to judges, a dashboard is essential because:

1. **Visual proof of the multi-agent system** — judges can see 4 agents working in parallel
2. **Evidence of FalkorDB integration** — show the code graph context that feeds the agents
3. **Review history & analytics** — prove the system accumulates knowledge over time
4. **Human-in-the-loop gate** — show low-confidence reviews held for approval

---

## Recommended Screen Architecture (3 pages)

### Page 1: Dashboard Overview (`/`)
**Purpose:** At-a-glance health of the review system.

**Layout:**
```
+-----------------------------------------------------------+
|  PR Reviewer Agent                [Repos ▼] [Refresh]     |
+-----------------------------------------------------------+
|  +-----------+  +-----------+  +-----------+  +---------+ |
|  | Total     |  | Held for  |  | Avg       |  | Graph   | |
|  | Reviews   |  | Human     |  | Confidence|  | Status  | |
|  |   47      |  |    8      |  |   0.78    |  | Online  | |
|  +-----------+  +-----------+  +-----------+  +---------+ |
+-----------------------------------------------------------+
|  Recent Reviews                                           |
|  +-----------------------------------------------------+  |
|  | Repo          | PR   | Title               | Agents |  |
|  | owner/webapp  | #42  | Add auth flow       | S Q T  |  |
|  | owner/api     | #38  | Fix rate limiter    | S Q    |  |
|  | ...           | ...  | ...                 | ...    |  |
|  +-----------------------------------------------------+  |
+-----------------------------------------------------------+
```

**API endpoints used:**
- `GET /api/stats` — header cards
- `GET /api/reviews?limit=20` — table rows

**Key interactions:**
- Click a row → navigates to Review Detail page
- Repo dropdown filters the table
- Agent badges (S=Security, Q=Quality, T=Tests, D=Docs) show which agents flagged findings

---

### Page 2: Review Detail (`/review/{id}`)
**Purpose:** Deep-dive into one PR review — the star of your demo.

**Layout:**
```
+-----------------------------------------------------------+
|  ← Back to Dashboard                                      |
+-----------------------------------------------------------+
|  Repo: owner/webapp  |  PR #42: Add auth flow             |
|  SHA: abc1234        |  Confidence: 0.82 | Status: PASS   |
+-----------------------------------------------------------+
|                                                           |
|  +-------------------+  +-----------------------------+   |
|  | AGENT TABS        |  | FINDING DETAIL              |   |
|  | [Security] [2]    |  |                             |   |
|  | [Quality ] [1]    |  | [HIGH] sql-injection        |   |
|  | [Tests   ] [0]    |  | File: auth.py:45            |   |
|  | [Docs    ] [0]    |  |                             |   |
|  |                   |  | User input is concatenated  |   |
|  |                   |  | directly into a raw SQL...  |   |
|  |                   |  |                             |   |
|  |                   |  | Suggested fix:              |   |
|  |                   |  | Use parameterized queries   |   |
|  +-------------------+  +-----------------------------+   |
|                                                           |
|  +-----------------------------+  +--------------------+  |
|  | CODE GRAPH CONTEXT          |  | DIFF SNIPPET       |  |
|  | Changed fn: `login()`       |  | ```python          |  |
|  | Callers: 5 direct           |  | - query = f"SELECT |  |
|  | Tests: none reach it        |  | + query = "SELECT  |  |
|  | Risk: HIGH                  |  | ```                |  |
|  +-----------------------------+  +--------------------+  |
|                                                           |
+-----------------------------------------------------------+
```

**API endpoint used:**
- `GET /api/reviews/{id}` — full review + findings

**Key interactions:**
- Agent tabs filter findings by agent type
- Click a finding → shows detail + code context + diff snippet
- "View on GitHub" button opens the actual PR

**Why this page wins hackathons:**
- It proves the 4-agent architecture isn't fake — each finding is tagged with its agent
- It shows FalkorDB value — the "Code Graph Context" panel displays caller/test info that no other tool surfaces
- It demonstrates the human-in-the-loop gate — held reviews show a warning banner

---

### Page 3: Repo Settings (`/repos`)
**Purpose:** Onboarding new repos, triggering manual graph rebuilds.

**Layout:**
```
+-----------------------------------------------------------+
|  Connected Repositories                                   |
+-----------------------------------------------------------+
|  +-------------------+  +-----------------------------+   |
|  | REPO LIST         |  | REPO DETAIL                 |   |
|  | owner/webapp   ●  |  | owner/webapp                |   |
|  | owner/api      ●  |  | Graph: 1,247 functions      |   |
|  | + Add repo...     |  | Last indexed: 2 hours ago   |   |
|  |                   |  |                             |   |
|  |                   |  | [Rebuild Graph] [Test Hook] |   |
|  +-------------------+  +-----------------------------+   |
+-----------------------------------------------------------+
```

**Why you need this:**
- Judges will ask "how does a repo get connected?" — show them it's just a webhook URL
- The "Rebuild Graph" button proves you control the FalkorDB graph lifecycle
- Good place to show off the tarball-fetch + tree-sitter pipeline

---

## Tech Stack Recommendation

For a hackathon, **keep it minimal**. You have ~2 days left. Here are three options:

### Option A: Static HTML + Vanilla JS (Fastest — 2-3 hours)
Create a single `dashboard.html` file served by FastAPI's `StaticFiles`.

Pros:
- Zero build step
- One file to demo
- Easy to style with Tailwind CDN

Cons:
- No routing (use hash-based navigation: `/#review/42`)
- Manual DOM manipulation

**Best if:** You want a working dashboard TODAY and can tolerate a simple UI.

### Option B: React + Vite (Medium — 4-6 hours)
A small React app in a `frontend/` folder, built to static files and served by FastAPI.

Pros:
- Component reusability
- React ecosystem (charts, tables, markdown rendering)
- Looks more polished

Cons:
- Build step adds complexity
- More files to manage

**Best if:** You already know React and want to impress judges with polish.

### Option C: Embed in FastAPI with Jinja2 (Quickest — 1 hour)
Server-rendered HTML pages using FastAPI's Jinja2Templates.

Pros:
- No separate frontend build
- Python-only codebase
- Good enough for a functional demo

Cons:
- Less interactive
- Harder to make "app-like"

**Best if:** You have zero frontend experience and just need something visible.

---

## My Recommendation for You

**Go with Option A (Static HTML + Tailwind CDN) for the hackathon.**

Here's why:
1. Your backend is sophisticated — the UI's job is to *expose* that sophistication, not compete with it
2. A clean single-page dashboard with 2-3 views is enough to tell the story
3. You can always rewrite it in React later

I've included a `demo_dashboard.html` starter file in this package. It:
- Has all 3 screen layouts pre-built
- Calls your actual API endpoints (`/api/stats`, `/api/reviews`)
- Uses Tailwind CSS via CDN (no build step)
- Has hash-based routing (`#review/123`)

---

## API Contract Reference

Your backend now exposes these endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server + graph status |
| `/webhook/github` | POST | GitHub webhook receiver |
| `/api/stats` | GET | Dashboard header stats |
| `/api/reviews` | GET | List reviews (opt: `?repo=owner/name&limit=50`) |
| `/api/reviews/{id}` | GET | Single review with findings |
| `/api/repos` | GET | List of all repos with reviews |

All JSON responses are CORS-enabled.

---

## Data Flow for the UI

```
User opens Dashboard
       │
       ▼
  GET /api/stats  ──►  Header cards (total, held, confidence)
  GET /api/reviews ──►  Table rows
       │
       ▼
User clicks a review
       │
       ▼
  GET /api/reviews/42 ──►  Full review + findings array
       │
       ▼
Render agent tabs from findings[].agent_type
Render finding cards from findings[]
Show graph context from the review summary (or future endpoint)
```

---

## Critical Demo Script

When presenting to judges, walk through this exact flow:

1. **Show the GitHub PR** — "Here's a real PR that just came in"
2. **Show the webhook delivery** — "GitHub hits our `/webhook/github` endpoint"
3. **Switch to Dashboard** — "Our system queued it; here's the live review"
4. **Open Review Detail** — "4 agents analyzed it in parallel using LangGraph"
5. **Point to agent tabs** — "Security found an injection risk, Quality found a logic bug"
6. **Point to graph context** — "FalkorDB told us this function has 5 callers but zero tests"
7. **Show the GitHub comment** — "The review posted back as inline comments on the PR"

This 2-minute walkthrough proves: multi-agent architecture ✓, FalkorDB integration ✓, real GitHub automation ✓.

---

## Future Enhancements (post-hackathon)

- **Real-time updates** — WebSocket from FastAPI to push new reviews to the dashboard
- **Agent configuration** — Toggle agents on/off per repo, adjust confidence thresholds
- **Custom rules** — Let users write their own agent prompts
- **Team annotations** — Human reviewers can thumbs-up/down AI findings to improve the model
