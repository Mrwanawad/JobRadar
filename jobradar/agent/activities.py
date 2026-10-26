"""Temporal activities for the `jobradar-agent` task queue.

Names and payloads match what the Go RadarWorkflow calls (orchestrator/internal/workflows):
protobuf messages from proto/jobradar/v1, carried as json/protobuf. Activities are sync
and run on the worker's thread pool; the LLM client is shared and serialized, because its
free-tier throttling state (requests per minute, parked keys) is per process.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from jobradar.agent import graphs
from jobradar.agent.search import JobIndex
from jobradar.agent.store import AgentStore
from jobradar.llm import CAPTURE_CONTENT, LLMClient, LLMError, LLMResult
from jobradar.outreach import channel_for
from jobradar.prefilter import Prefilter
from jobradar.v1 import radar_pb2 as pb

QUOTA_EXHAUSTED = "QuotaExhausted"  # workflows.QuotaExhausted on the Go side


class SerializedLLM:
    """One LLM call at a time per worker process."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm, self.lock = llm, threading.Lock()

    def chat(self, messages: list[dict[str, str]], **kw: Any) -> LLMResult:
        with self.lock:
            return self.llm.chat(messages, **kw)


@dataclass
class Agent:
    store: AgentStore
    index: JobIndex
    llm_factory: Callable[[], LLMClient] = LLMClient

    def __post_init__(self) -> None:
        self._llm: SerializedLLM | None = None

    @property
    def llm(self) -> SerializedLLM:
        if self._llm is None:  # built lazily: a worker without keys can still prefilter
            self._llm = SerializedLLM(self.llm_factory())
        return self._llm

    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.prefilter,
            self.index_jobs,
            self.match_jobs,
            self.score_job,
            self.draft_application,
            self.top_jobs,
        ]

    # ---- rules ---------------------------------------------------------------------------

    @activity.defn(name="Prefilter")
    def prefilter(self, inp: pb.PrefilterInput) -> pb.PrefilterResult:
        sp = self.store.profile(inp.profile_id)
        pf = Prefilter(sp.profile)
        seen = self.store.queued_fingerprints(inp.profile_id)
        out = pb.PrefilterResult()
        batch: list[tuple[str, str, str, int]] = []
        for i, j in enumerate(
            self.store.unjudged_jobs(inp.profile_id, sp.profile.scoring.max_age_days)
        ):
            v = pf.check(
                j["title"],
                j["location"],
                j["remote"],
                j["description"],
                j["posted_at"].isoformat() if j["posted_at"] else None,
            )
            fp = j["fingerprint"]
            if not v.keep:
                batch.append((j["uid"], "filtered", v.reason, 0))
                out.filtered += 1
            elif fp in seen:
                batch.append((j["uid"], "duplicate", "same company+title already queued", 0))
                out.duplicates += 1
            else:
                seen.add(fp)
                batch.append((j["uid"], "candidate", v.reason, v.kw_score))
                out.candidates += 1
            if len(batch) >= 500:
                self.store.save_verdicts(inp.profile_id, batch)
                batch.clear()
                activity.heartbeat(i)
        self.store.save_verdicts(inp.profile_id, batch)
        return out

    # ---- retrieval -----------------------------------------------------------------------

    @activity.defn(name="IndexJobs")
    def index_jobs(self, inp: pb.IndexJobsInput) -> pb.IndexJobsResult:
        jobs = self.store.jobs_to_index(inp.batch_size or 256, inp.profile_id)
        activity.heartbeat(len(jobs))
        n = self.index.index(jobs)
        self.store.mark_indexed([j["uid"] for j in jobs])
        return pb.IndexJobsResult(indexed=n)

    @activity.defn(name="MatchJobs")
    def match_jobs(self, inp: pb.MatchJobsInput) -> pb.MatchJobsResult:
        sp = self.store.profile(inp.profile_id)
        cands = self.store.candidates(inp.profile_id)
        if not cands:
            return pb.MatchJobsResult()
        query = " ".join([*sp.profile.skills, *_title_terms(sp.profile)])
        ranked = self.index.match(query, sp.cv_text, [c["job_uid"] for c in cands], inp.top_k)
        if not ranked:  # index empty or unreachable: fall back to the keyword prefilter score
            best = sorted(cands, key=lambda c: c["kw_score"], reverse=True)[: inp.top_k]
            ranked = [(c["job_uid"], float(c["kw_score"])) for c in best]
        self.store.set_match_scores(inp.profile_id, dict(ranked))
        return pb.MatchJobsResult(job_uids=[uid for uid, _ in ranked])

    # ---- LLM agents ----------------------------------------------------------------------

    @activity.defn(name="ScoreJob")
    def score_job(self, inp: pb.ScoreJobInput) -> pb.ScoreJobResult:
        sp = self.store.profile(inp.profile_id)
        job = self.store.job(inp.job_uid)
        with _heartbeating(), _content_policy(sp.kind):
            score, data, by = graphs.screen(self.llm, sp.profile, sp.cv_text, job)
        self.store.save_score(inp.profile_id, inp.job_uid, score, data, by)
        return pb.ScoreJobResult(job_uid=inp.job_uid, score=score, scored_by=by)

    @activity.defn(name="DraftApplication")
    def draft_application(self, inp: pb.DraftApplicationInput) -> pb.DraftApplicationResult:
        sp = self.store.profile(inp.profile_id)
        job = self.store.job_with_assessment(inp.profile_id, inp.job_uid)
        channel, recipient = channel_for(job)
        if sp.kind == "demo":
            channel, recipient = "manual", job["apply_url"] or job["url"]  # never sent
        with _heartbeating(), _content_policy(sp.kind):
            try:
                subject, body, critique, revisions = graphs.write(
                    self.llm, sp.profile, sp.cv_text, job, channel
                )
            except ValueError as e:  # fabricated every time: not worth retrying
                raise ApplicationError(str(e), type="DraftRejected", non_retryable=True) from e
        oid = self.store.save_draft(
            inp.profile_id, inp.job_uid, channel, recipient, subject, body, critique, revisions
        )
        return pb.DraftApplicationResult(
            outbox_id=oid, channel=channel, critique_score=critique["score"], revisions=revisions
        )

    @activity.defn(name="TopJobs")
    def top_jobs(self, inp: pb.TopJobsInput) -> pb.TopJobsResult:
        rows = self.store.top(inp.profile_id, inp.limit or 20)
        return pb.TopJobsResult(
            jobs=[
                pb.ScoredJob(
                    job_uid=r["uid"],
                    company=r["company"],
                    title=r["title"],
                    location=r["location"],
                    url=r["url"],
                    score=r["llm_score"] or 0,
                    pitch_angle=r["pitch"] or "",
                    outbox_id=r["outbox_id"] or 0,
                )
                for r in rows
            ]
        )


