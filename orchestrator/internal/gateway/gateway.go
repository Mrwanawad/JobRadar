// Package gateway is the HTTP API in front of Temporal and Postgres. The Cloudflare edge
// (apps/web) calls it through a Cloudflare Tunnel with a shared bearer token; it is never
// exposed to browsers directly.
package gateway

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/store"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/workflows"
)

const (
	maxBody   = 1 << 20 // CV text + profile JSON
	maxCVText = 60_000
	demoTTL   = 24 * time.Hour
)

// Demo runs are smaller than personal ones: one visitor must not drain the free LLM quota.
var DemoLimits = &pb.Limits{FetchConcurrency: 8, MatchTopK: 25, MaxScored: 10, MaxDrafts: 3, MinDraftScore: 7}

type Server struct {
	Store    *store.Store
	Temporal client.Client
	Sources  func() ([]*pb.Source, error)
	Token    string
	Log      *slog.Logger
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
	mux.Handle("POST /v1/profiles", s.auth(s.createProfile))
	mux.Handle("POST /v1/runs", s.auth(s.startRun))
	mux.Handle("GET /v1/runs/{id}", s.auth(s.getRun))
	mux.Handle("GET /v1/runs/{id}/events", s.auth(s.runEvents))
	mux.Handle("GET /v1/profiles/{id}/results", s.auth(s.results))
	mux.Handle("GET /v1/profiles/{id}/summary", s.auth(s.summary))
	mux.Handle("POST /v1/outbox/{id}/decision", s.auth(s.decide))
	return otelhttp.NewHandler(mux, "gateway", otelhttp.WithSpanNameFormatter(
		func(_ string, r *http.Request) string { return r.Method + " " + r.Pattern }))
}

func (s *Server) auth(h http.HandlerFunc) http.Handler {
	want := []byte("Bearer " + s.Token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), want) != 1 {
			writeErr(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		h(w, r)
	})
}

// ---- profiles ----

type createProfileReq struct {
	ID      string          `json:"id"`
	Kind    string          `json:"kind"`  // personal | demo
	Owner   string          `json:"owner"` // e.g. "github:12345"; demo quota key upstream
	Profile json.RawMessage `json:"profile"`
	CVText  string          `json:"cv_text"`
}

func (s *Server) createProfile(w http.ResponseWriter, r *http.Request) {
	var req createProfileReq
	if !readJSON(w, r, &req) {
		return
	}
	if req.Kind != "personal" && req.Kind != "demo" {
		writeErr(w, http.StatusBadRequest, "kind must be personal or demo")
		return
	}
	req.CVText = strings.TrimSpace(req.CVText)
	if len(req.CVText) < 200 || len(req.CVText) > maxCVText {
		writeErr(w, http.StatusBadRequest, "cv_text must be 200-60000 characters")
		return
	}
	if req.Owner == "" || !json.Valid(req.Profile) {
		writeErr(w, http.StatusBadRequest, "owner and a JSON profile are required")
		return
	}
	p := store.Profile{ID: req.ID, Kind: req.Kind, Owner: req.Owner, Profile: req.Profile, CVText: req.CVText}
	if p.Kind == "demo" {
		ok, err := s.Store.ClaimDemo(r.Context(), req.Owner)
		if err != nil {
			s.fail(w, err)
			return
		}
		if !ok {
			writeErr(w, http.StatusTooManyRequests, "this account already used its demo run")
			return
		}
		p.ID = "demo-" + randomID() // visitors never choose ids
		exp := time.Now().Add(demoTTL)
		p.ExpiresAt = &exp
	} else if p.ID == "" {
		writeErr(w, http.StatusBadRequest, "personal profiles need an id")
		return
	}
	if err := s.Store.UpsertProfile(r.Context(), p); err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": p.ID, "expires_at": p.ExpiresAt})
}

// ---- runs ----

type startRunReq struct {
	ProfileID string `json:"profile_id"`
}

// WorkflowID is deterministic per profile (and day, for personal runs). Temporal rejects
// a second start with the same id, which enforces one demo run per visitor server-side.
func WorkflowID(p store.Profile, now time.Time) string {
	if p.Kind == "demo" {
		return "radar-" + p.ID
	}
	return "radar-" + p.ID + "-" + now.UTC().Format("20060102-1504")
}

