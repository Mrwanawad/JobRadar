"""jobradar CLI: fetch, filter, score, list."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from jobradar.config import ROOT, load_profile
from jobradar.db import DB
from jobradar.outreach import draft_applications, sendable
from jobradar.pipeline import fetch_all, prefilter_new, score_candidates

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    # Windows consoles default to cp1252; job titles carry any Unicode.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )


@app.command()
def fetch() -> None:
    """Pull jobs from every board and feed into the DB, then prefilter new ones."""
    db = DB()
    s = fetch_all(db)
    typer.echo(f"fetched {s.fetched}, new {s.new}, failed sources {len(s.failed)}")
    for f in s.failed:
        typer.echo(f"  FAIL {f}")
    typer.echo(f"prefilter: {prefilter_new(db, load_profile())}")


@app.command()
def refilter() -> None:
    """Re-run the prefilter on all unscored jobs, e.g. after editing profile.yaml."""
    db = DB()
    typer.echo(f"reset {db.reset_unscored()} jobs")
    typer.echo(f"prefilter: {prefilter_new(db, load_profile())}")


@app.command()
def score(limit: int = typer.Option(None, help="Max LLM calls (default from profile)")) -> None:
    """LLM-score candidate jobs, best keyword matches first."""
    db, profile = DB(), load_profile()
    s = score_candidates(
        db, profile, limit=limit, on_scored=lambda name, sc: typer.echo(f"  {sc:4.1f}  {name}")
    )
    typer.echo(f"scored {s.scored}")
    for e in s.errors:
        typer.echo(f"  ERR {e}")
    if s.stopped:
        typer.echo(f"stopped early, LLM quota exhausted: {s.stopped}")


@app.command()
def run(limit: int = typer.Option(None, help="Max LLM calls (default from profile)")) -> None:
    """fetch + score + draft applications."""
    fetch()
    score(limit)
    draft(limit=None)
    top(min_score=None, n=25, xlsx=None)


@app.command()
def draft(limit: int = typer.Option(None, help="Max drafts (default from profile)")) -> None:
    """Draft an application (email, WhatsApp or form cover letter) per shortlisted job."""
    s = draft_applications(DB(), load_profile(), limit=limit)
    typer.echo(f"drafted {s.drafted}")
    for e in s.errors:
        typer.echo(f"  ERR {e}")
    if s.stopped:
        typer.echo(f"stopped early, LLM quota exhausted: {s.stopped}")


@app.command()
def outbox(
    status: str = typer.Option(None, help="draft | sent | failed | skipped"),
    channel: str = typer.Option(None, help="email | whatsapp | manual"),
    full: bool = typer.Option(False, help="Print message bodies"),
) -> None:
    """List drafted applications. `manual` ones are yours to paste into the apply form."""
    rows = DB().outbox(status, channel)
    for r in rows:
        typer.echo(
            f"#{r['id']:<4} {r['status']:<8} {r['channel']:<8} {r['llm_score']:4.1f}  "
            f"{r['company']} - {r['title']}\n      to: {r['recipient']}"
        )
        if full:
            if r["subject"]:
                typer.echo(f"      subject: {r['subject']}")
            typer.echo("      " + r["body"].replace("\n", "\n      ") + "\n")
    typer.echo(f"{len(rows)} items")


@app.command("sendable")
def sendable_cmd() -> None:
    """JSON of drafts allowed out now (dry run, daily cap, dedupe applied). Used by the sender."""
    s = sendable(DB(), load_profile())
    typer.echo(json.dumps({"blocked": s.blocked, "items": s.items}, ensure_ascii=False, indent=1))


@app.command()
def dashboard(
    port: int = typer.Option(0, help="0 = random free port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Serve a read-only dashboard of the agent's work on localhost."""
    import webbrowser

    from jobradar.dashboard import serve

    server = serve(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    typer.echo(f"JobRadar dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@app.command()
def mark(
    outbox_id: int,
    status: str = typer.Argument(..., help="sent | failed | skipped | draft"),
    ref: str = typer.Option("", help="Message id or error text"),
) -> None:
    """Record what happened to an outbox item."""
    if status not in {"sent", "failed", "skipped", "draft"}:
        raise typer.BadParameter("status must be sent, failed, skipped or draft")
    if not DB().mark(outbox_id, status, ref):
        raise typer.Exit(f"no outbox item #{outbox_id}")
    typer.echo(f"#{outbox_id} -> {status}")


@app.command()
def top(
    min_score: float = typer.Option(None, "--min", help="Default: profile shortlist_min"),
    n: int = 25,
    xlsx: Path = typer.Option(None, help="Also write the shortlist to this .xlsx file"),
) -> None:
    """Show the shortlist."""
    db, profile = DB(), load_profile()
    rows = db.shortlist(profile.scoring.shortlist_min if min_score is None else min_score, n)
    for r in rows:
        res = json.loads(r["llm_json"])
        contact = ", ".join(json.loads(r["emails"]) + json.loads(r["phones"])) or "-"
        typer.echo(
            f"{r['llm_score']:4.1f}  {r['company']} - {r['title']}\n"
            f"      {r['location'] or '?'} | {res.get('location_fit')} | contact: {contact}\n"
            f"      {r['url']}\n"
            f"      + {'; '.join(res.get('reasons', []))}\n"
            f"      - {'; '.join(res.get('red_flags', []))}"
        )
    typer.echo(f"{len(rows)} jobs. DB: {db.counts()}")
    if xlsx:
        _write_xlsx(db, rows, xlsx)
        typer.echo(f"wrote {xlsx}")


def _write_xlsx(db: DB, rows: list, path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "shortlist"
    cols = [
        "score",
        "company",
        "title",
        "location",
        "location_fit",
        "salary",
        "url",
        "reasons",
        "red_flags",
        "pitch_angle",
        "emails",
        "phones",
        "posted_at",
        "source",
        "apply_via",
        "outreach_status",
        "application_text",
    ]
    ws.append(cols)
    for r in rows:
        res = json.loads(r["llm_json"])
        d = db.draft_for(r["uid"])
        ws.append(
            [
                r["llm_score"],
                r["company"],
                r["title"],
                r["location"],
                res.get("location_fit"),
                r["salary"],
                r["url"],
                "; ".join(res.get("reasons", [])),
                "; ".join(res.get("red_flags", [])),
                res.get("pitch_angle", ""),
                ", ".join(json.loads(r["emails"])),
                ", ".join(json.loads(r["phones"])),
                r["posted_at"],
                r["source"],
                d and f"{d['channel']}: {d['recipient']}",
                d and d["status"],
                d and d["body"],
            ]
        )
    path = path if path.is_absolute() else ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


if __name__ == "__main__":
    app()
