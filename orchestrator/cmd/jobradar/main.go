// Command jobradar is the Go side of JobRadar: one binary, four roles.
//
//	jobradar migrate             apply the Postgres schema
//	jobradar worker              run workflows + ingestion activities (task queue jobradar-ingest)
//	jobradar gateway             serve the HTTP API used by the web app
//	jobradar schedule <profile>  create/update the Temporal Schedules (daily radar, hourly purge)
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	tlog "go.temporal.io/sdk/log"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/config"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/gateway"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/ingest"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/store"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/telemetry"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/workflows"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(log)
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: jobradar migrate|worker|gateway|schedule <profile-id>")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1], os.Args[2:], log); err != nil && !errors.Is(err, context.Canceled) {
		log.Error("fatal", "cmd", os.Args[1], "error", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, cmd string, args []string, log *slog.Logger) error {
	cfg := config.Load()
	shutdown, err := telemetry.Setup(ctx, "jobradar-"+cmd)
	if err != nil {
		return err
	}
	defer func() {
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = shutdown(c)
	}()

	st, err := store.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Close()
	if cmd == "migrate" {
		return st.Migrate(ctx)
	}
	if cmd == "worker" { // the worker owns the schema, so a fresh stack needs no extra step
		if err := st.Migrate(ctx); err != nil {
			return err
		}
	}

	tc, err := dialTemporal(ctx, cfg, log)
	if err != nil {
		return err
	}
	defer tc.Close()

	switch cmd {
	case "worker":
		w := worker.New(tc, workflows.IngestQueue, worker.Options{})
		w.RegisterWorkflow(workflows.RadarWorkflow)
		w.RegisterWorkflow(workflows.PurgeWorkflow)
		w.RegisterActivity(&workflows.Activities{Store: st, Fetcher: ingest.NewFetcher()})
		log.Info("worker started", "queue", workflows.IngestQueue)
		return w.Run(worker.InterruptCh())

	case "gateway":
		srv := &gateway.Server{
			Store:    st,
			Temporal: tc,
			Sources:  func() ([]*pb.Source, error) { return config.LoadSources(cfg.CompaniesFile) },
			Token:    os.Getenv("GATEWAY_TOKEN"),
			Log:      log,
		}
		if len(srv.Token) < 16 {
			return errors.New("GATEWAY_TOKEN must be set (16+ characters)")
		}
		hs := &http.Server{
			Addr:              config.Env("GATEWAY_ADDR", ":8080"),
			Handler:           srv.Handler(),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			<-ctx.Done()
			c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = hs.Shutdown(c)
		}()
		log.Info("gateway listening", "addr", hs.Addr)
		if err := hs.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil

	case "schedule":
		if len(args) != 1 {
			return errors.New("usage: jobradar schedule <personal-profile-id>")
		}
		return ensureSchedules(ctx, tc, cfg, args[0], log)
	}
	return fmt.Errorf("unknown command %q", cmd)
}

func dialTemporal(ctx context.Context, cfg config.Settings, log *slog.Logger) (client.Client, error) {
	opts, err := telemetry.TemporalOptions(client.Options{
		HostPort:  cfg.TemporalAddress,
		Namespace: cfg.TemporalNamespace,
		Logger:    tlog.NewStructuredLogger(log),
	})
	if err != nil {
		return nil, err
	}
	// Temporal may still be starting when the stack comes up; retry for a minute.
	var tc client.Client
	for i := range 30 {
		if tc, err = client.DialContext(ctx, opts); err == nil {
			return tc, nil
		}
		log.Warn("temporal not ready", "attempt", i+1, "error", err)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return nil, err
}

// ensureSchedules creates (or replaces) the two Temporal Schedules: the owner's daily radar
// at 09:00 Cairo time, and the hourly demo-data purge.
func ensureSchedules(ctx context.Context, tc client.Client, cfg config.Settings, profileID string, log *slog.Logger) error {
	sources, err := config.LoadSources(cfg.CompaniesFile)
	if err != nil {
		return err
	}
	specs := []client.ScheduleOptions{
		{
			ID: "daily-radar-" + profileID,
			Spec: client.ScheduleSpec{
				CronExpressions: []string{"0 9 * * *"},
				TimeZoneName:    "Africa/Cairo",
				Jitter:          5 * time.Minute,
			},
			Overlap: enumspb.SCHEDULE_OVERLAP_POLICY_SKIP,
			Action: &client.ScheduleWorkflowAction{
				ID:        "radar-" + profileID + "-scheduled",
				Workflow:  workflows.RadarWorkflow,
				Args:      []any{&pb.RadarRequest{ProfileId: profileID, Mode: pb.RunMode_RUN_MODE_PERSONAL, Sources: sources}},
				TaskQueue: workflows.IngestQueue,
			},
		},
		{
			ID:      "purge-demo-data",
			Spec:    client.ScheduleSpec{Intervals: []client.ScheduleIntervalSpec{{Every: time.Hour}}},
			Overlap: enumspb.SCHEDULE_OVERLAP_POLICY_SKIP,
			Action: &client.ScheduleWorkflowAction{
				ID:        "purge-demo-data",
				Workflow:  workflows.PurgeWorkflow,
				TaskQueue: workflows.IngestQueue,
				RetryPolicy: &temporal.RetryPolicy{
					MaximumAttempts: 3,
				},
			},
		},
	}
	for _, s := range specs {
		h := tc.ScheduleClient().GetHandle(ctx, s.ID)
		if _, err := h.Describe(ctx); err == nil {
			if err := h.Delete(ctx); err != nil {
				return err
			}
		}
		if _, err := tc.ScheduleClient().Create(ctx, s); err != nil {
			return fmt.Errorf("schedule %s: %w", s.ID, err)
		}
		log.Info("schedule ready", "id", s.ID)
	}
	return nil
}
