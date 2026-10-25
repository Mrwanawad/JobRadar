package workflows

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
)

// fakeAgent stands in for the Python worker; activities are registered by name, exactly
// as the real cross-language calls resolve them.
type fakeAgent struct {
	scores   map[string]float32
	quotaAt  int32 // ScoreJob call number that reports quota exhaustion (0 = never)
	calls    atomic.Int32
	drafted  atomic.Int32
	decided  atomic.Int32
	finished atomic.Value
}

func (f *fakeAgent) register(env *testsuite.TestWorkflowEnvironment) {
	reg := func(name string, fn any) { env.RegisterActivityWithOptions(fn, activity.RegisterOptions{Name: name}) }
	env.RegisterActivityWithOptions(func(_ context.Context, in *pb.FetchSourceInput) (*pb.FetchSourceResult, error) {
		if in.Source.Slug == "broken" {
			return nil, temporal.NewNonRetryableApplicationError("HTTP 404", "SourceGone", nil)
		}
		return &pb.FetchSourceResult{Source: in.Source.Slug, Fetched: 10, Inserted: 4}, nil
	}, activity.RegisterOptions{Name: "FetchSource"})
	env.RegisterActivityWithOptions(func(context.Context, RunRecord) error { return nil },
		activity.RegisterOptions{Name: "RecordRunStart"})
	env.RegisterActivityWithOptions(func(_ context.Context, r RunRecord) error { f.finished.Store(r.Status); return nil },
		activity.RegisterOptions{Name: "RecordRunFinish"})
	env.RegisterActivityWithOptions(func(context.Context, string, *pb.ApprovalSignal) error { f.decided.Add(1); return nil },
		activity.RegisterOptions{Name: "RecordDecision"})

	reg(ActPrefilter, func(context.Context, *pb.PrefilterInput) (*pb.PrefilterResult, error) {
		return &pb.PrefilterResult{Candidates: 12}, nil
	})
	reg(ActIndexJobs, func(context.Context, *pb.IndexJobsInput) (*pb.IndexJobsResult, error) {
		return &pb.IndexJobsResult{Indexed: 7}, nil
	})
	reg(ActMatchJobs, func(context.Context, *pb.MatchJobsInput) (*pb.MatchJobsResult, error) {
		return &pb.MatchJobsResult{JobUids: []string{"a", "b", "c", "d"}}, nil
	})
	reg(ActScoreJob, func(_ context.Context, in *pb.ScoreJobInput) (*pb.ScoreJobResult, error) {
		if n := f.calls.Add(1); f.quotaAt > 0 && n >= f.quotaAt {
			return nil, temporal.NewNonRetryableApplicationError("all providers exhausted", QuotaExhausted, nil)
		}
		return &pb.ScoreJobResult{JobUid: in.JobUid, Score: f.scores[in.JobUid]}, nil
	})
	reg(ActDraft, func(_ context.Context, in *pb.DraftApplicationInput) (*pb.DraftApplicationResult, error) {
		f.drafted.Add(1)
		return &pb.DraftApplicationResult{OutboxId: 1, Channel: "manual"}, nil
	})
	reg(ActTopJobs, func(context.Context, *pb.TopJobsInput) (*pb.TopJobsResult, error) {
		return &pb.TopJobsResult{Jobs: []*pb.ScoredJob{{JobUid: "a", Score: 9}}}, nil
	})
}

func request(mode pb.RunMode) *pb.RadarRequest {
	return &pb.RadarRequest{
		ProfileId: "p1",
		Mode:      mode,
		Sources: []*pb.Source{
			{Kind: "greenhouse", Slug: "one"}, {Kind: "lever", Slug: "two"},
			{Kind: "ashby", Slug: "broken"}, {Kind: "feed", Slug: "hn"},
		},
		Limits: &pb.Limits{FetchConcurrency: 2},
	}
}

func TestDemoRunEndToEnd(t *testing.T) {
	var s testsuite.WorkflowTestSuite
	env := s.NewTestWorkflowEnvironment()
	f := &fakeAgent{scores: map[string]float32{"a": 9, "b": 8, "c": 4, "d": 7.6}}
	f.register(env)

	env.ExecuteWorkflow(RadarWorkflow, request(pb.RunMode_RUN_MODE_DEMO))
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}
	var res pb.RadarResult
	if err := env.GetWorkflowResult(&res); err != nil {
		t.Fatal(err)
	}
	p := res.Progress
	if p.SourcesDone != 4 || p.Fetched != 30 || p.Inserted != 12 {
		t.Errorf("fetch totals: %+v", p)
	}
	if len(p.Errors) != 1 { // the broken board is recorded, not fatal
		t.Errorf("errors: %v", p.Errors)
	}
	if p.Scored != 4 || f.drafted.Load() != 3 { // a, b, d >= 7.5
		t.Errorf("scored %d drafted %d", p.Scored, f.drafted.Load())
	}
	if p.Stage != pb.Stage_STAGE_DONE || f.finished.Load() != "completed" {
		t.Errorf("stage %v finished %v", p.Stage, f.finished.Load())
	}
}

func TestQuotaExhaustionStopsScoringNotTheRun(t *testing.T) {
	var s testsuite.WorkflowTestSuite
	env := s.NewTestWorkflowEnvironment()
	f := &fakeAgent{scores: map[string]float32{"a": 9, "b": 9, "c": 9, "d": 9}, quotaAt: 2}
	f.register(env)

	env.ExecuteWorkflow(RadarWorkflow, request(pb.RunMode_RUN_MODE_DEMO))
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}
	var res pb.RadarResult
	_ = env.GetWorkflowResult(&res)
	if res.Progress.Scored >= 4 {
		t.Errorf("scoring should stop early, scored %d", res.Progress.Scored)
	}
	if f.drafted.Load() != 0 {
		t.Errorf("no drafts once quota is gone, got %d", f.drafted.Load())
	}
}

func TestPersonalRunWaitsForApprovals(t *testing.T) {
	var s testsuite.WorkflowTestSuite
	env := s.NewTestWorkflowEnvironment()
	f := &fakeAgent{scores: map[string]float32{"a": 9, "b": 2, "c": 2, "d": 2}}
	f.register(env)

	env.RegisterDelayedCallback(func() {
		v, err := env.QueryWorkflow(ProgressQuery)
		if err != nil {
			t.Error(err)
			return
		}
		var p pb.RadarProgress
		_ = v.Get(&p)
		if p.Stage != pb.Stage_STAGE_AWAITING_APPROVAL {
			t.Errorf("stage %v, want awaiting approval", p.Stage)
		}
		env.SignalWorkflow(ApprovalSignal, &pb.ApprovalSignal{OutboxId: 1, Approve: true})
	}, time.Hour)

	env.ExecuteWorkflow(RadarWorkflow, request(pb.RunMode_RUN_MODE_PERSONAL))
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}
	if f.decided.Load() != 1 {
		t.Errorf("decisions %d", f.decided.Load())
	}
}

func TestApprovalWindowCloses(t *testing.T) {
	var s testsuite.WorkflowTestSuite
	env := s.NewTestWorkflowEnvironment()
	f := &fakeAgent{scores: map[string]float32{"a": 9, "b": 9, "c": 2, "d": 2}}
	f.register(env)
	env.ExecuteWorkflow(RadarWorkflow, request(pb.RunMode_RUN_MODE_PERSONAL))
	if err := env.GetWorkflowError(); err != nil {
		t.Fatal(err)
	}
	if f.finished.Load() != "completed" || f.decided.Load() != 0 {
		t.Errorf("finished %v decided %d", f.finished.Load(), f.decided.Load())
	}
}
