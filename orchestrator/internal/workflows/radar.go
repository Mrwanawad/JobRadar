// Package workflows holds the Temporal workflows. They are deterministic orchestration
// only: every side effect (HTTP, Postgres, Elasticsearch, LLM calls) is an activity, run
// either by the Go worker (IngestQueue) or by the Python agent worker (AgentQueue).
package workflows

import (
	"errors"
	"fmt"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
)

const (
	ProgressQuery  = "progress"
	ApprovalSignal = "approval"
	// ApprovalWindow is how long a personal run stays open for draft decisions.
	ApprovalWindow = 48 * time.Hour
)

// Defaults applied to zero-valued request limits.
var DefaultLimits = &pb.Limits{
	FetchConcurrency: 8,
	MatchTopK:        40,
	MaxScored:        25,
	MaxDrafts:        8,
	MinDraftScore:    7.5,
}

// QuotaExhausted is the error type the Python worker raises when every LLM provider is
// out of free quota. Scoring stops early instead of failing the run.
const QuotaExhausted = "QuotaExhausted"

// RadarWorkflow is one end-to-end run for one profile:
//
//	fetch (fan-out, Go) -> prefilter -> index -> hybrid match -> score -> draft (Python)
//	-> [personal only] wait for human approval signals
func RadarWorkflow(ctx workflow.Context, req *pb.RadarRequest) (*pb.RadarResult, error) {
	lim := withDefaults(req.GetLimits())
	info := workflow.GetInfo(ctx)
	log := workflow.GetLogger(ctx)
	p := &pb.RadarProgress{Stage: pb.Stage_STAGE_FETCHING, SourcesTotal: int32(len(req.GetSources()))}
	touch := func() { p.UpdatedAt = timestamppb.New(workflow.Now(ctx)) }
	touch()

	if err := workflow.SetQueryHandler(ctx, ProgressQuery, func() (*pb.RadarProgress, error) {
		return p, nil
	}); err != nil {
		return nil, err
	}

	book := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           IngestQueue,
		StartToCloseTimeout: 30 * time.Second,
	})
	rec := RunRecord{WorkflowID: info.WorkflowExecution.ID, ProfileID: req.GetProfileId(), Mode: req.GetMode().String()}
	var a *Activities
	if err := workflow.ExecuteActivity(book, a.RecordRunStart, rec).Get(ctx, nil); err != nil {
		return nil, err
	}

	result, runErr := runPipeline(ctx, req, lim, p, touch)
	if runErr == nil && req.GetMode() == pb.RunMode_RUN_MODE_PERSONAL && p.Drafted > 0 {
		p.Stage = pb.Stage_STAGE_AWAITING_APPROVAL
		touch()
		awaitApprovals(ctx, req.GetProfileId(), int(p.Drafted), book)
	}

	// Bookkeeping must run even if the pipeline failed or the workflow was cancelled.
	dctx, cancel := workflow.NewDisconnectedContext(book)
	defer cancel()
	rec.Status = "completed"
	if runErr != nil {
		rec.Status = "failed"
		p.Errors = append(p.Errors, runErr.Error())
	}
	p.Stage = pb.Stage_STAGE_DONE
	touch()
	rec.Progress, _ = protojson.Marshal(p)
	if err := workflow.ExecuteActivity(dctx, a.RecordRunFinish, rec).Get(dctx, nil); err != nil {
		log.Error("record run finish", "error", err)
	}
	if runErr != nil {
		return nil, runErr
	}
	result.Progress = p
	return result, nil
}

