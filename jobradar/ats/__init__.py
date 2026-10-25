"""Company job boards on public ATS APIs. One parser per ATS, all fed raw JSON.

Endpoints verified in docs/ENDPOINTS.md. Slugs live in companies.yaml.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from jobradar.models import Job, from_epoch, from_iso, html_to_text

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
}


def parse_greenhouse(data: dict[str, Any], company: str) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(
            Job(
                source="greenhouse",
                external_id=str(j["id"]),
                company=j.get("company_name") or company,
                title=j["title"],
                location=loc,
                remote="remote" in loc.lower() or None,
                url=j["absolute_url"],
                description=html_to_text(j.get("content")),
                # updated_at is edit time; first_published is the real post date
                posted_at=from_iso(j.get("first_published") or j.get("updated_at")),
            )
        )
    return out


def parse_lever(data: list[dict[str, Any]], company: str) -> list[Job]:
    out = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        desc = "\n\n".join(
            [j.get("descriptionPlain", "")]
            + [f"{x['text']}\n{html_to_text(x['content'])}" for x in j.get("lists", [])]
            + [j.get("additionalPlain", "")]
        )
        out.append(
            Job(
                source="lever",
                external_id=j["id"],
                company=company,
                title=j["text"],
                location="; ".join(filter(None, locs)),
                remote=j.get("workplaceType") == "remote" or None,
                url=j["hostedUrl"],
                apply_url=j.get("applyUrl", ""),
                description=desc.strip(),
                posted_at=from_epoch(j.get("createdAt"), ms=True),
                tags=list(filter(None, [cat.get("team"), cat.get("commitment")])),
            )
        )
    return out


def parse_ashby(data: dict[str, Any], company: str) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []
        ]
        comp = j.get("compensation") or {}
        out.append(
            Job(
                source="ashby",
                external_id=j["id"],
                company=company,
                title=j["title"],
                location="; ".join(filter(None, locs)),
                remote=j.get("isRemote") or j.get("workplaceType") == "Remote" or None,
                url=j["jobUrl"],
                apply_url=j.get("applyUrl", ""),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=from_iso(j.get("publishedAt")),
                salary=comp.get("scrapeableCompensationSalarySummary") or "",
                tags=list(filter(None, [j.get("department"), j.get("employmentType")])),
            )
        )
    return out


def parse_smartrecruiters(data: dict[str, Any], company: str) -> list[Job]:
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        cid = (j.get("company") or {}).get("identifier", company)
        out.append(
            Job(
                source="smartrecruiters",
                external_id=str(j["id"]),
                company=(j.get("company") or {}).get("name") or company,
                title=j["name"],
                location=loc.get("fullLocation", ""),
                remote=loc.get("remote") or None,
                url=f"https://jobs.smartrecruiters.com/{cid}/{j['id']}",
                posted_at=from_iso(j.get("releasedDate")),
                tags=list(filter(None, [(j.get("experienceLevel") or {}).get("label")])),
            )
        )
    return out


def parse_workable(data: dict[str, Any], company: str) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        locs = [
            ", ".join(filter(None, [x.get("city"), x.get("country")]))
            for x in j.get("locations") or []
        ] or [", ".join(filter(None, [j.get("city"), j.get("country")]))]
        out.append(
            Job(
                source="workable",
                external_id=j["shortcode"],
                company=data.get("name") or company,
                title=j["title"],
                location="; ".join(filter(None, locs)),
                remote=j.get("telecommuting") or None,
                url=j["url"],
                apply_url=j.get("application_url", ""),
                posted_at=from_iso(j.get("published_on")),
                tags=list(filter(None, [j.get("department"), j.get("experience")])),
            )
        )
    return out


PARSERS: dict[str, Callable[[Any, str], list[Job]]] = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "smartrecruiters": parse_smartrecruiters,
    "workable": parse_workable,
}


def fetch_board(http: httpx.Client, ats: str, slug: str, company: str) -> list[Job]:
    r = http.get(URLS[ats].format(slug=slug))
    r.raise_for_status()
    return PARSERS[ats](r.json(), company)
