// Package store is the Go side's Postgres access: job upserts from ingestion, run records
// from workflows, and the read models the gateway serves.
package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"

	"github.com/Mrwanawad/JobRadar/orchestrator/internal/ingest"
	"github.com/Mrwanawad/JobRadar/orchestrator/migrations"
)

var ErrNotFound = errors.New("not found")

type Store struct {
	Pool *pgxpool.Pool
}

func Open(ctx context.Context, dsn string) (*Store, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("postgres: %w", err)
	}
	return &Store{Pool: pool}, nil
}

func (s *Store) Close() { s.Pool.Close() }

// Migrate applies the embedded schema migrations.
func (s *Store) Migrate(ctx context.Context) error {
	db := stdlib.OpenDBFromPool(s.Pool)
	defer db.Close()
	goose.SetBaseFS(migrations.FS)
	if err := goose.SetDialect("postgres"); err != nil {
		return err
	}
	return goose.UpContext(ctx, db, ".")
}

const upsertJob = `
INSERT INTO jobs (uid, fingerprint, source, external_id, company, title, location, remote, url,
                  apply_url, description, posted_at, salary, tags, emails, phones)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
ON CONFLICT (uid) DO UPDATE SET last_seen = now()
RETURNING (xmax = 0)`

// UpsertJobs stores unseen jobs and refreshes last_seen on known ones. Returns how many
// were new. A stored job is never overwritten: profiles already have matches against it.
func (s *Store) UpsertJobs(ctx context.Context, jobs []ingest.Job) (int, error) {
	if len(jobs) == 0 {
		return 0, nil
	}
	b := &pgx.Batch{}
	seen := map[string]bool{}
	for _, j := range jobs {
		uid := j.UID()
		if seen[uid] { // a board can list one posting twice; one statement batch must not
			continue
		}
		seen[uid] = true
		b.Queue(upsertJob, uid, ingest.Fingerprint(j.Company, j.Title), j.Source, j.ExternalID,
			j.Company, j.Title, j.Location, j.Remote, j.URL, j.ApplyURL, j.Description,
			j.PostedAt, j.Salary, nz(j.Tags), nz(j.Emails), nz(j.Phones))
	}
	res := s.Pool.SendBatch(ctx, b)
	defer res.Close()
	inserted := 0
	for range b.Len() {
		var isNew bool
		if err := res.QueryRow().Scan(&isNew); err != nil {
			return inserted, err
		}
		if isNew {
			inserted++
		}
	}
	return inserted, res.Close()
}

// FetchedSince reports whether a source was fetched after t (the shared fetch cache).
func (s *Store) FetchedSince(ctx context.Context, key string, t time.Time) (bool, error) {
	var ok bool
	err := s.Pool.QueryRow(ctx,
		`SELECT EXISTS (SELECT 1 FROM source_fetches WHERE source_key=$1 AND fetched_at > $2)`,
		key, t).Scan(&ok)
	return ok, err
}

func (s *Store) RecordFetch(ctx context.Context, key string, fetched, inserted int) error {
	_, err := s.Pool.Exec(ctx, `
		INSERT INTO source_fetches (source_key, fetched_at, fetched, inserted) VALUES ($1, now(), $2, $3)
		ON CONFLICT (source_key) DO UPDATE SET fetched_at=now(), fetched=$2, inserted=$3`,
		key, fetched, inserted)
	return err
}

func nz(xs []string) []string {
	if xs == nil {
		return []string{}
	}
	return xs
}

// Run is one workflow execution as the UI lists it.
type Run struct {
	WorkflowID string          `json:"workflow_id"`
	ProfileID  string          `json:"profile_id"`
	Mode       string          `json:"mode"`
	Status     string          `json:"status"`
	Progress   json.RawMessage `json:"progress"`
	StartedAt  time.Time       `json:"started_at"`
	FinishedAt *time.Time      `json:"finished_at"`
}

const runCols = `workflow_id, profile_id, mode, status, progress, started_at, finished_at`

func scanRun(row pgx.Row) (Run, error) {
	var r Run
	err := row.Scan(&r.WorkflowID, &r.ProfileID, &r.Mode, &r.Status, &r.Progress, &r.StartedAt, &r.FinishedAt)
	return r, err
}

func (s *Store) StartRun(ctx context.Context, workflowID, profileID, mode string) error {
	_, err := s.Pool.Exec(ctx, `
		INSERT INTO runs (workflow_id, profile_id, mode) VALUES ($1,$2,$3)
		ON CONFLICT (workflow_id) DO NOTHING`, workflowID, profileID, mode)
	return err
}

func (s *Store) FinishRun(ctx context.Context, workflowID, status string, progress []byte) error {
	_, err := s.Pool.Exec(ctx, `
		UPDATE runs SET status=$2, progress=$3, finished_at=now() WHERE workflow_id=$1`,
		workflowID, status, progress)
	return err
}

func (s *Store) GetRun(ctx context.Context, workflowID string) (Run, error) {
	r, err := scanRun(s.Pool.QueryRow(ctx, `SELECT `+runCols+` FROM runs WHERE workflow_id=$1`, workflowID))
	if errors.Is(err, pgx.ErrNoRows) {
		return r, ErrNotFound
	}
	return r, err
}

