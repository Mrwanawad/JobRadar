package workflows

import (
	"context"
	"errors"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/ingest"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/store"
)

// Task queues. Go owns orchestration and ingestion; Python owns everything that thinks.
const (
	IngestQueue = "jobradar-ingest"
	AgentQueue  = "jobradar-agent"
)

// Names of the activities the Python agent worker registers (jobradar/worker).
const (
	ActPrefilter = "Prefilter"
	ActIndexJobs = "IndexJobs"
	ActMatchJobs = "MatchJobs"
	ActScoreJob  = "ScoreJob"
	ActDraft     = "DraftApplication"
	ActTopJobs   = "TopJobs"
)

// FetchCacheTTL: a source fetched more recently than this is served from Postgres.
const FetchCacheTTL = 6 * time.Hour

// Activities are the Go-side activities, run on IngestQueue.
type Activities struct {
	Store   *store.Store
	Fetcher *ingest.Fetcher
}

// FetchSource downloads one board or feed and upserts its jobs. Jobs go straight to
// Postgres; only counts travel back through Temporal (payloads stay tiny).
func (a *Activities) FetchSource(ctx context.Context, in *pb.FetchSourceInput) (*pb.FetchSourceResult, error) {
	src := in.GetSource()
	key := src.GetKind() + ":" + src.GetSlug()
	res := &pb.FetchSourceResult{Source: key}

	fresh, err := a.Store.FetchedSince(ctx, key, time.Now().Add(-FetchCacheTTL))
	if err != nil {
		return nil, err
	}
	if fresh {
		activity.GetLogger(ctx).Info("fetch cache hit", "source", key)
		return res, nil
	}

	jobs, err := a.Fetcher.Fetch(ctx, src.GetKind(), src.GetSlug(), src.GetName())
	if err != nil {
		var se *ingest.StatusError
		if errors.As(err, &se) && se.Permanent() {
			return nil, temporal.NewNonRetryableApplicationError(err.Error(), "SourceGone", err)
		}
		return nil, err
	}
	inserted, err := a.Store.UpsertJobs(ctx, jobs)
	if err != nil {
		return nil, err
	}
	if err := a.Store.RecordFetch(ctx, key, len(jobs), inserted); err != nil {
		return nil, err
	}
	res.Fetched, res.Inserted = int32(len(jobs)), int32(inserted)
	return res, nil
}

// RunRecord is the input of the run bookkeeping activities.
type RunRecord struct {
	WorkflowID string
	ProfileID  string
	Mode       string
	Status     string
	Progress   []byte // protojson of pb.RadarProgress
}

func (a *Activities) RecordRunStart(ctx context.Context, r RunRecord) error {
	return a.Store.StartRun(ctx, r.WorkflowID, r.ProfileID, r.Mode)
}

func (a *Activities) RecordRunFinish(ctx context.Context, r RunRecord) error {
	return a.Store.FinishRun(ctx, r.WorkflowID, r.Status, r.Progress)
}

// RecordDecision applies an approval signal to the outbox. An unknown or already decided
// draft is not an error worth retrying.
func (a *Activities) RecordDecision(ctx context.Context, profileID string, s *pb.ApprovalSignal) error {
	err := a.Store.DecideDraft(ctx, profileID, s.GetOutboxId(), s.GetApprove(), s.GetEditedBody())
	if errors.Is(err, store.ErrNotFound) {
		return nil
	}
	return err
}

// PurgeExpired deletes demo profiles (and their CV text, matches and drafts) past expiry.
func (a *Activities) PurgeExpired(ctx context.Context) (int64, error) {
	return a.Store.PurgeExpired(ctx)
}