def _title_terms(profile: Any) -> list[str]:
    """Readable words from the include regexes, for the BM25 query."""
    words: list[str] = []
    for rx in profile.roles.include:
        words += re.findall(r"[a-z][a-z .+-]{1,40}", rx.replace("\\b", " ").replace("\\", ""))
    return [w.strip() for w in words if w.strip()][:20]


HEARTBEAT_EVERY = 20.0  # seconds; the workflow's heartbeat timeout is 2 minutes


@contextlib.contextmanager
def _heartbeating() -> Iterator[None]:
    """Heartbeat from a side thread while an LLM activity waits on the shared client.

    A call can sit behind the serialization lock or free-tier backoff for minutes; that
    is progress, not a hang. If the process dies the heartbeats stop and Temporal retries
    elsewhere. The thread runs in a copy of this activity's context so heartbeat() finds it.
    """
    stop = threading.Event()
    ctx = contextvars.copy_context()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_EVERY):
            ctx.run(activity.heartbeat)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


class _content_policy:  # noqa: N801
    """Demo visitors' CVs never reach trace backends; LLM errors become typed failures."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __enter__(self) -> None:
        self.token = CAPTURE_CONTENT.set(CAPTURE_CONTENT.get() and self.kind != "demo")

    def __exit__(self, typ: Any, exc: Any, tb: Any) -> bool:
        CAPTURE_CONTENT.reset(self.token)
        if isinstance(exc, LLMError):
            # Every provider is out of quota: stop the run's scoring, do not retry.
            raise ApplicationError(str(exc), type=QUOTA_EXHAUSTED, non_retryable=True) from exc
        return False
