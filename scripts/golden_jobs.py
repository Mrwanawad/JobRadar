"""Write the Python parsers' output on every fixture to tests/fixtures/golden/.

The Go ingest worker's parsers are tested against these files, so both implementations
stay field-for-field identical. Re-run after changing a parser or a fixture:

    uv run python scripts/golden_jobs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from jobradar.ats import PARSERS
from jobradar.sources import parse_himalayas, parse_hn_thread, parse_remoteok, parse_wwr

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT = FIX / "golden"


def dump(name: str, jobs: list) -> None:
    rows = [j.model_dump(mode="json") for j in jobs]
    (OUT / f"{name}.json").write_text(
        json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"{name}: {len(rows)} jobs")


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for ats, parse in PARSERS.items():
        dump(ats, parse(load(f"{ats}.json"), "Fixture Co"))
    dump("remoteok", parse_remoteok(load("remoteok.json")))
    dump("himalayas", parse_himalayas(load("himalayas.json")))
    dump("hn", parse_hn_thread(load("hn_item.json")))
    dump("wwr", parse_wwr((FIX / "wwr_rss.xml").read_bytes()))


if __name__ == "__main__":
    main()
