-- +goose Up
-- Jobs are global: one row per posting, shared by every profile, so a board fetched for one
-- run is never fetched-and-stored twice. Everything profile-specific lives in `matches`.
CREATE TABLE jobs (
    uid          TEXT PRIMARY KEY,               -- sha1(source:external_id)[:16]
    fingerprint  TEXT NOT NULL,                  -- normalized company|title, for cross-posts
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    remote       BOOLEAN,
    url          TEXT NOT NULL,
    apply_url    TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    posted_at    TIMESTAMPTZ,
    salary       TEXT NOT NULL DEFAULT '',
    tags         TEXT[] NOT NULL DEFAULT '{}',
    emails       TEXT[] NOT NULL DEFAULT '{}',
    phones       TEXT[] NOT NULL DEFAULT '{}',
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at   TIMESTAMPTZ                     -- set once embedded into Elasticsearch
);
CREATE INDEX jobs_fingerprint ON jobs (fingerprint);
CREATE INDEX jobs_unindexed ON jobs (first_seen) WHERE indexed_at IS NULL;

-- A searcher. kind = personal (the owner) or demo (a public visitor; expires).
CREATE TABLE profiles (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('personal', 'demo')),
    owner       TEXT NOT NULL,                   -- email or OAuth subject
    profile     JSONB NOT NULL,                  -- same shape as profile.yaml
    cv_text     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ                      -- demo data is purged after this
);

CREATE TABLE runs (
    workflow_id  TEXT PRIMARY KEY,
    profile_id   TEXT NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | completed | failed
    progress     JSONB NOT NULL DEFAULT '{}',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX runs_profile ON runs (profile_id, started_at DESC);

-- One profile's view of one job: prefilter verdict, then LLM score.
CREATE TABLE matches (
    profile_id     TEXT NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
    job_uid        TEXT NOT NULL REFERENCES jobs (uid) ON DELETE CASCADE,
    status         TEXT NOT NULL,                -- filtered | candidate | duplicate | scored
    filter_reason  TEXT NOT NULL DEFAULT '',
    kw_score       INTEGER NOT NULL DEFAULT 0,
    match_score    REAL,                         -- hybrid search rank score
    llm_score      REAL,
    llm_json       JSONB,
    scored_by      TEXT,
    scored_at      TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, job_uid)
);
CREATE INDEX matches_status ON matches (profile_id, status);
CREATE INDEX matches_score ON matches (profile_id, llm_score DESC NULLS LAST);

-- Drafted applications. status: draft -> approved -> sent | failed; or rejected | skipped.
-- Demo profiles only ever reach draft.
CREATE TABLE outbox (
    id              BIGSERIAL PRIMARY KEY,
    profile_id      TEXT NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
    job_uid         TEXT NOT NULL REFERENCES jobs (uid) ON DELETE CASCADE,
    channel         TEXT NOT NULL,               -- email | whatsapp | manual
    recipient       TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    critique        JSONB,                       -- the critic's last verdict
    revisions       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    ref             TEXT NOT NULL DEFAULT '',
    UNIQUE (profile_id, job_uid)
);
CREATE INDEX outbox_status ON outbox (profile_id, status);

-- Last fetch per source. Demo runs share these, so a board fetched in the last few hours
-- is not fetched again for every visitor.
CREATE TABLE source_fetches (
    source_key  TEXT PRIMARY KEY,                -- kind:slug
    fetched_at  TIMESTAMPTZ NOT NULL,
    fetched     INTEGER NOT NULL,
    inserted    INTEGER NOT NULL
);

-- One demo run per visitor, ever. Keyed by sha256 of the OAuth identity, so the quota
-- survives the 24h purge of the visitor's profile without keeping who they were.
CREATE TABLE demo_quota (
    owner_hash  TEXT PRIMARY KEY,
    used_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE demo_quota;
DROP TABLE source_fetches;
DROP TABLE outbox;
DROP TABLE matches;
DROP TABLE runs;
DROP TABLE profiles;
DROP TABLE jobs;
