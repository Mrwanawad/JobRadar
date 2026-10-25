"""Free first pass: title, location and age rules from profile.yaml. Spends no LLM quota."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobradar.config import Profile

_NOT_PLACE = re.compile(r"\b(remote|fully|hybrid|work from home|wfh|first|only)\b|[\W\d_]+", re.I)


@dataclass
class Verdict:
    keep: bool
    reason: str
    kw_score: int = 0


class Prefilter:
    def __init__(self, profile: Profile) -> None:
        self.p = profile
        r, loc = profile.roles, profile.locations
        self.include = [profile.rx(x) for x in r.include]
        self.conditional = [profile.rx(x) for x in r.conditional]
        self.cond_req = profile.rx(r.conditional_requires)
        self.exclude = [profile.rx(x) for x in r.exclude]
        self.remote_allowed = profile.rx(loc.remote_allowed)
        self.onsite_allowed = profile.rx(loc.onsite_allowed)
        self.sponsor = profile.rx(loc.sponsor_hint)
        self.skills = [s.lower() for s in profile.skills]

    def title_ok(self, title: str) -> tuple[bool, str]:
        if any(x.search(title) for x in self.exclude):
            return False, "title excluded"
        if any(x.search(title) for x in self.include):
            return True, "title match"
        if any(x.search(title) for x in self.conditional):
            if self.cond_req.search(title):
                return True, "title match (conditional)"
            return False, "conditional title without seniority"
        return False, "title not a target role"

    def location_ok(self, location: str, remote: bool | None, description: str) -> tuple[bool, str]:
        loc = location or ""
        if self.onsite_allowed.search(loc):
            return True, "location in Egypt/Gulf"
        if self.remote_allowed.search(loc):
            return True, "remote, open region"
        if remote or "remote" in loc.lower():
            # "Remote" alone is open; "Remote - US" or a remote flag on "San Francisco"
            # means remote within that country, which needs local work authorisation.
            if not _NOT_PLACE.sub("", loc).strip():
                return True, "remote"
            return False, "remote but locked to another region"
        if not loc.strip():
            return True, "location unknown"
        if self.sponsor.search(description or ""):
            return True, "on-site abroad, mentions sponsorship/relocation"
        return False, "on-site abroad, no sponsorship mentioned"

    def kw_score(self, title: str, description: str) -> int:
        t, d = title.lower(), (description or "").lower()
        return sum(3 if s in t else 1 for s in self.skills if s in t or s in d)

    def check(
        self,
        title: str,
        location: str,
        remote: bool | None,
        description: str,
        posted_at: str | None,
        now: datetime | None = None,
    ) -> Verdict:
        if posted_at:
            now = now or datetime.now(UTC)
            age = now - datetime.fromisoformat(posted_at)
            if age > timedelta(days=self.p.scoring.max_age_days):
                return Verdict(False, f"older than {self.p.scoring.max_age_days} days")
        ok, why = self.title_ok(title)
        if not ok:
            return Verdict(False, why)
        ok, where = self.location_ok(location, remote, description)
        if not ok:
            return Verdict(False, where)
        return Verdict(True, f"{why}; {where}", self.kw_score(title, description))
