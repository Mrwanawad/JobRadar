"""Postgres access for the agent activities. Schema: orchestrator/migrations (owned by Go)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from jobradar.agent import profiles
from jobradar.config import Profile

JOB_COLS = """j.uid, j.fingerprint, j.source, j.company, j.title, j.location, j.remote, j.url,
    j.apply_url, j.description, j.posted_at, j.salary, j.tags, j.emails, j.phones"""


@dataclass
class StoredProfile:
    id: str
    kind: str  # personal | demo
    profile: Profile
    cv_text: str


class AgentStore:
    def __init__(self, dsn: str, max_size: int = 8) -> None:
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=max_size, kwargs={"row_factory": dict_row}, open=True
        )

    def close(self) -> None:
        self.pool.close()

    def profile(self, profile_id: str) -> StoredProfile:
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT id, kind, profile, cv_text FROM profiles WHERE id = %s", (profile_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"profile {profile_id} not found")
        return StoredProfile(
            row["id"], row["kind"], profiles.load(row["kind"], row["profile"]), row["cv_text"]
        )

    # ---- prefilter -------------------------------------------------------------------------

    def unjudged_jobs(self, profile_id: str, max_age_days: int) -> Iterator[dict[str, Any]]:
        """Jobs this profile has no verdict on yet, recent first. Server-side cursor: a new
        profile can face thousands of rows."""
        since = datetime.now(UTC) - timedelta(days=max_age_days)
        with self.pool.connection() as c, c.cursor(name="unjudged") as cur:
            cur.execute(
                f"""SELECT {JOB_COLS} FROM jobs j
                    LEFT JOIN matches m ON m.job_uid = j.uid AND m.profile_id = %s
                    WHERE m.job_uid IS NULL AND (j.posted_at IS NULL OR j.posted_at >= %s)
                    ORDER BY j.posted_at DESC NULLS LAST""",
                (profile_id, since),
            )
            yield from cur

    def save_verdicts(self, profile_id: str, verdicts: list[tuple[str, str, str, int]]) -> None:
        """[(job_uid, status, reason, kw_score)]"""
        if not verdicts:
            return
        with self.pool.connection() as c, c.cursor() as cur:
            cur.executemany(
                """INSERT INTO matches (profile_id, job_uid, status, filter_reason, kw_score)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (profile_id, job_uid) DO NOTHING""",
                [(profile_id, *v) for v in verdicts],
            )

    def queued_fingerprints(self, profile_id: str) -> set[str]:
        with self.pool.connection() as c:
            rows = c.execute(
                """SELECT DISTINCT j.fingerprint FROM matches m JOIN jobs j ON j.uid = m.job_uid
                   WHERE m.profile_id = %s AND m.status IN ('candidate', 'scored')""",
                (profile_id,),
            ).fetchall()
        return {r["fingerprint"] for r in rows}

    # ---- indexing --------------------------------------------------------------------------

    def jobs_to_index(self, limit: int) -> list[dict[str, Any]]:
        with self.pool.connection() as c:
            return c.execute(
                f"""SELECT {JOB_COLS} FROM jobs j WHERE j.indexed_at IS NULL
                    ORDER BY j.first_seen LIMIT %s""",
                (limit,),
            ).fetchall()

    def mark_indexed(self, uids: list[str]) -> None:
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET indexed_at = now() WHERE uid = ANY(%s)", (uids,))

    # ---- matching / scoring ----------------------------------------------------------------

    def candidates(self, profile_id: str) -> list[dict[str, Any]]:
        with self.pool.connection() as c:
            return c.execute(
                """SELECT job_uid, kw_score FROM matches
                   WHERE profile_id = %s AND status = 'candidate'""",
                (profile_id,),
            ).fetchall()

    def set_match_scores(self, profile_id: str, scores: dict[str, float]) -> None:
        with self.pool.connection() as c, c.cursor() as cur:
            cur.executemany(
                """UPDATE matches SET match_score = %s, updated_at = now()
                   WHERE profile_id = %s AND job_uid = %s""",
                [(s, profile_id, uid) for uid, s in scores.items()],
            )

    def job(self, uid: str) -> dict[str, Any]:
        with self.pool.connection() as c:
            row = c.execute(f"SELECT {JOB_COLS} FROM jobs j WHERE j.uid = %s", (uid,)).fetchone()
        if row is None:
            raise LookupError(f"job {uid} not found")
        return row

    def job_with_assessment(self, profile_id: str, uid: str) -> dict[str, Any]:
        with self.pool.connection() as c:
            row = c.execute(
                f"""SELECT {JOB_COLS}, m.llm_score, m.llm_json FROM jobs j
                    JOIN matches m ON m.job_uid = j.uid AND m.profile_id = %s
                    WHERE j.uid = %s""",
                (profile_id, uid),
            ).fetchone()
        if row is None:
            raise LookupError(f"no assessment of {uid} for {profile_id}")
        return row

    def save_score(
        self, profile_id: str, uid: str, score: float, data: dict[str, Any], by: str
    ) -> None:
        with self.pool.connection() as c:
            c.execute(
                """UPDATE matches SET status = 'scored', llm_score = %s, llm_json = %s,
                       scored_by = %s, scored_at = now(), updated_at = now()
                   WHERE profile_id = %s AND job_uid = %s""",
                (score, Jsonb(data), by, profile_id, uid),
            )

    # ---- outbox ----------------------------------------------------------------------------

    def save_draft(
        self,
        profile_id: str,
        uid: str,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        critique: dict[str, Any],
        revisions: int,
    ) -> int:
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO outbox (profile_id, job_uid, channel, recipient, subject, body,
                                       critique, revisions)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (profile_id, job_uid) DO UPDATE SET
                       body = EXCLUDED.body, subject = EXCLUDED.subject,
                       critique = EXCLUDED.critique, revisions = EXCLUDED.revisions
                   WHERE outbox.status = 'draft'
                   RETURNING id""",
                (profile_id, uid, channel, recipient, subject, body, Jsonb(critique), revisions),
            ).fetchone()
            if row is None:  # already decided by the human: leave it alone
                row = c.execute(
                    "SELECT id FROM outbox WHERE profile_id = %s AND job_uid = %s",
                    (profile_id, uid),
                ).fetchone()
        return int(row["id"])

    def top(self, profile_id: str, limit: int) -> list[dict[str, Any]]:
        with self.pool.connection() as c:
            return c.execute(
                """SELECT j.uid, j.company, j.title, j.location, j.url, m.llm_score,
                          m.llm_json ->> 'pitch_angle' AS pitch, o.id AS outbox_id
                   FROM matches m JOIN jobs j ON j.uid = m.job_uid
                   LEFT JOIN outbox o ON o.profile_id = m.profile_id AND o.job_uid = m.job_uid
                   WHERE m.profile_id = %s AND m.status = 'scored'
                   ORDER BY m.llm_score DESC NULLS LAST LIMIT %s""",
                (profile_id, limit),
            ).fetchall()
