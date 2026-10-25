"""LangGraph agents driven by a scripted LLM: every branch of both graphs."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")

from jobradar.agent import graphs  # noqa: E402
from jobradar.agent.profiles import DemoForm, demo_profile  # noqa: E402
from jobradar.llm import LLMResult  # noqa: E402

GOOD = {
    "score": 9,
    "role_fit": 9,
    "seniority_fit": "match",
    "location_fit": "eligible",
    "visa_sponsorship": "unknown",
    "reasons": ["LLM agents"],
    "red_flags": [],
    "pitch_angle": "Shipped voice agents",
}

JOB = {
    "uid": "u1",
    "company": "Acme",
    "title": "Senior AI Engineer",
    "location": "Remote",
    "remote": True,
    "salary": "",
    "posted_at": None,
    "description": "Build LLM agents.",
    "emails": [],
    "phones": [],
    "apply_url": "https://acme.example/apply",
    "url": "https://acme.example/job",
    "llm_json": GOOD,
}


class Script:
    """Returns the queued replies in order and records every prompt."""

    def __init__(self, *replies: object) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.prompts: list[list[dict[str, str]]] = []

    def chat(self, messages, *, max_tokens=0, temperature=None):  # noqa: ANN001
        self.prompts.append(messages)
        return LLMResult(self.replies.pop(0), "fake", "fake-1", {})


@pytest.fixture
def profile():
    return demo_profile(
        DemoForm(base="Cairo, Egypt", target_titles=["AI Engineer"], skills=["llm"])
    )


def test_screen_valid_first_time(profile):
    llm = Script(GOOD)
    score, data, by = graphs.screen(llm, profile, "cv text", JOB)
    assert score == 9 and data["pitch_angle"] == "Shipped voice agents" and by == "fake:fake-1"
    assert len(llm.prompts) == 1


def test_screen_repairs_bad_schema(profile):
    bad = {**GOOD, "location_fit": "maybe"}
    llm = Script(bad, GOOD)
    score, _, _ = graphs.screen(llm, profile, "cv", JOB)
    assert score == 9
    assert "did not match" in llm.prompts[1][-1]["content"]  # error fed back


def test_screen_gives_up_after_max_repairs(profile):
    llm = Script("no json", "still no", "nope")
    with pytest.raises(ValueError, match="invalid after"):
        graphs.screen(llm, profile, "cv", JOB)


def test_screen_calibrates_ineligible_location(profile):
    llm = Script({**GOOD, "location_fit": "ineligible"})
    score, _, _ = graphs.screen(llm, profile, "cv", JOB)
    assert score == 3


def test_demo_constraints_reach_the_prompt(profile):
    llm = Script(GOOD)
    graphs.screen(llm, profile, "cv", JOB)
    assert "Based in Cairo, Egypt" in llm.prompts[0][1]["content"]
    assert "Egyptian citizenship" not in llm.prompts[0][1]["content"]


DRAFT = {"subject": "", "body": "Hi Acme, I built LLM agents."}


def test_write_passes_first_draft(profile):
    llm = Script(DRAFT, {"fabrications": [], "issues": [], "score": 9})
    subject, body, critique, revisions = graphs.write(llm, profile, "cv", JOB, "manual")
    assert body.startswith("Hi Acme") and revisions == 0 and critique["score"] == 9


def test_write_revises_fabrication(profile):
    llm = Script(
        {"subject": "", "body": "I led a team of 50 at Google."},
        {"fabrications": ["led a team of 50 at Google"], "issues": [], "score": 9},
        DRAFT,
        {"fabrications": [], "issues": [], "score": 8.5},
    )
    _, body, _, revisions = graphs.write(llm, profile, "cv", JOB, "manual")
    assert "Google" not in body and revisions == 1
    assert "not in the CV" in llm.prompts[2][-1]["content"]


def test_write_keeps_best_when_revisions_run_out(profile):
    llm = Script(
        {"subject": "", "body": "v1"},
        {"fabrications": [], "issues": ["generic"], "score": 6},
        {"subject": "", "body": "v2"},
        {"fabrications": [], "issues": ["generic"], "score": 7},
        {"subject": "", "body": "v3"},
        {"fabrications": [], "issues": ["generic"], "score": 5},
    )
    _, body, _, revisions = graphs.write(llm, profile, "cv", JOB, "manual")
    assert body.startswith("v2") and revisions == 2


def test_write_refuses_when_every_draft_fabricates(profile):
    fab = {"fabrications": ["PhD"], "issues": [], "score": 9}
    llm = Script(DRAFT, fab, DRAFT, fab, DRAFT, fab)
    with pytest.raises(ValueError, match="does not support"):
        graphs.write(llm, profile, "cv", JOB, "manual")
