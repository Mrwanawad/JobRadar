"""SQLite store. One row per job; status moves new -> filtered|candidate -> scored."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobradar.config import ROOT
from jobradar.models import Job

DEFAULT_PATH = ROOT / "data" / "jobradar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    url TEXT NOT NULL,
    apply_url TEXT,
    description TEXT,
    posted_at TEXT,
    salary TEXT,
    tags TEXT,
    emails TEXT,
    phones TEXT,
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',   -- new | filtered | candidate | scored | duplicate
    filter_reason TEXT,
    kw_score INTEGER,
    llm_score REAL,
    llm_json TEXT,
    scored_by TEXT,
    scored_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_fp ON jobs(fingerprint);

-- One drafted application per job. channel: email | whatsapp | manual (apply via the posting's
-- form). status: draft -> sent | failed | skipped. Sending happens outside Python (Claude Code
-- with the Gmail / WhatsApp MCP tools), which reports back through `jobradar mark`.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE REFERENCES jobs(uid),
    channel TEXT NOT NULL,
    recipient TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    ref TEXT
);
"""


def fingerprint(company: str, title: str) -> str:
    """Same role cross-posted on several boards collapses to one fingerprint."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return f"{norm(company)}|{norm(title)}"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def upsert_jobs(self, jobs: list[Job]) -> int:
        """Insert unseen jobs; return how many were new. Seen jobs are left untouched."""
        new = 0
        for j in jobs:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO jobs (uid, fingerprint, source, company, title, location,
                   remote, url, apply_url, description, posted_at, salary, tags, emails, phones,
                   first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    j.uid,
                    fingerprint(j.company, j.title),
                    j.source,
                    j.company,
                    j.title,
                    j.location,
                    None if j.remote is None else int(j.remote),
                    j.url,
                    j.apply_url,
                    j.description,
                    j.posted_at.isoformat() if j.posted_at else None,
                    j.salary,
                    json.dumps(j.tags),
                    json.dumps(j.emails),
                    json.dumps(j.phones),
                    now(),
                ),
            )
            new += cur.rowcount
        self.conn.commit()
        return new

    def by_status(self, status: str, order: str = "first_seen") -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status = ? ORDER BY {order}", (status,)
        ).fetchall()

    def set_filter(self, uid: str, status: str, reason: str, kw_score: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, filter_reason=?, kw_score=? WHERE uid=?",
            (status, reason, kw_score, uid),
        )

    def fingerprint_taken(self, fp: str, uid: str) -> bool:
        """Another row with the same role is already a candidate or scored."""
        return (
            self.conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint=? AND uid<>? "
                "AND status IN ('candidate','scored') LIMIT 1",
                (fp, uid),
            ).fetchone()
            is not None
        )

    def set_score(self, uid: str, score: float, result: dict[str, Any], by: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='scored', llm_score=?, llm_json=?, scored_by=?, scored_at=? "
            "WHERE uid=?",
            (score, json.dumps(result, ensure_ascii=False), by, now(), uid),
        )
        self.conn.commit()

    def shortlist(self, min_score: float, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status='scored' AND llm_score >= ? "
            "ORDER BY llm_score DESC, posted_at DESC LIMIT ?",
            (min_score, limit),
        ).fetchall()

    def reset_unscored(self) -> int:
        """Send every not-yet-scored job back through the prefilter (after a profile edit)."""
        cur = self.conn.execute(
            "UPDATE jobs SET status='new', filter_reason=NULL, kw_score=NULL "
            "WHERE status IN ('filtered','candidate','duplicate')"
        )
        self.conn.commit()
        return cur.rowcount

    def to_draft(self, min_score: float) -> list[sqlite3.Row]:
        """Shortlisted jobs with no application drafted yet, best first."""
        return self.conn.execute(
            "SELECT j.* FROM jobs j LEFT JOIN outbox o ON o.uid = j.uid "
            "WHERE j.status='scored' AND j.llm_score >= ? AND o.id IS NULL "
            "ORDER BY j.llm_score DESC, j.posted_at DESC",
            (min_score,),
        ).fetchall()

    def add_draft(self, uid: str, channel: str, recipient: str, subject: str, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO outbox (uid, channel, recipient, subject, body, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, channel, recipient, subject, body, now()),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def outbox(self, status: str | None = None, channel: str | None = None) -> list[sqlite3.Row]:
        where, args = [], []
        if status:
            where.append("o.status = ?")
            args.append(status)
        if channel:
            where.append("o.channel = ?")
            args.append(channel)
        sql = (
            "SELECT o.*, j.company, j.title, j.url, j.apply_url, j.llm_score "
            "FROM outbox o JOIN jobs j ON j.uid = o.uid"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY j.llm_score DESC, o.id"
        )
        return self.conn.execute(sql, args).fetchall()

    def draft_for(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM outbox WHERE uid=?", (uid,)).fetchone()

    def sent_since(self, since_iso: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status='sent' AND sent_at >= ?", (since_iso,)
        ).fetchone()[0]

    def recipient_contacted(self, recipient: str, exclude_id: int) -> bool:
        """Already sent to this address/number (several jobs can share one recruiter)."""
        return (
            self.conn.execute(
                "SELECT 1 FROM outbox WHERE recipient=? AND status='sent' AND id<>? LIMIT 1",
                (recipient, exclude_id),
            ).fetchone()
            is not None
        )

    def mark(self, outbox_id: int, status: str, ref: str = "") -> bool:
        cur = self.conn.execute(
            "UPDATE outbox SET status=?, ref=?, sent_at=? WHERE id=?",
            (status, ref, now() if status == "sent" else None, outbox_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def commit(self) -> None:
        self.conn.commit()
