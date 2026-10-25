"""Common job shape every source parser produces."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field, computed_field

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_BLOCK = re.compile(r"<\s*(br|/p|p|/div|/li|li|/h\d)[^>]*>", re.I)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # some feeds double-escape
    s = _BLOCK.sub("\n", s)
    s = html.unescape(_TAG.sub("", s))
    s = _WS.sub(" ", s)
    return _NL.sub("\n\n", "\n".join(line.strip() for line in s.splitlines())).strip()


def from_epoch(v: int | float | str | None, ms: bool = False) -> datetime | None:
    if v in (None, "", 0, "0"):
        return None
    n = float(v)
    return datetime.fromtimestamp(n / 1000 if ms else n, tz=UTC)


def from_iso(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class Job(BaseModel):
    source: str  # greenhouse, lever, remoteok, hn, ...
    external_id: str
    company: str
    title: str
    location: str = ""
    remote: bool | None = None
    url: str
    apply_url: str = ""
    description: str = ""
    posted_at: datetime | None = None
    salary: str = ""
    tags: list[str] = Field(default_factory=list)
    # contacts published in the posting itself (HN posts, WWR descriptions)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def uid(self) -> str:
        return hashlib.sha1(f"{self.source}:{self.external_id}".encode()).hexdigest()[:16]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
_PHONE = re.compile(r"(?:\+|00)\d[\d\s().-]{7,16}\d")
_JUNK_EMAIL = ("example.com", "sentry.io", "wixpress.com", ".png", ".jpg")


def extract_contacts(text: str) -> tuple[list[str], list[str]]:
    emails = []
    for e in _EMAIL.findall(text):
        e = e.strip(".").lower()
        if e not in emails and not any(j in e for j in _JUNK_EMAIL):
            emails.append(e)
    phones = []
    for p in _PHONE.findall(text):
        norm = "+" + re.sub(r"\D", "", p).removeprefix("00")
        if 10 <= len(norm) <= 16 and norm not in phones:
            phones.append(norm)
    return emails, phones
