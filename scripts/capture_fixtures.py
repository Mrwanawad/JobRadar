"""Hit each external source once, save a trimmed real response to tests/fixtures/.

Run: uv run python scripts/capture_fixtures.py
Prints status + field names per source so endpoints are verified, not assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
UA = {"User-Agent": "jobradar-verify/0.1 (personal job search; contact via repo owner)"}

# name -> (url, kind). kind: json | text
SOURCES: dict[str, tuple[str, str]] = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true",
        "json",
    ),
    "lever": ("https://api.lever.co/v0/postings/palantir?mode=json", "json"),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true",
        "json",
    ),
    "smartrecruiters": (
        "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings?limit=5",
        "json",
    ),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/huggingface", "json"),
    "hn_whoishiring": (
        "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=3",
        "json",
    ),
    "remoteok": ("https://remoteok.com/api", "json"),
    "himalayas": ("https://himalayas.app/jobs/api?limit=5", "json"),
    "yc_hiring": ("https://yc-oss.github.io/api/companies/hiring.json", "json"),
    "wwr_rss": ("https://weworkremotely.com/remote-jobs.rss", "text"),
}


def trim(data: object, n: int = 3) -> object:
    """Keep fixtures small: first n items of any top-level or 'jobs'/'content'/'hits' list."""
    if isinstance(data, list):
        return data[:n]
    if isinstance(data, dict):
        return {k: (v[:n] if isinstance(v, list) else v) for k, v in data.items()}
    return data


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=UA, timeout=45, follow_redirects=True) as c:
        for name, (url, kind) in SOURCES.items():
            try:
                r = c.get(url)
                status = r.status_code
                if status != 200:
                    print(f"FAIL {name}: HTTP {status}")
                    continue
                if kind == "json":
                    data = trim(r.json())
                    (OUT / f"{name}.json").write_text(json.dumps(data, indent=1)[:200_000])
                    sample = data[0] if isinstance(data, list) and data else data
                    keys = list(sample) if isinstance(sample, dict) else type(sample).__name__
                    print(f"OK   {name}: keys={keys}")
                else:
                    (OUT / f"{name}.xml").write_text(r.text[:60_000])
                    print(f"OK   {name}: {len(r.text)} chars")
            except Exception as e:  # one failing source never stops the rest
                print(f"FAIL {name}: {e!r}")


if __name__ == "__main__":
    main()
