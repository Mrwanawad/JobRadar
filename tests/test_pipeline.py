from datetime import UTC, datetime, timedelta

import httpx
import respx

from jobradar.config import load_companies, load_profile
from jobradar.db import DB, fingerprint
from jobradar.llm import LLMClient, Provider
from jobradar.models import Job
from jobradar.pipeline import prefilter_new, score_candidates
from jobradar.prefilter import Prefilter
from jobradar.score import clamp

PROFILE = load_profile()
PF = Prefilter(PROFILE)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def check(title, location="Remote", remote=True, desc="", age_days=1):
    posted = (NOW - timedelta(days=age_days)).isoformat()
    return PF.check(title, location, remote, desc, posted, now=NOW)


def test_titles():
    assert check("Senior AI Engineer").keep
    assert check("LLM Engineer, Agents").keep
    assert check("Machine Learning Engineer").keep
    assert check("Computer Vision Specialist").keep
    assert check("Voice AI Engineer").keep
    assert check("Senior Data Scientist").keep
    assert check("AI Technical Trainer").keep
    assert check("Principal Data Analyst").keep
    assert not check("Data Analyst").keep  # conditional needs seniority
    assert not check("Junior ML Engineer").keep
    assert not check("Account Executive, AI").keep
    assert not check("Frontend Engineer").keep
    assert not check("Head of AI").keep
    assert not check("Staff Product Designer, Conversational AI").keep
    assert not check("AI Support Engineer - Tokyo").keep
    assert not check("Senior GTM AI Engineer").keep


def test_locations():
    assert check("AI Engineer", "Remote (Anywhere)").keep
    assert check("AI Engineer", "Remote").keep
    assert not check("AI Engineer", "Remote (United States)").keep
    assert not check("AI Engineer", "Remote - US").keep
    assert check("AI Engineer", "Remote - EMEA").keep
    assert check("AI Engineer", "Anywhere in the World").keep
    assert check("AI Engineer", "Remote (Fully)").keep
    assert not check("AI Engineer", "San Francisco", remote=True).keep  # remote-in-US
    assert not check("AI Engineer", "Remote, London, UK").keep
    assert not check("AI Engineer", "Korea", remote=True).keep
    assert check("AI Engineer", "Riyadh, Saudi Arabia", remote=None).keep
    assert check("AI Engineer", "Cairo, Egypt", remote=False).keep
    assert not check("AI Engineer", "San Francisco, CA", remote=None).keep
    assert check("AI Engineer", "San Francisco, CA", remote=None, desc="We sponsor visas.").keep


def test_age():
    assert not check("AI Engineer", age_days=45).keep


def test_kw_score_prefers_skill_heavy_titles():
    assert PF.kw_score("LLM Agent Engineer", "rag python") > PF.kw_score("ML Engineer", "python")


def test_companies_yaml_loads():
    boards = load_companies()
    assert ("ashby", "openai", "OpenAI") in boards
    assert len(boards) > 40


def job(i, title="Senior AI Engineer", company="Acme", location="Remote"):
    return Job(
        source="test",
        external_id=str(i),
        company=company,
        title=title,
        location=location,
        remote=True,
        url=f"https://x/{i}",
        description="LLM agents with RAG in python",
        posted_at=datetime.now(UTC) - timedelta(days=1),
    )


def test_db_dedupe_and_prefilter():
    db = DB(":memory:")
    assert db.upsert_jobs([job(1), job(2, title="Frontend Engineer")]) == 2
    assert db.upsert_jobs([job(1)]) == 0  # seen before
    db.upsert_jobs([job(3, company="ACME")])  # same role, other board
    out = prefilter_new(db, PROFILE)
    assert out == {"candidate": 1, "filtered": 1, "duplicate": 1}
    assert fingerprint("Acme!", "Senior  AI Engineer") == "acme|senior ai engineer"


def test_clamp_caps_ineligible():
    assert clamp(9, {"location_fit": "ineligible"}) == 3
    assert clamp(9, {"location_fit": "needs_sponsorship", "visa_sponsorship": "no"}) == 3
    assert clamp(9, {"location_fit": "eligible"}) == 9
    assert clamp("bad", {}) == 0


REPLY = '{"score": 8.5, "location_fit": "eligible", "reasons": ["agents"]}'


@respx.mock
def test_score_candidates_writes_scores(monkeypatch):
    monkeypatch.setattr("jobradar.pipeline.cv_text", lambda p: "CV: LLM engineer")
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": REPLY}}],
                "usage": {"total_tokens": 10},
            },
        )
    )
    db = DB(":memory:")
    db.upsert_jobs([job(1), job(2, company="Beta")])
    prefilter_new(db, PROFILE)
    llm = LLMClient([Provider("t", "https://llm.test/v1", "k", "m")], http=httpx.Client())
    s = score_candidates(db, PROFILE, llm=llm, limit=1)
    assert s.scored == 1
    rows = db.shortlist(7)
    assert len(rows) == 1 and rows[0]["llm_score"] == 8.5
    assert rows[0]["scored_by"] == "t:m"
