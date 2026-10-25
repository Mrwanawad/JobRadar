"""LangGraph agents.

Screening   assess -> validate -+-> calibrate -> END
                     ^          |
                     +-- repair -+   (invalid JSON or schema, at most MAX_REPAIRS times)

Writing     draft -> critique -+-> finalize -> END
                     ^         |
                     +- revise -+   (critic found fabrications or a weak draft, at most
                                     MAX_REVISIONS times)

The LLM is injected, so tests drive both graphs with scripted replies. Every LLM call is
one OpenTelemetry GenAI span (jobradar.llm), and each node is a child span, so a trace
shows the whole decision path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from jobradar.config import Profile
from jobradar.llm import LLMResult, parse_json
from jobradar.outreach import draft_messages, finish_draft
from jobradar.score import MAX_DESC_CHARS, build_prompt, clamp

try:
    from opentelemetry import trace

    _tracer: Any = trace.get_tracer("jobradar.agent")
except ImportError:  # pragma: no cover
    _tracer = None

MAX_REPAIRS = 2
MAX_REVISIONS = 2
PASS_SCORE = 8.0


class LLM(Protocol):
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = ...,
        temperature: float | None = ...,
    ) -> LLMResult: ...


def _span(name: str):  # noqa: ANN202
    from contextlib import nullcontext

    return _tracer.start_as_current_span(name) if _tracer else nullcontext()


# ---- screening ------------------------------------------------------------------------------


class Assessment(BaseModel):
    """The screening contract. Extra keys are kept; these must be present and sane."""

    model_config = {"extra": "allow"}

    score: float = Field(ge=0, le=10)
    role_fit: float = Field(ge=0, le=10)
    seniority_fit: Literal["under", "match", "over"]
    location_fit: Literal["eligible", "needs_sponsorship", "ineligible"]
    visa_sponsorship: Literal["yes", "no", "unknown"] = "unknown"
    reasons: list[str] = Field(default_factory=list, max_length=5)
    red_flags: list[str] = Field(default_factory=list, max_length=5)
    pitch_angle: str = ""


class ScreenState(TypedDict, total=False):
    messages: list[dict[str, str]]
    raw: str
    by: str
    error: str
    repairs: int
    assessment: dict[str, Any]
    score: float


def screening_graph(llm: LLM) -> Any:
    def assess(s: ScreenState) -> ScreenState:
        with _span("screen.assess"):
            res = llm.chat(s["messages"], max_tokens=4000, temperature=0)
        return {"raw": res.text, "by": f"{res.provider}:{res.model}", "error": ""}

    def validate(s: ScreenState) -> ScreenState:
        try:
            data = parse_json(s["raw"])
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
            return {"assessment": Assessment.model_validate(data).model_dump(), "error": ""}
        except (ValueError, ValidationError) as e:
            return {"error": str(e)[:500]}

    def repair(s: ScreenState) -> ScreenState:
        fix = (
            f"Your reply did not match the required JSON ({s['error']}). "
            "Reply with only the corrected JSON object."
        )
        return {
            "messages": [
                *s["messages"],
                {"role": "assistant", "content": s["raw"]},
                {"role": "user", "content": fix},
            ],
            "repairs": s.get("repairs", 0) + 1,
        }

    def calibrate(s: ScreenState) -> ScreenState:
        a = s["assessment"]
        return {"score": clamp(a["score"], a)}

    def route(s: ScreenState) -> str:
        if not s.get("error"):
            return "calibrate"
        if s.get("repairs", 0) < MAX_REPAIRS:
            return "repair"
        raise ValueError(f"screening reply invalid after {MAX_REPAIRS} repairs: {s['error']}")

    g = StateGraph(ScreenState)
    g.add_node("assess", assess)
    g.add_node("validate", validate)
    g.add_node("repair", repair)
    g.add_node("calibrate", calibrate)
    g.add_edge(START, "assess")
    g.add_edge("assess", "validate")
    g.add_conditional_edges("validate", route, ["calibrate", "repair"])
    g.add_edge("repair", "assess")
    g.add_edge("calibrate", END)
    return g.compile()


def screen(
    llm: LLM, profile: Profile, cv: str, job: Mapping[str, Any]
) -> tuple[float, dict[str, Any], str]:
    with _span("agent.screen"):
        out = screening_graph(llm).invoke({"messages": build_prompt(profile, cv, job)})
    return out["score"], out["assessment"], out["by"]


# ---- writing --------------------------------------------------------------------------------

CRITIC_SYSTEM = """You review a job application written for one candidate. You are strict about
truth: every claim about the candidate must be supported by the CV. Reply with JSON only."""

CRITIC_TEMPLATE = """CANDIDATE CV
{cv}

