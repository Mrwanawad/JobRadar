"""Stored profiles. Personal profiles are profile.yaml as JSON; demo profiles are the short
form a visitor fills in on the website, expanded here into the same Profile model so the
prefilter, screening and writing code paths are identical for both."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from jobradar.config import Candidate, Locations, Outreach, Profile, Roles, Scoring

# Regions a visitor can pick for remote roles, as regex alternatives for location text.
REGIONS: dict[str, str] = {
    "worldwide": "worldwide|anywhere|global|international",
    "emea": "emea|europe|middle east|africa",
    "mena": "mena|middle east|north africa|gcc|gulf",
    "europe": "europe|eu\\b|european union",
    "americas": "americas|north america|latin america|latam",
    "apac": "apac|asia|pacific",
}

SENIORITY_EXCLUDE: dict[str, str] = {
    "junior": r"\b(senior|sr\.?|lead|staff|principal|director|head of|vp)\b",
    "mid": r"\b(intern|internship|junior|jr\.?|graduate|staff|principal|director|head of|vp)\b",
    "senior": r"\b(intern|internship|junior|jr\.?|graduate|new grad|director|vp|vice president)\b",
    "lead": r"\b(intern|internship|junior|jr\.?|graduate|new grad|vp|vice president)\b",
}

ALWAYS_EXCLUDE = [r"\b(sales|account executive|recruiter|marketing|legal|counsel|accountant)\b"]


class DemoForm(BaseModel):
    """What a website visitor submits. Bounded so one visitor cannot craft a costly run."""

    name: str = Field(default="Candidate", max_length=80)
    base: str = Field(max_length=80)
    target_titles: list[str] = Field(min_length=1, max_length=6)
    seniority: Literal["junior", "mid", "senior", "lead"] = "senior"
    remote_regions: list[Literal["worldwide", "emea", "mena", "europe", "americas", "apac"]] = (
        Field(default_factory=lambda: ["worldwide"], max_length=6)
    )
    onsite_locations: list[str] = Field(default_factory=list, max_length=8)
    needs_sponsorship: bool = False
    skills: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("target_titles", "onsite_locations", "skills")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = re.sub(r"\s+", " ", s).strip()[:60]
            if s and s.lower() not in (x.lower() for x in out):
                out.append(s)
        return out


def _alternatives(words: list[str]) -> str:
    return "|".join(re.escape(w.lower()) for w in words)


def demo_profile(form: DemoForm) -> Profile:
    titles = _alternatives(form.target_titles)
    onsite = _alternatives(form.onsite_locations + [form.base]) or "(?!)"
    remote = "|".join(REGIONS[r] for r in form.remote_regions) or "(?!)"
    place = form.base or "an undisclosed location"
    auth = (
        "Needs visa sponsorship to work outside their home country."
        if form.needs_sponsorship
        else "Can work where the CV says they are authorised."
    )
    constraints = (
        f"- Based in {place}. {auth}\n"
        f"- Acceptable: remote roles open to {', '.join(form.remote_regions) or 'nowhere'}; "
        f"on-site in {', '.join(form.onsite_locations + [place])}.\n"
        f"- Target: {', '.join(form.target_titles)} at {form.seniority} level."
    )
    return Profile(
        candidate=Candidate(
            name=form.name, email="", base=form.base, cv="", constraints=constraints
        ),
        roles=Roles(
            include=[rf"\b({titles})\b"],
            exclude=[SENIORITY_EXCLUDE[form.seniority], *ALWAYS_EXCLUDE],
        ),
        locations=Locations(
            remote_allowed=remote,
            onsite_allowed=onsite,
            sponsor_hint="visa|sponsor|relocat|work permit" if form.needs_sponsorship else "(?!)",
        ),
        skills=[s.lower() for s in form.skills],
        scoring=Scoring(max_age_days=30),
        outreach=Outreach(min_score=7.0, signature=form.name),
    )


def load(kind: str, data: dict[str, Any]) -> Profile:
    if kind == "demo":
        return demo_profile(DemoForm.model_validate(data))
    return Profile.model_validate(data)