func (s *Store) ListRuns(ctx context.Context, profileID string, limit int) ([]Run, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+runCols+` FROM runs WHERE profile_id=$1
		ORDER BY started_at DESC LIMIT $2`, profileID, limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Run, error) { return scanRun(row) })
}

// Profile is a stored searcher; Profile holds the profile.yaml shape as JSON.
type Profile struct {
	ID        string          `json:"id"`
	Kind      string          `json:"kind"`
	Owner     string          `json:"owner"`
	Profile   json.RawMessage `json:"profile"`
	CVText    string          `json:"-"`
	ExpiresAt *time.Time      `json:"expires_at"`
}

func (s *Store) UpsertProfile(ctx context.Context, p Profile) error {
	_, err := s.Pool.Exec(ctx, `
		INSERT INTO profiles (id, kind, owner, profile, cv_text, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6)
		ON CONFLICT (id) DO UPDATE SET profile=EXCLUDED.profile, cv_text=EXCLUDED.cv_text,
		                               expires_at=EXCLUDED.expires_at`,
		p.ID, p.Kind, p.Owner, p.Profile, p.CVText, p.ExpiresAt)
	return err
}

func (s *Store) GetProfile(ctx context.Context, id string) (Profile, error) {
	var p Profile
	err := s.Pool.QueryRow(ctx, `
		SELECT id, kind, owner, profile, cv_text, expires_at FROM profiles WHERE id=$1`, id).
		Scan(&p.ID, &p.Kind, &p.Owner, &p.Profile, &p.CVText, &p.ExpiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, ErrNotFound
	}
	return p, err
}

// ClaimDemo takes the visitor's single demo run. False means it was already used.
func (s *Store) ClaimDemo(ctx context.Context, owner string) (bool, error) {
	sum := sha256.Sum256([]byte(owner))
	tag, err := s.Pool.Exec(ctx, `INSERT INTO demo_quota (owner_hash) VALUES ($1) ON CONFLICT DO NOTHING`,
		hex.EncodeToString(sum[:]))
	return tag.RowsAffected() == 1, err
}

// PurgeExpired deletes demo profiles past expires_at; matches, outbox and runs cascade.
func (s *Store) PurgeExpired(ctx context.Context) (int64, error) {
	tag, err := s.Pool.Exec(ctx, `DELETE FROM profiles WHERE expires_at < now()`)
	return tag.RowsAffected(), err
}

// Result is one ranked job, and its draft if one was written, for the results view.
type Result struct {
	JobUID      string          `json:"job_uid"`
	Company     string          `json:"company"`
	Title       string          `json:"title"`
	Location    string          `json:"location"`
	URL         string          `json:"url"`
	Score       *float64        `json:"score"`
	Assessment  json.RawMessage `json:"assessment"`
	OutboxID    *int64          `json:"outbox_id"`
	Channel     *string         `json:"channel"`
	Subject     *string         `json:"subject"`
	Body        *string         `json:"body"`
	DraftStatus *string         `json:"draft_status"`
}

func (s *Store) Results(ctx context.Context, profileID string, limit int) ([]Result, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT j.uid, j.company, j.title, j.location, j.url, m.llm_score::float8,
		       COALESCE(m.llm_json, 'null'::jsonb), o.id, o.channel, o.subject, o.body, o.status
		FROM matches m
		JOIN jobs j ON j.uid = m.job_uid
		LEFT JOIN outbox o ON o.profile_id = m.profile_id AND o.job_uid = m.job_uid
		WHERE m.profile_id = $1 AND m.status = 'scored'
		ORDER BY m.llm_score DESC NULLS LAST
		LIMIT $2`, profileID, limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Result, error) {
		var r Result
		err := row.Scan(&r.JobUID, &r.Company, &r.Title, &r.Location, &r.URL, &r.Score,
			&r.Assessment, &r.OutboxID, &r.Channel, &r.Subject, &r.Body, &r.DraftStatus)
		return r, err
	})
}

// Counts summarizes the pipeline for the dashboard header.
func (s *Store) Counts(ctx context.Context, profileID string) (map[string]int, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT 'match_' || status, count(*) FROM matches WHERE profile_id=$1 GROUP BY status
		UNION ALL
		SELECT 'outbox_' || status, count(*) FROM outbox WHERE profile_id=$1 GROUP BY status
		UNION ALL
		SELECT 'jobs_total', count(*) FROM jobs`, profileID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var k string
		var n int
		if err := rows.Scan(&k, &n); err != nil {
			return nil, err
		}
		out[k] = n
	}
	return out, rows.Err()
}

// DecideDraft records a human decision on a draft (personal profiles only).
func (s *Store) DecideDraft(ctx context.Context, profileID string, outboxID int64, approve bool, body string) error {
	status := "rejected"
	if approve {
		status = "approved"
	}
	tag, err := s.Pool.Exec(ctx, `
		UPDATE outbox SET status=$3, decided_at=now(), body=COALESCE(NULLIF($4,''), body)
		WHERE id=$2 AND profile_id=$1 AND status='draft'`, profileID, outboxID, status, body)
	if err == nil && tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return err
}
