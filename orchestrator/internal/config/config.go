// Package config reads environment settings and the shared companies.yaml source list.
package config

import (
	"os"
	"sort"

	"gopkg.in/yaml.v3"

	pb "github.com/Mrwanawad/JobRadar/orchestrator/gen/jobradar/v1"
	"github.com/Mrwanawad/JobRadar/orchestrator/internal/ingest"
)

func Env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

type Settings struct {
	DatabaseURL       string
	TemporalAddress   string
	TemporalNamespace string
	CompaniesFile     string
}

func Load() Settings {
	return Settings{
		DatabaseURL:       Env("DATABASE_URL", "postgres://jobradar:jobradar@localhost:5432/jobradar?sslmode=disable"),
		TemporalAddress:   Env("TEMPORAL_ADDRESS", "localhost:7233"),
		TemporalNamespace: Env("TEMPORAL_NAMESPACE", "jobradar"),
		CompaniesFile:     Env("COMPANIES_FILE", "companies.yaml"),
	}
}

// LoadSources reads companies.yaml ({ats: {slug: name}}) and appends the aggregator feeds,
// in a stable order so schedules produce identical requests.
func LoadSources(path string) ([]*pb.Source, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var boards map[string]map[string]string
	if err := yaml.Unmarshal(raw, &boards); err != nil {
		return nil, err
	}
	out := []*pb.Source{}
	kinds := make([]string, 0, len(boards))
	for k := range boards {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	for _, kind := range kinds {
		slugs := make([]string, 0, len(boards[kind]))
		for s := range boards[kind] {
			slugs = append(slugs, s)
		}
		sort.Strings(slugs)
		for _, slug := range slugs {
			out = append(out, &pb.Source{Kind: kind, Slug: slug, Name: boards[kind][slug]})
		}
	}
	for _, f := range ingest.Feeds {
		out = append(out, &pb.Source{Kind: "feed", Slug: f, Name: f})
	}
	return out, nil
}
