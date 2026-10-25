"""Draft applications for shortlisted jobs and decide which drafts may be sent today.

Python never sends. `sendable()` is the only gate: dry-run period, daily cap, CV link and
one-message-per-recipient are enforced here, and the Claude Code sender (see
.claude/commands/send-outreach.md) sends exactly what it returns.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from jobradar.config import ROOT, Profile
from jobradar.db import DB
from jobradar.llm import LLMClient, LLMError
from jobradar.score import MAX_DESC_CHARS, cv_text

# Addresses in a posting that are not where applications go.
_BAD_EMAIL = re.compile(
    r"privacy|gdpr|dpo@|legal|security|abuse|noreply|no-reply|donotreply|press|media@|"
    r"support|help@|accommodat|accessib|billing|invoice|sales@|marketing|unsubscribe"
)
_GOOD_EMAIL = re.compile(r"job|career|hiring|recruit|talent|hr@|people|apply|cv@|resume")

SYSTEM = """You write job applications for one candidate. Use only facts from the CV; never
invent experience, numbers or employers. Plain text, no Markdown. Reply with JSON only."""

TEMPLATE = """CANDIDATE CV
{cv}

CANDIDATE
Name: {name}
Based in: {base}
Links: {links}

JOB
Company: {company}
Title: {title}
Location: {location}
Strongest hook (from screening): {pitch}
Why it fits: {reasons}
Description:
{description}

Write {kind}. Tone: {tone}.
- Open with why this role at {company}, not with "I am writing to apply".
- Two or three concrete CV facts that match the job; mention the hook.
- {extra}
- End with a one-line call to action. Do not add a signature; it is appended later.

Return this JSON:
{{"subject": "<email subject, under 80 chars{subject_note}>", "body": "<message text>"}}"""

KINDS = {
    "email": (
        "a short application email to the hiring team",
        "Say the CV is linked below.",
        "",
    ),
    "whatsapp": (
        "a short WhatsApp message to the recruiter (60-100 words, no greeting line like 'Dear')",
        "Say you can send the CV here right away.",
        "; unused for WhatsApp, return an empty string",
    ),
    "manual": (
        "a cover letter to paste into the company's application form (150-250 words)",
        "No mention of attachments.",
        "; unused, return an empty string",
    ),
}


def pick_email(emails: list[str]) -> str:
    ok = [e for e in emails if not _BAD_EMAIL.search(e)]
    good = [e for e in ok if _GOOD_EMAIL.search(e)]
    return (good or ok or [""])[0]


def channel_for(row: sqlite3.Row) -> tuple[str, str]:
    """(channel, recipient). Email if the posting lists a usable address; WhatsApp only when a
    phone number is the sole contact; otherwise apply by hand through the posting's form."""
    emails, phones = json.loads(row["emails"] or "[]"), json.loads(row["phones"] or "[]")
    if email := pick_email(emails):
        return "email", email
    if phones and not emails:
        return "whatsapp", phones[0].lstrip("+")
    return "manual", row["apply_url"] or row["url"]


def draft_one(
    llm: LLMClient, profile: Profile, cv: str, row: sqlite3.Row, channel: str
) -> tuple[str, str]:
    res: dict[str, Any] = json.loads(row["llm_json"] or "{}")
    kind, extra, subject_note = KINDS[channel]
    c, o = profile.candidate, profile.outreach
    links = ", ".join(f"{k}: {v}" for k, v in c.links.items() if v) or "none"
    prompt = TEMPLATE.format(
        cv=cv,
        name=c.name,
        base=c.base,
        links=links,
        company=row["company"],
        title=row["title"],
        location=row["location"] or "unknown",
        pitch=res.get("pitch_angle", ""),
        reasons="; ".join(res.get("reasons", [])),
        description=(row["description"] or "")[:MAX_DESC_CHARS],
        kind=kind,
        tone=o.tone,
        extra=extra,
        subject_note=subject_note,
    )
    data, _ = llm.chat_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=4000,
    )
    if not isinstance(data, dict) or not str(data.get("body", "")).strip():
        raise ValueError("draft missing body")
    body = str(data["body"]).strip()
    if channel == "email":
        tail = [f"CV: {c.cv_link}" if c.cv_link else "", *[v for v in c.links.values() if v]]
        body += "\n\n" + "\n".join(t for t in tail if t)
    if o.signature.strip():
        body = body.rstrip() + "\n\n" + o.signature.strip()
    subject = str(data.get("subject", "")).strip() or f"{row['title']} - {c.name}"
    return (subject if channel == "email" else ""), body


@dataclass
class DraftStats:
    drafted: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    errors: list[str] = field(default_factory=list)
    stopped: str = ""


def draft_applications(
    db: DB, profile: Profile, llm: LLMClient | None = None, limit: int | None = None
) -> DraftStats:
    o = profile.outreach
    limit = o.max_drafts_per_run if limit is None else limit
    rows = db.to_draft(o.min_score)[:limit]
    stats = DraftStats()
    if not rows:
        return stats
    llm = llm or LLMClient()
    cv = cv_text(profile)
    for row in rows:
        channel, recipient = channel_for(row)
        try:
            subject, body = draft_one(llm, profile, cv, row, channel)
        except LLMError as e:
            stats.stopped = str(e)
            break
        except (ValueError, KeyError) as e:
            stats.errors.append(f"{row['company']} / {row['title']}: {e}")
            continue
        db.add_draft(row["uid"], channel, recipient, subject, body)
        stats.drafted[channel] += 1
    return stats


def local_midnight_utc(today: date | None = None) -> str:
    d = today or date.today()
    midnight = datetime(d.year, d.month, d.day).astimezone()
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class Sendable:
    items: list[dict[str, Any]]
    blocked: str = ""  # why nothing (more) can go out today


def sendable(db: DB, profile: Profile, today: date | None = None) -> Sendable:
    """Drafts the sender may deliver now. Duplicate recipients are marked skipped here."""
    o, c = profile.outreach, profile.candidate
    if o.dry_run(today):
        return Sendable([], f"dry run until {o.dry_run_until}")
    left = o.daily_cap - db.sent_since(local_midnight_utc(today))
    if left <= 0:
        return Sendable([], f"daily cap {o.daily_cap} reached")
    items, notes = [], []
    for r in db.outbox(status="draft"):
        if r["channel"] == "manual":
            continue
        if r["channel"] == "email" and not c.cv_link:
            notes.append("emails need candidate.cv_link in profile.yaml")
            continue
        if db.recipient_contacted(r["recipient"], r["id"]):
            db.mark(r["id"], "skipped", "recipient already contacted")
            continue
        if any(i["recipient"] == r["recipient"] for i in items):
            continue  # one per recipient per day; the rest wait for the dedupe above
        items.append(
            {
                "id": r["id"],
                "channel": r["channel"],
                "recipient": r["recipient"],
                "subject": r["subject"],
                "body": r["body"],
                "company": r["company"],
                "title": r["title"],
                # WhatsApp sends the PDF after the message; email carries cv_link instead.
                "attach": str(ROOT / c.cv) if r["channel"] == "whatsapp" else "",
            }
        )
        if len(items) >= left:
            break
    return Sendable(items, "; ".join(sorted(set(notes))))
