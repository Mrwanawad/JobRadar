// Package migrations embeds the Postgres schema. `jobradar migrate` applies it with goose;
// the Python agent worker only reads and writes the tables.
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
