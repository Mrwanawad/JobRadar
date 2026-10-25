"""LLM scoring: CV + one job -> 0-10 fit score with reasons. Second, paid-in-quota pass."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from jobradar.config import ROOT, Profile
from jobradar.llm import LLMClient

MAX_DESC_CHARS = 6000

SYSTEM = """You screen job postings for one candidate. Judge strictly and honestly; a wrong
"yes" costs the candidate an embarrassing application. Reply with JSON only."""

TEMPLATE = """CANDIDATE CV
{cv}

CANDIDATE CONSTRAINTS
- Based in {base}. Holds Egyptian citizenship; no work authorisation elsewhere unless
  the CV says so.
- Acceptable: remote roles open to Egypt/EMEA/worldwide; on-site or relocation in Egypt or
  the Gulf (KSA, UAE, Qatar, Kuwait, Bahrain, Oman); EU/US on-site only with visa sponsorship.
- Target: mid-to-senior AI/LLM/ML engineering, voice/agents, computer vision, data science,
  AI training/education. Senior-to-principal data analyst only if the package looks strong.

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


def score_job(
    llm: LLMClient, profile: Profile, cv: str, row: sqlite3.Row
) -> tuple[float, dict[str, Any], str]:
    prompt = TEMPLATE.format(
        cv=cv,
        base=profile.candidate.base,
        company=row["company"],
        title=row["title"],
        location=row["location"] or "unknown",
        remote={1: "yes", 0: "no"}.get(row["remote"], "unknown"),
        salary=row["salary"] or "not stated",
        posted_at=row["posted_at"] or "unknown",
        description=(row["description"] or "(no description available)")[:MAX_DESC_CHARS],
    )
    data, res = llm.chat_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=4000,  # some free models reason in the visible reply
    )
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return clamp(data.get("score"), data), data, f"{res.provider}:{res.model}"
