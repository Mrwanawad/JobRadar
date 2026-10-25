"""Local read-only dashboard: `jobradar dashboard` serves the DB as one page on 127.0.0.1."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jobradar.config import ROOT, load_profile
from jobradar.db import DEFAULT_PATH
from jobradar.outreach import local_midnight_utc

LOGS = ROOT / "out" / "logs"
PAGE = Path(__file__).with_name("dashboard.html")


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


def _grab(rx: str, text: str) -> str | None:
    m = re.search(rx, text)
    return m.group(1) if m else None


def _log_runs(limit: int = 14) -> list[dict[str, Any]]:
    """One entry per daily log: what each run fetched, scored, drafted and sent."""
    runs = []
    for f in sorted(LOGS.glob("*.log"), reverse=True)[:limit]:
        text = f.read_text(encoding="utf-8", errors="replace")
        runs.append(
            {
                "date": f.stem,
                "started": _grab(r"=== run (\S+)", text),
                "fetched": _grab(r"fetched (\d+)", text),
                "new": _grab(r"new (\d+)", text),
                "failed": _grab(r"failed sources (\d+)", text),
                "scored": _grab(r"scored (\d+)", text),
                "drafted": _grab(r"drafted (\{[^}]*\})", text),
                "send": _grab(r"=== send [^\n]*\n([^\n]+)", text),
            }
        )
    return runs


def snapshot(db_path: Path = DEFAULT_PATH) -> dict[str, Any]:
    profile = load_profile()
    o = profile.outreach
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        status = {
            r["status"]: r["n"]
            for r in _rows(conn, "SELECT status, COUNT(*) n FROM jobs GROUP BY status")
        }
        outbox = _rows(
            conn,
            "SELECT o.id, o.channel, o.recipient, o.status, o.created_at, o.sent_at, o.ref, "
            "j.company, j.title, j.location, j.llm_score, j.url "
            "FROM outbox o JOIN jobs j ON j.uid = o.uid ORDER BY o.id DESC",
        )
        shortlist = _rows(
            conn,
            "SELECT j.company, j.title, j.location, j.llm_score, j.url, j.source, j.llm_json, "
            "o.status AS outreach FROM jobs j LEFT JOIN outbox o ON o.uid = j.uid "
            "WHERE j.status='scored' ORDER BY j.llm_score DESC, j.posted_at DESC LIMIT 60",
        )
        for r in shortlist:
            res = json.loads(r.pop("llm_json") or "{}")
            r["fit"] = res.get("location_fit")
            r["pitch"] = res.get("pitch_angle", "")
        return {
            "today": date.today().isoformat(),
            "status": status,
            "total": sum(status.values()),
            "last_fetch": conn.execute("SELECT MAX(first_seen) FROM jobs").fetchone()[0],
            "sources": _rows(
                conn,
                "SELECT source, COUNT(*) total, SUM(status='scored') scored "
                "FROM jobs GROUP BY source ORDER BY total DESC",
            ),
            "scores": _rows(
                conn,
                "SELECT CAST(llm_score AS INT) bucket, COUNT(*) n FROM jobs "
                "WHERE status='scored' GROUP BY bucket ORDER BY bucket",
            ),
            "outbox": outbox,
            "outbox_counts": {
                r["status"]: r["n"]
                for r in _rows(conn, "SELECT status, COUNT(*) n FROM outbox GROUP BY status")
            },
            "sent_today": conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='sent' AND sent_at >= ?",
                (local_midnight_utc(),),
            ).fetchone()[0],
            "shortlist": shortlist,
            "config": {
                "min_score": o.min_score,
                "daily_cap": o.daily_cap,
                "dry_run_until": o.dry_run_until.isoformat() if o.dry_run_until else None,
                "dry_run": o.dry_run(),
            },
            "runs": _log_runs(),
        }
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif self.path == "/api/snapshot":
            body = json.dumps(snapshot(), ensure_ascii=False, default=str).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def serve(port: int = 0) -> ThreadingHTTPServer:
    """Bind to localhost only; port 0 lets the OS pick a free random port."""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