func runPipeline(ctx workflow.Context, req *pb.RadarRequest, lim *pb.Limits, p *pb.RadarProgress, touch func()) (*pb.RadarResult, error) {
	profileID := req.GetProfileId()

	// 1. Fetch every source in parallel, at most FetchConcurrency at a time. One broken
	// board is recorded and skipped; it never fails the run.
	fetch := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           IngestQueue,
		StartToCloseTimeout: 3 * time.Minute,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    5 * time.Second,
			BackoffCoefficient: 3,
			MaximumAttempts:    3,
		},
	})
	var a *Activities
	sel := workflow.NewSelector(ctx)
	inFlight := 0
	collect := func(f workflow.Future) {
		inFlight--
		p.SourcesDone++
		var r pb.FetchSourceResult
		if err := f.Get(ctx, &r); err != nil {
			p.Errors = append(p.Errors, shortErr(err))
		}
		p.Fetched += r.Fetched
		p.Inserted += r.Inserted
		touch()
	}
	for _, src := range req.GetSources() {
		for inFlight >= int(lim.FetchConcurrency) {
			sel.Select(ctx)
		}
		f := workflow.ExecuteActivity(fetch, a.FetchSource, &pb.FetchSourceInput{Source: src})
		inFlight++
		sel.AddFuture(f, collect)
	}
	for inFlight > 0 {
		sel.Select(ctx)
	}

	agent := func(timeout time.Duration, attempts int32) workflow.Context {
		return workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
			TaskQueue:           AgentQueue,
			StartToCloseTimeout: timeout,
			HeartbeatTimeout:    2 * time.Minute,
			RetryPolicy: &temporal.RetryPolicy{
				InitialInterval:        10 * time.Second,
				BackoffCoefficient:     2,
				MaximumAttempts:        attempts,
				NonRetryableErrorTypes: []string{QuotaExhausted},
			},
		})
	}

	// 2. Free rules pass for this profile.
	p.Stage = pb.Stage_STAGE_PREFILTERING
	touch()
	var pf pb.PrefilterResult
	if err := workflow.ExecuteActivity(agent(10*time.Minute, 3), ActPrefilter,
		&pb.PrefilterInput{ProfileId: profileID}).Get(ctx, &pf); err != nil {
		return nil, fmt.Errorf("prefilter: %w", err)
	}
	p.Candidates = pf.Candidates

	// 3. Embed and index new jobs, in batches, until nothing is left.
	p.Stage = pb.Stage_STAGE_INDEXING
	touch()
	const batch = 256
	for range 40 {
		var ix pb.IndexJobsResult
		if err := workflow.ExecuteActivity(agent(15*time.Minute, 5), ActIndexJobs,
			&pb.IndexJobsInput{BatchSize: batch}).Get(ctx, &ix); err != nil {
			return nil, fmt.Errorf("index: %w", err)
		}
		p.Indexed += ix.Indexed
		touch()
		if ix.Indexed < batch {
			break
		}
	}

	// 4. Hybrid search: which candidates look most like this CV.
	p.Stage = pb.Stage_STAGE_MATCHING
	touch()
	var m pb.MatchJobsResult
	if err := workflow.ExecuteActivity(agent(5*time.Minute, 3), ActMatchJobs,
		&pb.MatchJobsInput{ProfileId: profileID, TopK: lim.MatchTopK}).Get(ctx, &m); err != nil {
		return nil, fmt.Errorf("match: %w", err)
	}
	p.Matched = int32(len(m.JobUids))

	// 5. LLM screening, best matches first, a few at a time (free-tier rate limits).
	p.Stage = pb.Stage_STAGE_SCORING
	touch()
	uids := m.JobUids
	if len(uids) > int(lim.MaxScored) {
		uids = uids[:lim.MaxScored]
	}
	scores := map[string]float32{}
	quotaOut := false
	runBounded(ctx, uids, 3, func(uid string) workflow.Future {
		return workflow.ExecuteActivity(agent(10*time.Minute, 4), ActScoreJob,
			&pb.ScoreJobInput{ProfileId: profileID, JobUid: uid})
	}, func(uid string, f workflow.Future) bool {
		var r pb.ScoreJobResult
		if err := f.Get(ctx, &r); err != nil {
			if isType(err, QuotaExhausted) {
				quotaOut = true
				p.Errors = append(p.Errors, "LLM free quota exhausted; scoring stopped early")
				return false
			}
			p.Errors = append(p.Errors, "score "+uid+": "+shortErr(err))
		} else {
			scores[uid] = r.Score
			p.Scored++
		}
		touch()
		return true
	})

	// 6. Draft applications for the best scores (critique-and-revise inside the agent).
	p.Stage = pb.Stage_STAGE_DRAFTING
	touch()
	toDraft := []string{}
	for _, uid := range uids { // uids keep match order; filter by score
		if s, ok := scores[uid]; ok && s >= lim.MinDraftScore && len(toDraft) < int(lim.MaxDrafts) {
			toDraft = append(toDraft, uid)
		}
	}
	if !quotaOut {
		runBounded(ctx, toDraft, 2, func(uid string) workflow.Future {
			return workflow.ExecuteActivity(agent(10*time.Minute, 3), ActDraft,
				&pb.DraftApplicationInput{ProfileId: profileID, JobUid: uid})
		}, func(uid string, f workflow.Future) bool {
			var r pb.DraftApplicationResult
			if err := f.Get(ctx, &r); err != nil {
				p.Errors = append(p.Errors, "draft "+uid+": "+shortErr(err))
				return !isType(err, QuotaExhausted)
			}
			p.Drafted++
			touch()
			return true
		})
	}

	var top pb.TopJobsResult
	if err := workflow.ExecuteActivity(agent(time.Minute, 3), ActTopJobs,
		&pb.TopJobsInput{ProfileId: profileID, Limit: 20}).Get(ctx, &top); err != nil {
		return nil, fmt.Errorf("top jobs: %w", err)
	}
	return &pb.RadarResult{Top: top.Jobs}, nil
}

