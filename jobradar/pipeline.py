"""fetch -> prefilter -> score. Each stage reads/writes the DB, so stages can run separately."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from jobradar.ats import fetch_board
from jobradar.config import Profile, load_companies
from jobradar.db import DB
from jobradar.llm import LLMClient, LLMError
from jobradar.models import Job
from jobradar.prefilter import Prefilter
from jobradar.score import cv_text, score_job
from jobradar.sources import FEEDS

log = logging.getLogger(__name__)
UA = {"User-Agent": "jobradar/0.1 (personal job search)"}


@dataclass
class FetchStats:
    fetched: int = 0
    new: int = 0
    failed: list[str] = field(default_factory=list)


def fetch_all(db: DB, http: httpx.Client | None = None, workers: int = 8) -> FetchStats:
    http = http or httpx.Client(headers=UA, timeout=60, follow_redirects=True)
    tasks: list[tuple[str, Callable[[], list[Job]]]] = [
        (f"{ats}:{slug}", lambda a=ats, s=slug, n=name: fetch_board(http, a, s, n))
        for ats, slug, name in load_companies()
    ] + [(name, lambda f=fn: f(http)) for name, fn in FEEDS.items()]

    stats = FetchStats()

    def run(task: tuple[str, Callable[[], list[Job]]]) -> tuple[str, list[Job] | Exception]:
        name, fn = task
        try:
            return name, fn()
        except Exception as e:  # one broken board never stops the rest
            return name, e

    with ThreadPoolExecutor(workers) as ex:
        for name, result in ex.map(run, tasks):
            if isinstance(result, Exception):
                stats.failed.append(f"{name}: {type(result).__name__}: {str(result)[:120]}")
                continue
            stats.fetched += len(result)
            stats.new += db.upsert_jobs(result)
    return stats


def prefilter_new(db: DB, profile: Profile) -> dict[str, int]:
    pf = Prefilter(profile)
    out = {"candidate": 0, "filtered": 0, "duplicate": 0}
    for row in db.by_status("new"):
        v = pf.check(
            row["title"],
            row["location"] or "",
            None if row["remote"] is None else bool(row["remote"]),
            row["description"] or "",
            row["posted_at"],
        )
        if not v.keep:
            db.set_filter(row["uid"], "filtered", v.reason, 0)
            out["filtered"] += 1
        elif db.fingerprint_taken(row["fingerprint"], row["uid"]):
            db.set_filter(row["uid"], "duplicate", "same company+title already queued", v.kw_score)
            out["duplicate"] += 1
        else:
            db.set_filter(row["uid"], "candidate", v.reason, v.kw_score)
            out["candidate"] += 1
        db.commit()
    return out


@dataclass
class ScoreStats:
    scored: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: str = ""


def score_candidates(
    db: DB,
    profile: Profile,
    llm: LLMClient | None = None,
    limit: int | None = None,
    on_scored: Callable[[str, float], None] | None = None,
) -> ScoreStats:
    """Score best keyword matches first, up to `limit` (profile default) LLM calls."""
    limit = profile.scoring.max_llm_per_run if limit is None else limit
    llm = llm or LLMClient()
    cv = cv_text(profile)
    stats = ScoreStats()
    rows = db.by_status("candidate", order="kw_score DESC, posted_at DESC")[:limit]
    for row in rows:
        try:
            score, result, by = score_job(llm, profile, cv, row)
        except LLMError as e:
            stats.stopped = str(e)  # every provider is out; retry next run
            break
        except (ValueError, KeyError) as e:
            stats.errors.append(f"{row['company']} / {row['title']}: {e}")
            continue
        db.set_score(row["uid"], score, result, by)
        stats.scored += 1
        if on_scored:
            on_scored(f"{row['company']} - {row['title']}", score)
    return stats
