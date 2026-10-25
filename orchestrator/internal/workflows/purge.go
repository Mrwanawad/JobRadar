package workflows

import (
	"time"

	"go.temporal.io/sdk/workflow"
)

// PurgeWorkflow deletes expired demo data. A Temporal Schedule runs it hourly, so a
// visitor's CV text never outlives its retention window by more than an hour.
func PurgeWorkflow(ctx workflow.Context) (int64, error) {
	ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           IngestQueue,
		StartToCloseTimeout: time.Minute,
	})
	var a *Activities
	var n int64
	err := workflow.ExecuteActivity(ctx, a.PurgeExpired).Get(ctx, &n)
	return n, err
}
