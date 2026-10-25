import json
from pathlib import Path

from jobradar.ats import PARSERS
from jobradar.models import extract_contacts, html_to_text
from jobradar.sources import parse_himalayas, parse_hn_thread, parse_remoteok, parse_wwr

FX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def check(jobs, source, n_min=1):
    assert len(jobs) >= n_min
    for j in jobs:
        assert j.source == source
        assert j.title and j.url.startswith("http") and j.external_id
        assert len(j.uid) == 16
    assert len({j.uid for j in jobs}) == len(jobs)


def test_greenhouse():
    jobs = PARSERS["greenhouse"](load("greenhouse.json"), "Anthropic")
    check(jobs, "greenhouse")
    assert jobs[0].posted_at and jobs[0].posted_at.year == 2024  # first_published, not updated_at
    assert "<" not in jobs[0].description[:200]


def test_lever():
    jobs = PARSERS["lever"](load("lever.json"), "Palantir")
    check(jobs, "lever")
    assert jobs[0].location == "Singapore, Singapore"
    assert jobs[0].posted_at is not None


def test_ashby():
    jobs = PARSERS["ashby"](load("ashby.json"), "OpenAI")
    check(jobs, "ashby")
    assert jobs[0].salary.startswith("$")


def test_smartrecruiters():
    jobs = PARSERS["smartrecruiters"](load("smartrecruiters.json"), "BoschGroup")
    check(jobs, "smartrecruiters")
    assert jobs[0].location == "Blaj, Alba, Romania"


def test_workable():
    jobs = PARSERS["workable"](load("workable.json"), "huggingface")
    check(jobs, "workable")
    assert jobs[0].company == "Hugging Face"
    assert "Paris" in jobs[0].location


def test_remoteok_skips_legal_notice():
    data = load("remoteok.json")
    jobs = parse_remoteok(data)
    check(jobs, "remoteok")
    assert len(jobs) == len(data) - 1
    assert all(j.remote for j in jobs)


def test_wwr():
    jobs = parse_wwr((FX / "wwr_rss.xml").read_bytes())
    check(jobs, "wwr")
    assert jobs[0].company == "ATOM PARTNERS LIMITED"
    assert jobs[0].title == "Junior Crypto Trader (Remote)"
    assert jobs[0].posted_at is not None


def test_himalayas():
    jobs = parse_himalayas(load("himalayas.json"))
    check(jobs, "himalayas")
    assert jobs[0].location == "Remote (United States)"


def test_hn_thread():
    jobs = parse_hn_thread(load("hn_item.json"))
    check(jobs, "hn")
    assert jobs[0].company == "PrairieLearn"
    assert "Full-Stack" in jobs[0].title
    assert jobs[0].remote is True


def test_html_to_text():
    assert html_to_text("<p>a&amp;b</p><p>c<br>d</p>") == "a&b\n\nc\nd"


def test_extract_contacts():
    e, p = extract_contacts("mail jobs@acme.io or +20 100 000 0000, not logo@2x.png")
    assert e == ["jobs@acme.io"]
    assert p == ["+201000000000"]
