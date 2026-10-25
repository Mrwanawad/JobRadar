# JobRadar

Daily job radar. Finds, filters and LLM-scores jobs against your CV.
Later milestones draft and send applications (Gmail connector; WhatsApp only when a phone
number is the sole contact), with daily caps and a dry-run period first.

Status: milestone 3 (fetch, prefilter, score, draft, send with caps + dry run). Sources and limits: `docs/ENDPOINTS.md`.

## Setup

```
uv sync
copy .env.example .env               # fill GEMINI_API_KEY_1/_2 and OPENROUTER_API_KEY
copy profile.example.yaml profile.yaml  # your details; gitignored
uv run python scripts/llm_smoke.py
```

Put your CV PDF in `cv/` and point `profile.yaml -> candidate.cv` at it.

## Use

```
uv run jobradar run                 # fetch + prefilter + score + show shortlist
uv run jobradar fetch               # pull all boards/feeds, prefilter new jobs
uv run jobradar score --limit 20    # LLM-score best keyword matches first
uv run jobradar top --min 7 --xlsx out/shortlist.xlsx
uv run jobradar refilter            # re-apply profile.yaml rules to unscored jobs
uv run jobradar draft               # draft applications for jobs >= outreach.min_score
uv run jobradar outbox --full       # review drafts; `--channel manual` = paste into apply forms
uv run jobradar sendable            # what the sender may deliver now (JSON)
uv run jobradar mark 12 skipped     # drop a draft you don't want sent
claude -p "/send-outreach" --permission-mode dontAsk   # send it (Gmail / WhatsApp MCP)
```

Daily schedule (Windows Task Scheduler, 09:00, logs in `out/logs/`):

```
powershell -ExecutionPolicy Bypass -File scripts/install_schedule.ps1
```

## How it works

1. **Fetch**: ~47 company boards (`companies.yaml`: Greenhouse, Lever, Ashby, Workable,
   SmartRecruiters) plus RemoteOK, We Work Remotely, Himalayas and HN "Who is hiring".
2. **Prefilter** (free): title, location and age rules from `profile.yaml`. Remote roles
   locked to another country are dropped; on-site abroad is kept only if the posting
   mentions visa sponsorship or relocation. Cross-posted duplicates collapse.
3. **Score** (LLM): CV + job -> 0-10 with reasons, red flags, location fit and a pitch angle.
   Ineligible locations are capped at 3. Highest keyword matches are scored first, so the
   daily free quota goes to the likeliest fits.

LLM chain: Gemini key 1 -> Gemini key 2 -> OpenRouter. A bad key or rate limit moves on to
the next; a run that exhausts every provider stops and resumes next run.
4. **Draft** (LLM): one application per job at or above `outreach.min_score`. Email if the
   posting lists a hiring address, WhatsApp only if a phone number is the sole contact,
   otherwise a cover letter to paste into the apply form (also in `out/shortlist.xlsx`).
5. **Send** (Claude Code, local): `/send-outreach` sends exactly what `jobradar sendable`
   returns. Nothing is sendable before `outreach.dry_run_until`, beyond `daily_cap` per day,
   twice to one recipient, or (email) without `candidate.cv_link`.
