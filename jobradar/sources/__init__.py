"""Aggregator boards and feeds: RemoteOK, We Work Remotely, Himalayas, HN Who is hiring."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from jobradar.models import Job, extract_contacts, from_epoch, from_iso, html_to_text

REMOTEOK_URL = "https://remoteok.com/api"
WWR_URL = "https://weworkremotely.com/remote-jobs.rss"
HIMALAYAS_URL = "https://himalayas.app/jobs/api"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"


def _with_contacts(job: Job) -> Job:
    job.emails, job.phones = extract_contacts(job.description)
    return job


def parse_remoteok(data: list[dict[str, Any]]) -> list[Job]:
    out = []
    for j in data:
        if "legal" in j or "id" not in j:  # first element is the API terms notice
            continue
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        out.append(
            _with_contacts(
                Job(
                    source="remoteok",
                    external_id=str(j["id"]),
                    company=j.get("company", ""),
                    title=j.get("position", ""),
                    location=j.get("location") or "Remote",
                    remote=True,
                    url=j["url"],
                    apply_url=j.get("apply_url", ""),
                    description=html_to_text(j.get("description")),
                    posted_at=from_iso(j.get("date")) or from_epoch(j.get("epoch")),
                    salary=f"${lo:,}-${hi:,}" if hi else "",
                    tags=list(j.get("tags") or []),
                )
            )
        )
    return out


def parse_wwr(xml: str | bytes) -> list[Job]:
    out = []
    for e in feedparser.parse(xml).entries:
        if not e.get("link"):  # truncated/partial entry
            continue
        company, _, title = e.get("title", "").partition(": ")
        if not title:
            company, title = "", company
        region = e.get("region", "")
        out.append(
            _with_contacts(
                Job(
                    source="wwr",
                    external_id=e.get("id") or e["link"],
                    company=company.strip(),
                    title=title.strip(),
                    location=region or "Remote",
                    remote=True,
                    url=e["link"],
                    description=html_to_text(e.get("description")),
                    posted_at=_published(e),
                    tags=list(filter(None, [e.get("category"), e.get("skills")])),
                )
            )
        )
    return out


def _published(e: Any) -> datetime | None:
    t = e.get("published_parsed")
    return datetime(*t[:6], tzinfo=UTC) if t else None


def parse_himalayas(data: dict[str, Any]) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        restr = j.get("locationRestrictions") or []
        lo, hi, cur = j.get("minSalary"), j.get("maxSalary"), j.get("currency") or ""
        out.append(
            _with_contacts(
                Job(
                    source="himalayas",
                    external_id=j["guid"],
                    company=j.get("companyName", ""),
                    title=j.get("title", ""),
                    location="Remote (" + ", ".join(restr) + ")" if restr else "Remote (Anywhere)",
                    remote=True,
                    url=j["guid"],
                    apply_url=j.get("applicationLink", ""),
                    description=html_to_text(j.get("description") or j.get("excerpt")),
                    posted_at=from_epoch(j.get("pubDate")),
                    salary=f"{cur} {lo:,}-{hi:,}".strip() if lo and hi else "",
                    tags=list(j.get("seniority") or []) + list(j.get("categories") or []),
                )
            )
        )
    return out


_HN_SPLIT = re.compile(r"\s+[|—–]\s+|\s+-\s+")


def parse_hn_thread(item: dict[str, Any]) -> list[Job]:
    """Each top-level comment is one posting: first line 'Company | Role | Location | ...'."""
    out = []
    for c in item.get("children", []):
        text = c.get("text") or ""
        if not text or c.get("parent_id") != item.get("id"):
            continue
        body = html_to_text(text)
        first = body.split("\n", 1)[0]
        parts = [p.strip() for p in _HN_SPLIT.split(first) if p.strip()]
        company = re.sub(r"\s*\(.*?\)\s*", " ", parts[0]).strip() if parts else ""
        title = parts[1] if len(parts) > 1 else first[:120]
        loc = next((p for p in parts if re.search(r"remote|onsite|on-site|hybrid", p, re.I)), "")
        if not loc and parts and "(" in parts[0]:
            loc = parts[0][parts[0].find("(") + 1 : parts[0].rfind(")")]
        out.append(
            _with_contacts(
                Job(
                    source="hn",
                    external_id=str(c["id"]),
                    company=company[:120],
                    title=title[:200],
                    location=loc,
                    remote=bool(re.search(r"remote", first, re.I)) or None,
                    url=f"https://news.ycombinator.com/item?id={c['id']}",
                    description=body,
                    posted_at=from_epoch(c.get("created_at_i")),
                )
            )
        )
    return out


def fetch_remoteok(http: httpx.Client) -> list[Job]:
    r = http.get(REMOTEOK_URL)
    r.raise_for_status()
    return parse_remoteok(r.json())


def fetch_wwr(http: httpx.Client) -> list[Job]:
    r = http.get(WWR_URL)
    r.raise_for_status()
    return parse_wwr(r.content)


def fetch_himalayas(http: httpx.Client, pages: int = 5) -> list[Job]:
    out: list[Job] = []
    cursor = None
    for _ in range(pages):
        params: dict[str, Any] = {"limit": 20}
        if cursor:
            params["cursor"] = cursor
        r = http.get(HIMALAYAS_URL, params=params)
        r.raise_for_status()
        data = r.json()
        out += parse_himalayas(data)
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return out


def fetch_hn(http: httpx.Client) -> list[Job]:
    r = http.get(HN_SEARCH_URL, params={"hitsPerPage": 5})
    r.raise_for_status()
    story = next(
        (
            h
            for h in r.json()["hits"]
            if (h.get("title") or "").startswith("Ask HN: Who is hiring?")
        ),
        None,
    )
    if story is None:
        return []
    item = http.get(HN_ITEM_URL.format(id=story["objectID"]))
    item.raise_for_status()
    return parse_hn_thread(item.json())


FEEDS = {
    "remoteok": fetch_remoteok,
    "wwr": fetch_wwr,
    "himalayas": fetch_himalayas,
    "hn": fetch_hn,
}
