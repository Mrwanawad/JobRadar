# Verified endpoints (2026-10-14)

Fixtures live in `tests/fixtures/`, refreshed with `uv run python scripts/capture_fixtures.py`.

| Source | URL | Status | Notes |
|---|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | 200 | top keys `jobs`, `meta`; `updated_at` = edit time, not post time |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` | 200 | list; `createdAt` ms epoch, `workplaceType`, `country`, `hostedUrl`, `descriptionPlain` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | 200 | `publishedAt`, `isRemote`, `workplaceType`, `jobUrl`, `descriptionPlain`, `compensation` |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/{slug}/postings` | 200 | `offset/limit/totalFound/content[]`; `releasedDate`; description needs per-posting call |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/{slug}` | 200 | widget JSON exists: `published_on`, `telecommuting`, `country`, `city`, `url`; no description |
| HN Who is hiring | `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring` | 200 | then `/items/{id}`: top-level `children[]` = postings, `text` HTML, first line `Company | Role | Location` (fixture `hn_item.json`); some hits lack `title` |
| RemoteOK | `https://remoteok.com/api` | 200 | first element is legal notice; keep attribution link |
| We Work Remotely | `https://weworkremotely.com/remote-jobs.rss` | 200 | RSS |
| Himalayas | `https://himalayas.app/jobs/api?limit=N` | 200 | `jobs[]`, cursor pagination |
| YC mirror | `https://yc-oss.github.io/api/companies/hiring.json` | 200 | 1493 cos; `isHiring, batch, team_size, website, regions, one_liner` |
| Wamda RSS | `https://www.wamda.com/feed` | 200 | funding news, MENA |
| TechCrunch venture RSS | `https://techcrunch.com/category/venture/feed/` | 200 | funding news |
| MAGNiTT news | `https://magnitt.com/news/feed` | **403** | skip (manual list in README) |
| DOL OFLC page | `https://www.dol.gov/agencies/eta/foreign-labor/performance` | **403 to scripts** | blocks non-browser clients |
| DOL LCA file | `https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx` | unverified | URL read from DOL page via WebFetch; script download may be blocked -> need manual download fallback |
| Getro public API | `https://api.getro.com/` | 404 | no public endpoint; per-board scrape not planned |

## LLM (free tiers, checked 2026-10-15)
Client: `jobradar/llm.py`, OpenAI-compatible, providers tried in `LLM_PROVIDERS` order.
- Primary: Gemini `gemini-3.8-flash` via `https://generativelanguage.googleapis.com/v1beta/openai`; `reasoning_effort` minimal/low/medium/high. Free limits per project, read them in AI Studio (no public table).
- Alt: Groq `openai/gpt-oss-120b` (user reports it unavailable to them 2026-10-15; status page green). Free limits 30 RPM, 1K RPD, 8K TPM, 200K TPD. Params `reasoning_effort` (low/medium/high), `include_reasoning=false`; `reasoning_format` not supported.
- Fallback: OpenRouter `nvidia/nemotron-3-ultra-550b-a55b:free` (`gpt-oss-120b:free` returned 404 "unavailable for free" on 2026-10-15). 20 RPM, 50 RPD (1K RPD once $10 credits bought).
- Observed 2026-10-15: both Gemini keys returned 429 together and stayed limited after ~20 calls (looks like a shared per-project daily cap; confirm in AI Studio). OpenRouter nemotron ~30-50 s per scoring call and reasons in the visible reply, so scoring uses max_tokens=4000 and parse_json takes the last JSON value.
- Groq budget: 200K TPD / ~4K tokens per CV+job call = ~50 LLM-scored jobs per day. Keyword prefilter before the LLM.

## Still open
- Search providers (Tavily/Exa/Brave) and email finders (Hunter/Snov/Prospeo): need API keys, none in env.
- DOL download from script: try with `Referer`/browser headers, else manual download step.
