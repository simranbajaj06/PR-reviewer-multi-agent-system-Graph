"""Lightweight SQLite persistence for review history.

Zero external infrastructure: the DB file lives on disk and is created
automatically on first use.  For production you would swap this for
Postgres or let FalkorDB hold the review metadata too.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.pr_reviewer.domain.contracts import AgentType, Finding, Severity

_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "reviews.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def db_path() -> str:
    return str(_DB_PATH)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                repo        TEXT NOT NULL,
                pr_number   INTEGER NOT NULL,
                pr_title    TEXT,
                head_sha    TEXT,
                verdict     TEXT,           -- hold | comment | approve
                summary     TEXT,
                overall_confidence REAL,
                needs_human_review INTEGER,
                created_at  TEXT NOT NULL,
                UNIQUE(repo, pr_number, head_sha)
            );

            CREATE TABLE IF NOT EXISTS findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                agent_type  TEXT NOT NULL,
                severity    TEXT NOT NULL,
                category    TEXT,
                summary     TEXT NOT NULL,
                file        TEXT,
                line        INTEGER,
                confidence  REAL,
                rationale   TEXT,
                suggestion  TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reviews_repo ON reviews(repo);
            CREATE INDEX IF NOT EXISTS idx_reviews_time ON reviews(created_at);
            CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "graph_context" not in cols:   # upgrade older databases in place
            conn.execute("ALTER TABLE reviews ADD COLUMN graph_context TEXT")
        conn.commit()


def _as_dict(f: Any) -> dict[str, Any]:
    """The worker hands us plain dicts (Review.findings); older callers pass
    Finding models. Normalise both, and turn enums into plain strings."""
    d = dict(f) if isinstance(f, dict) else f.model_dump()
    for k in ("agent_type", "severity"):
        d[k] = getattr(d[k], "value", d[k])
    return d


def save_review(
    repo: str,
    pr_number: int,
    pr_title: str,
    head_sha: str,
    verdict: str,
    summary: str,
    overall_confidence: float,
    needs_human_review: bool,
    findings: list[Finding],
    graph_context: dict | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO reviews (repo, pr_number, pr_title, head_sha, verdict,
                                 summary, overall_confidence, needs_human_review,
                                 created_at, graph_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, pr_number, head_sha) DO UPDATE SET
                verdict=excluded.verdict,
                summary=excluded.summary,
                overall_confidence=excluded.overall_confidence,
                needs_human_review=excluded.needs_human_review,
                graph_context=excluded.graph_context,
                created_at=excluded.created_at
            RETURNING id
            """,
            (repo, pr_number, pr_title, head_sha, verdict, summary,
             overall_confidence, int(needs_human_review), now,
             json.dumps(graph_context) if graph_context else None),
        )
        row = cur.fetchone()
        review_id = row[0] if row else 0

        # Replace findings atomically
        conn.execute("DELETE FROM findings WHERE review_id = ?", (review_id,))
        for f in findings:
            f = _as_dict(f)
            conn.execute(
                """
                INSERT INTO findings (review_id, agent_type, severity, category,
                                      summary, file, line, confidence, rationale,
                                      suggestion, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (review_id, f["agent_type"], f["severity"], f.get("category"),
                 f["summary"], f.get("file"), f.get("line"), f.get("confidence"),
                 f.get("rationale"), f.get("suggestion"), now),
            )
        conn.commit()
        return review_id


def list_reviews(repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT r.*, COUNT(f.id) AS finding_count, "
        "COALESCE(GROUP_CONCAT(DISTINCT f.agent_type), '') AS agents "
        "FROM reviews r LEFT JOIN findings f ON f.review_id = r.id"
    )
    params: tuple = ()
    if repo:
        sql += " WHERE r.repo = ?"
        params = (repo,)
    sql += " GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?"
    params += (limit,)

    with _conn() as conn:
        out = []
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            d.pop("graph_context", None)   # large; only the detail view needs it
            d["agents"] = sorted(a for a in d["agents"].split(",") if a)
            out.append(d)
        return out


def get_review(review_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        if not row:
            return None
        review = dict(row)
        gc = review.get("graph_context")
        review["graph_context"] = json.loads(gc) if gc else None
        review["findings"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM findings WHERE review_id = ?", (review_id,)
            )
        ]
        return review


def get_stats() -> dict[str, Any]:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        by_verdict = {
            r["verdict"]: r["n"]
            for r in conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM reviews GROUP BY verdict"
            )
        }
        by_agent = {
            r["agent_type"]: r["n"]
            for r in conn.execute(
                "SELECT agent_type, COUNT(*) AS n FROM findings GROUP BY agent_type"
            )
        }
        by_severity = {
            r["severity"]: r["n"]
            for r in conn.execute(
                "SELECT severity, COUNT(*) AS n FROM findings GROUP BY severity"
            )
        }
        repos = [
            r["repo"]
            for r in conn.execute(
                "SELECT DISTINCT repo FROM reviews ORDER BY repo"
            )
        ]
    return {
        "total_reviews": total,
        "by_verdict": by_verdict,
        "by_agent": by_agent,
        "by_severity": by_severity,
        "repos": repos,
    }