// awaitApprovals applies `approval` signals until every draft is decided or the window
// closes. Undecided drafts simply stay drafts.
func awaitApprovals(ctx workflow.Context, profileID string, drafts int, book workflow.Context) {
	ch := workflow.GetSignalChannel(ctx, ApprovalSignal)
	timer := workflow.NewTimer(ctx, ApprovalWindow)
	var a *Activities
	decided, open := 0, true
	for decided < drafts && open {
		sel := workflow.NewSelector(ctx)
		sel.AddReceive(ch, func(c workflow.ReceiveChannel, _ bool) {
			var s pb.ApprovalSignal
			c.Receive(ctx, &s)
			if err := workflow.ExecuteActivity(book, a.RecordDecision, profileID, &s).Get(ctx, nil); err == nil {
				decided++
			}
		})
		sel.AddFuture(timer, func(workflow.Future) { open = false })
		sel.Select(ctx)
	}
}

// runBounded starts start(item) for each item with at most n in flight, calling done as
// each finishes. done returning false stops launching new items.
func runBounded(ctx workflow.Context, items []string, n int, start func(string) workflow.Future, done func(string, workflow.Future) bool) {
	sel := workflow.NewSelector(ctx)
	inFlight, keepGoing := 0, true
	for _, it := range items {
		for inFlight >= n {
			sel.Select(ctx)
		}
		if !keepGoing {
			break
		}
		f := start(it)
		inFlight++
		sel.AddFuture(f, func(f workflow.Future) {
			inFlight--
			if !done(it, f) {
				keepGoing = false
			}
		})
	}
	for inFlight > 0 {
		sel.Select(ctx)
	}
}

func withDefaults(l *pb.Limits) *pb.Limits {
	out := &pb.Limits{
		FetchConcurrency: l.GetFetchConcurrency(),
		MatchTopK:        l.GetMatchTopK(),
		MaxScored:        l.GetMaxScored(),
		MaxDrafts:        l.GetMaxDrafts(),
		MinDraftScore:    l.GetMinDraftScore(),
	}
	if out.FetchConcurrency <= 0 {
		out.FetchConcurrency = DefaultLimits.FetchConcurrency
	}
	if out.MatchTopK <= 0 {
		out.MatchTopK = DefaultLimits.MatchTopK
	}
	if out.MaxScored <= 0 {
		out.MaxScored = DefaultLimits.MaxScored
	}
	if out.MaxDrafts < 0 {
		out.MaxDrafts = 0
	} else if out.MaxDrafts == 0 {
		out.MaxDrafts = DefaultLimits.MaxDrafts
	}
	if out.MinDraftScore <= 0 {
		out.MinDraftScore = DefaultLimits.MinDraftScore
	}
	return out
}

func isType(err error, typ string) bool {
	var appErr *temporal.ApplicationError
	return errors.As(err, &appErr) && appErr.Type() == typ
}

// shortErr keeps the innermost cause: Temporal wraps activity errors several layers deep.
func shortErr(err error) string {
	for {
		next := errors.Unwrap(err)
		if next == nil {
			break
		}
		err = next
	}
	s := err.Error()
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