JOB
{company} - {title}
{description}

DRAFT ({channel})
Subject: {subject}
{body}

Check the draft and return this JSON:
{{
  "fabrications": ["<each claim not supported by the CV; empty if none>"],
  "issues": ["<max 4 concrete fixes: generic opening, cliches, missing job hook, too long/short>"],
  "score": <0-10 overall quality; 8+ only if truthful, specific to this job and well written>
}}"""


class Critique(BaseModel):
    fabrications: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    score: float = Field(ge=0, le=10)

    @property
    def passes(self) -> bool:
        return not self.fabrications and self.score >= PASS_SCORE


class WriteState(TypedDict, total=False):
    messages: list[dict[str, str]]
    draft: dict[str, Any]
    critique: dict[str, Any]
    revisions: int
    best: dict[str, Any]
    best_score: float


def _chat_json(llm: LLM, messages: list[dict[str, str]]) -> dict[str, Any]:
    res = llm.chat(messages, max_tokens=4000, temperature=0)
    try:
        data = parse_json(res.text)
    except ValueError:
        retry = [
            *messages,
            {"role": "assistant", "content": res.text},
            {"role": "user", "content": "That was not valid JSON. Reply with only the JSON."},
        ]
        data = parse_json(llm.chat(retry, max_tokens=4000, temperature=0).text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def writer_graph(llm: LLM, cv: str, job: Mapping[str, Any], channel: str) -> Any:
    def draft(s: WriteState) -> WriteState:
        with _span("write.draft"):
            return {"draft": _chat_json(llm, s["messages"])}

    def critique(s: WriteState) -> WriteState:
        d = s["draft"]
        prompt = CRITIC_TEMPLATE.format(
            cv=cv,
            company=job["company"],
            title=job["title"],
            description=(job["description"] or "")[: MAX_DESC_CHARS // 2],
            channel=channel,
            subject=d.get("subject", ""),
            body=d.get("body", ""),
        )
        with _span("write.critique"):
            c = Critique.model_validate(
                _chat_json(
                    llm,
                    [
                        {"role": "system", "content": CRITIC_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                )
            )
        out: WriteState = {"critique": c.model_dump()}
        # A fabricating draft is never "best", however polished.
        effective = c.score if not c.fabrications else -1.0
        if effective > s.get("best_score", -2.0):
            out["best"], out["best_score"] = d, effective
        return out

    def revise(s: WriteState) -> WriteState:
        c = s["critique"]
        notes = [f"Remove or fix (not in the CV): {f}" for f in c["fabrications"]] + c["issues"]
        return {
            "messages": [
                *s["messages"],
                {"role": "assistant", "content": json.dumps(s["draft"], ensure_ascii=False)},
                {
                    "role": "user",
                    "content": "Revise the draft. Apply every point:\n- "
                    + "\n- ".join(notes)
                    + "\nReturn the same JSON shape.",
                },
            ],
            "revisions": s.get("revisions", 0) + 1,
        }

    def route(s: WriteState) -> str:
        c = Critique.model_validate(s["critique"])
        if c.passes or s.get("revisions", 0) >= MAX_REVISIONS:
            return END
        return "revise"

    g = StateGraph(WriteState)
    g.add_node("draft", draft)
    g.add_node("critique", critique)
    g.add_node("revise", revise)
    g.add_edge(START, "draft")
    g.add_edge("draft", "critique")
    g.add_conditional_edges("critique", route, ["revise", END])
    g.add_edge("revise", "draft")
    return g.compile()


def write(
    llm: LLM, profile: Profile, cv: str, job: Mapping[str, Any], channel: str
) -> tuple[str, str, dict[str, Any], int]:
    """(subject, body, final critique, revisions). Raises if every draft fabricated."""
    with _span("agent.write"):
        out = writer_graph(llm, cv, job, channel).invoke(
            {"messages": draft_messages(profile, cv, job, channel)}
        )
    if out.get("best_score", -1.0) < 0:
        raise ValueError("every draft contained claims the CV does not support")
    subject, body = finish_draft(profile, job, channel, out["best"])
    return subject, body, out["critique"], out.get("revisions", 0)
