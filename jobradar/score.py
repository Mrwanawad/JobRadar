"""LLM scoring: CV + one job -> 0-10 fit score with reasons. Second, paid-in-quota pass."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from jobradar.config import DEFAULT_CONSTRAINTS, ROOT, Profile
from jobradar.llm import LLMClient

MAX_DESC_CHARS = 6000

SYSTEM = """You screen job postings for one candidate. Judge strictly and honestly; a wrong
"yes" costs the candidate an embarrassing application. Reply with JSON only."""

TEMPLATE = """CANDIDATE CV
{cv}

CANDIDATE CONSTRAINTS
{constraints}

JOB
Company: {company}
Title: {title}
Location: {location} (remote flag: {remote})
Salary: {salary}
Posted: {posted_at}
Description:
{description}

Return this JSON:
{{
  "score": <0-10 overall fit; 8+ only if role, seniority and location all fit>,
  "role_fit": <0-10 skills/experience match>,
  "seniority_fit": "under" | "match" | "over",
  "location_fit": "eligible" | "needs_sponsorship" | "ineligible",
  "visa_sponsorship": "yes" | "no" | "unknown",
  "salary_signal": "<short note or 'unknown'>",
  "reasons": ["<max 3 short reasons it fits>"],
  "red_flags": ["<max 3 short concerns>"],
  "pitch_angle": "<one sentence: the strongest CV hook for this job>"
}}"""


def cv_text(profile: Profile) -> str:
    """Text of the CV PDF, cached next to it as .txt (gitignored)."""
    pdf = ROOT / profile.candidate.cv
    cache = pdf.with_suffix(".txt")
    if cache.exists() and cache.stat().st_mtime >= pdf.stat().st_mtime:
        return cache.read_text(encoding="utf-8")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(pdf).pages).strip()
    Path(cache).write_text(text, encoding="utf-8")
    return text


def clamp(score: Any, result: dict[str, Any]) -> float:
    try:
        s = max(0.0, min(10.0, float(score)))
    except (TypeError, ValueError):
        return 0.0
    if result.get("location_fit") == "ineligible":
        s = min(s, 3.0)
    if result.get("location_fit") == "needs_sponsorship" and result.get("visa_sponsorship") == "no":
        s = min(s, 3.0)
    return s


def constraints_text(profile: Profile) -> str:
    c = profile.candidate
    return (c.constraints or DEFAULT_CONSTRAINTS).format(base=c.base or "unknown")


def build_prompt(profile: Profile, cv: str, job: Mapping[str, Any]) -> list[dict[str, str]]:
    """Messages for one screening call. `job` is a SQLite row or a Postgres dict row."""
    prompt = TEMPLATE.format(
        cv=cv,
        constraints=constraints_text(profile),
        company=job["company"],
        title=job["title"],
        location=job["location"] or "unknown",
        remote={1: "yes", 0: "no"}.get(job["remote"], "unknown"),
        salary=job["salary"] or "not stated",
        posted_at=job["posted_at"] or "unknown",
        description=(job["description"] or "(no description available)")[:MAX_DESC_CHARS],
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]


def score_job(
    llm: LLMClient, profile: Profile, cv: str, row: Mapping[str, Any]
) -> tuple[float, dict[str, Any], str]:
    data, res = llm.chat_json(
        build_prompt(profile, cv, row),
        max_tokens=4000,  # some free models reason in the visible reply
    )
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return clamp(data.get("score"), data), data, f"{res.provider}:{res.model}"