func (s *Server) startRun(w http.ResponseWriter, r *http.Request) {
	var req startRunReq
	if !readJSON(w, r, &req) {
		return
	}
	p, err := s.Store.GetProfile(r.Context(), req.ProfileID)
	if err != nil {
		s.fail(w, err)
		return
	}
	sources, err := s.Sources()
	if err != nil {
		s.fail(w, err)
		return
	}
	in := &pb.RadarRequest{ProfileId: p.ID, Sources: sources, Mode: pb.RunMode_RUN_MODE_PERSONAL}
	if p.Kind == "demo" {
		in.Mode, in.Limits = pb.RunMode_RUN_MODE_DEMO, DemoLimits
	}
	id := WorkflowID(p, time.Now())
	// Record the run before starting it, so a status poll right after this call finds it.
	if err := s.Store.StartRun(r.Context(), id, p.ID, in.Mode.String()); err != nil {
		s.fail(w, err)
		return
	}
	run, err := s.Temporal.ExecuteWorkflow(r.Context(), client.StartWorkflowOptions{
		ID:                    id,
		TaskQueue:             workflows.IngestQueue,
		WorkflowIDReusePolicy: enumspb.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
		WorkflowRunTimeout:    workflows.ApprovalWindow + 6*time.Hour,
	}, workflows.RadarWorkflow, in)
	var started *serviceerror.WorkflowExecutionAlreadyStarted
	if errors.As(err, &started) {
		writeErr(w, http.StatusConflict, "this profile already has a run")
		return
	}
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"workflow_id": run.GetID()})
}

// progress is the live query result while the workflow runs, else the stored final state.
func (s *Server) progress(ctx context.Context, id string) (json.RawMessage, string, error) {
	run, err := s.Store.GetRun(ctx, id)
	if err != nil {
		return nil, "", err
	}
	if run.Status != "running" {
		return run.Progress, run.Status, nil
	}
	v, err := s.Temporal.QueryWorkflow(ctx, id, "", workflows.ProgressQuery)
	if err != nil {
		return run.Progress, run.Status, nil // worker busy or restarting: serve last known
	}
	var p pb.RadarProgress
	if err := v.Get(&p); err != nil {
		return nil, "", err
	}
	b, err := protojson.MarshalOptions{EmitUnpopulated: true}.Marshal(&p)
	return b, run.Status, err
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	prog, status, err := s.progress(r.Context(), id)
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"workflow_id": id, "status": status, "progress": prog})
}

// runEvents streams progress as Server-Sent Events until the run ends.
func (s *Server) runEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	tick := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	var last string
	for {
		prog, status, err := s.progress(r.Context(), id)
		if err != nil {
			fmt.Fprintf(w, "event: error\ndata: %q\n\n", err.Error())
			flusher.Flush()
			return
		}
		if msg := fmt.Sprintf(`{"status":%q,"progress":%s}`, status, orNull(prog)); msg != last {
			fmt.Fprintf(w, "data: %s\n\n", msg)
			flusher.Flush()
			last = msg
		}
		if status != "running" {
			fmt.Fprint(w, "event: done\ndata: {}\n\n")
			flusher.Flush()
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-tick.C:
		}
	}
}

// ---- results ----

func (s *Server) results(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 || limit > 100 {
		limit = 30
	}
	res, err := s.Store.Results(r.Context(), r.PathValue("id"), limit)
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": res})
}

func (s *Server) summary(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	counts, err := s.Store.Counts(r.Context(), id)
	if err != nil {
		s.fail(w, err)
		return
	}
	runs, err := s.Store.ListRuns(r.Context(), id, 14)
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"counts": counts, "runs": runs})
}

type decisionReq struct {
	ProfileID  string `json:"profile_id"`
	WorkflowID string `json:"workflow_id"`
	Approve    bool   `json:"approve"`
	Body       string `json:"body"`
}

// decide signals the run that drafted the message, if it is still open; otherwise it
// records the decision directly.
func (s *Server) decide(w http.ResponseWriter, r *http.Request) {
	outboxID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad outbox id")
		return
	}
	var req decisionReq
	if !readJSON(w, r, &req) {
		return
	}
	p, err := s.Store.GetProfile(r.Context(), req.ProfileID)
	if err != nil {
		s.fail(w, err)
		return
	}
	if p.Kind != "personal" {
		writeErr(w, http.StatusForbidden, "demo drafts are never sent")
		return
	}
	sig := &pb.ApprovalSignal{OutboxId: outboxID, Approve: req.Approve, EditedBody: req.Body}
	if req.WorkflowID != "" {
		err = s.Temporal.SignalWorkflow(r.Context(), req.WorkflowID, "", workflows.ApprovalSignal, sig)
		if err == nil {
			writeJSON(w, http.StatusAccepted, map[string]string{"via": "signal"})
			return
		}
	}
	if err := s.Store.DecideDraft(r.Context(), p.ID, outboxID, req.Approve, req.Body); err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"via": "db"})
}

// ---- helpers ----

func (s *Server) fail(w http.ResponseWriter, err error) {
	if errors.Is(err, store.ErrNotFound) {
		writeErr(w, http.StatusNotFound, "not found")
		return
	}
	s.Log.Error("request failed", "error", err)
	writeErr(w, http.StatusInternalServerError, "internal error")
}

func readJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func orNull(b json.RawMessage) string {
	if len(b) == 0 {
		return "null"
	}
	return string(b)
}

func randomID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
