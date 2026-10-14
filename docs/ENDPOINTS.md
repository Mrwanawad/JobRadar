# Verified endpoints (2026-10-14)

Fixtures live in `tests/fixtures/`, refreshed with `uv run python scripts/capture_fixtures.py`.

| Source | URL | Status | Notes |
|---|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | 200 | top keys `jobs`, `meta`; `updated_at` = edit time, not post time |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` | 200 | list; `createdAt` ms epoch, `workplaceType`, `country`, `hostedUrl`, `descriptionPlain` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | 200 | `publishedAt`, `isRemote`, `workplaceType`, `jobUrl`, `descriptionPlain`, `compensation` |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/{slug}/postings` | 200 | `offset/limit/totalFound/content[]`; `releasedDate`; description needs per-posting call |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/{slug}` | 200 | widget JSON exists: `published_on`, `telecommuting`, `country`, `city`, `url`; no description |
| HN Who is hiring | `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring` | 200 | then `/items/{id}` or `search?tags=comment,story_{id}` for comments |
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

## Claude API (from official reference)
- Model: `claude-haiku-5-5`, $0.10 in / $0.50 out per MTok (prompts <=100K).
- Default effort `medium`; thinking adaptive/on by default; non-default sampling params 400. Set `output_config={"effort": "low"}` for scoring.
- Cache: `cache_control={"type": "ephemeral"}` top-level or on the system block; check `usage.cache_read_input_tokens`. Minimum cacheable prefix is model-dependent; a short CV may fall under it.

## Still open
- Search providers (Tavily/Exa/Brave) and email finders (Hunter/Snov/Prospeo): need API keys, none in env.
- DOL download from script: try with `Referer`/browser headers, else manual download step.
