"""Agent activities against in-memory fakes of Postgres and Elasticsearch."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("temporalio")

from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment  # noqa: E402

from jobradar.agent.activities import QUOTA_EXHAUSTED, Agent  # noqa: E402
from jobradar.agent.profiles import DemoForm, demo_profile  # noqa: E402
from jobradar.agent.search import rrf  # noqa: E402
from jobradar.agent.store import StoredProfile  # noqa: E402
from jobradar.llm import LLMError, LLMResult  # noqa: E402
from jobradar.v1 import radar_pb2 as pb  # noqa: E402

NOW = datetime.now(UTC)


def job(uid: str, title: str, location: str = "Remote", fp: str | None = None) -> dict:
    return {
        "uid": uid,
        "fingerprint": fp or uid,
        "company": "Acme",
        "title": title,
        "location": location,
        "remote": None,
        "description": "LLM agents in production",
        "posted_at": NOW,
        "salary": "",
        "url": f"https://acme.example/{uid}",
        "apply_url": "",
        "emails": ["jobs@acme.example"],
        "phones": [],
        "tags": [],
    }


class FakeStore:
    def __init__(self, kind: str = "demo") -> None:
        form = DemoForm(base="Cairo", target_titles=["AI Engineer"], skills=["llm"])
        self.sp = StoredProfile("p1", kind, demo_profile(form), "CV: built LLM agents")
        self.jobs = {
            "a": job("a", "Senior AI Engineer"),
            "b": job("b", "Sales Manager"),
            "c": job("c", "AI Engineer", fp="a"),  # cross-post of a
            "d": job("d", "AI Engineer", location="San Francisco, CA"),
        }
        self.verdicts: dict[str, tuple] = {}
        self.scores: dict[str, float] = {}
        self.drafts: list[tuple] = []

    def profile(self, pid):  # noqa: ANN001
        return self.sp

    def queued_fingerprints(self, pid):  # noqa: ANN001
        return set()

    def unjudged_jobs(self, pid, days):  # noqa: ANN001
        return iter(self.jobs.values())

    def save_verdicts(self, pid, rows):  # noqa: ANN001
        self.verdicts.update({r[0]: r[1:] for r in rows})

    def job(self, uid):  # noqa: ANN001
        return self.jobs[uid]

    def job_with_assessment(self, pid, uid):  # noqa: ANN001
        return {**self.jobs[uid], "llm_score": 9, "llm_json": {"pitch_angle": "agents"}}

    def save_score(self, pid, uid, score, data, by):  # noqa: ANN001
        self.scores[uid] = score

    def save_draft(self, *args):  # noqa: ANN002
        self.drafts.append(args)
        return 42


class Script:
    def __init__(self, *replies: object) -> None:
        self.replies = [r if isinstance(r, str | Exception) else json.dumps(r) for r in replies]

    def chat(self, messages, **kw):  # noqa: ANN001, ANN003
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResult(r, "fake", "fake-1", {})


def agent(store: FakeStore, *replies: object) -> Agent:
    a = Agent(store=store, index=None)  # type: ignore[arg-type]
    a._llm = Script(*replies)  # type: ignore[assignment]
    return a


def test_prefilter_classifies_and_dedupes():
    store = FakeStore()
    res = ActivityEnvironment().run(agent(store).prefilter, pb.PrefilterInput(profile_id="p1"))
    assert store.verdicts["a"][0] == "candidate"
    assert store.verdicts["b"][0] == "filtered"  # not a target title
    assert store.verdicts["c"][0] == "duplicate"  # same fingerprint as a
    assert store.verdicts["d"][0] == "filtered"  # on-site abroad, no sponsorship
    assert (res.candidates, res.filtered, res.duplicates) == (1, 2, 1)


SCORE = {
    "score": 8.5,
    "role_fit": 9,
    "seniority_fit": "match",
    "location_fit": "eligible",
    "reasons": [],
    "red_flags": [],
    "pitch_angle": "agents",
}


def test_score_job_saves_result():
    store = FakeStore()
    res = ActivityEnvironment().run(
        agent(store, SCORE).score_job, pb.ScoreJobInput(profile_id="p1", job_uid="a")
    )
    assert res.score == pytest.approx(8.5) and store.scores["a"] == 8.5


def test_quota_exhaustion_is_a_typed_non_retryable_failure():
    store = FakeStore()
    with pytest.raises(ApplicationError) as e:
        ActivityEnvironment().run(
            agent(store, LLMError("all LLM providers failed")).score_job,
            pb.ScoreJobInput(profile_id="p1", job_uid="a"),
        )
    assert e.value.type == QUOTA_EXHAUSTED and e.value.non_retryable


def test_demo_drafts_are_manual_never_email():
    store = FakeStore("demo")
    res = ActivityEnvironment().run(
        agent(
            store,
            {"subject": "Hi", "body": "I built LLM agents."},
            {"fabrications": [], "issues": [], "score": 9},
        ).draft_application,
        pb.DraftApplicationInput(profile_id="p1", job_uid="a"),
    )
    assert res.channel == "manual" and res.outbox_id == 42
    assert store.drafts[0][2] == "manual"  # despite jobs@acme.example in the posting


def test_rrf_rewards_agreement():
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]])
    assert set(fused) == {"a", "b", "c", "d"}
    assert fused["a"] == fused["b"] > fused["c"] == fused["d"]
