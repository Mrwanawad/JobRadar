package ingest

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

const (
	userAgent    = "jobradar/0.2 (+https://github.com/Mrwanawad/JobRadar)"
	maxBodyBytes = 64 << 20

	remoteOKURL  = "https://remoteok.com/api"
	wwrURL       = "https://weworkremotely.com/remote-jobs.rss"
	himalayasURL = "https://himalayas.app/jobs/api"
	hnSearchURL  = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=5"
	hnItemURL    = "https://hn.algolia.com/api/v1/items/%s"
)

// Feeds are the aggregator sources, fetched with kind = "feed".
var Feeds = []string{"remoteok", "wwr", "himalayas", "hn"}

// StatusError is a non-2xx response. 4xx (except 429) is permanent: a retry will not fix a
// board that was renamed or removed.
type StatusError struct {
	URL  string
	Code int
}

func (e *StatusError) Error() string { return fmt.Sprintf("GET %s: HTTP %d", e.URL, e.Code) }

func (e *StatusError) Permanent() bool {
	return e.Code >= 400 && e.Code < 500 && e.Code != http.StatusTooManyRequests
}

// Fetcher downloads boards and feeds over one traced HTTP client.
type Fetcher struct {
	HTTP *http.Client
}

func NewFetcher() *Fetcher {
	return &Fetcher{HTTP: &http.Client{
		Timeout:   60 * time.Second,
		Transport: otelhttp.NewTransport(http.DefaultTransport),
	}}
}

func (f *Fetcher) get(ctx context.Context, u string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := f.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return nil, &StatusError{URL: u, Code: resp.StatusCode}
	}
	return io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))
}

// Fetch returns every job a source currently lists.
func (f *Fetcher) Fetch(ctx context.Context, kind, slug, name string) ([]Job, error) {
	if kind == "feed" {
		return f.fetchFeed(ctx, slug)
	}
	tmpl, ok := BoardURLs[kind]
	if !ok {
		return nil, fmt.Errorf("unknown source kind %q", kind)
	}
	raw, err := f.get(ctx, fmt.Sprintf(tmpl, url.PathEscape(slug)))
	if err != nil {
		return nil, err
	}
	return BoardParsers[kind](raw, name)
}

func (f *Fetcher) fetchFeed(ctx context.Context, slug string) ([]Job, error) {
	switch slug {
	case "remoteok":
		raw, err := f.get(ctx, remoteOKURL)
		if err != nil {
			return nil, err
		}
		return ParseRemoteOK(raw)
	case "wwr":
		raw, err := f.get(ctx, wwrURL)
		if err != nil {
			return nil, err
		}
		return ParseWWR(raw)
	case "himalayas":
		return f.fetchHimalayas(ctx, 5)
	case "hn":
		return f.fetchHN(ctx)
	}
	return nil, fmt.Errorf("unknown feed %q", slug)
}

func (f *Fetcher) fetchHimalayas(ctx context.Context, pages int) ([]Job, error) {
	out := []Job{}
	cursor := ""
	for range pages {
		u := himalayasURL + "?limit=20"
		if cursor != "" {
			u += "&cursor=" + url.QueryEscape(cursor)
		}
		raw, err := f.get(ctx, u)
		if err != nil {
			return nil, err
		}
		jobs, next, err := ParseHimalayas(raw)
		if err != nil {
			return nil, err
		}
		out = append(out, jobs...)
		if cursor = next; cursor == "" {
			break
		}
	}
	return out, nil
}

// fetchHN finds the latest "Who is hiring?" story, then reads its comment tree.
func (f *Fetcher) fetchHN(ctx context.Context) ([]Job, error) {
	raw, err := f.get(ctx, hnSearchURL)
	if err != nil {
		return nil, err
	}
	var search struct {
		Hits []struct {
			Title    string `json:"title"`
			ObjectID string `json:"objectID"`
		} `json:"hits"`
	}
	if err := json.Unmarshal(raw, &search); err != nil {
		return nil, err
	}
	for _, h := range search.Hits {
		if strings.HasPrefix(h.Title, "Ask HN: Who is hiring?") {
			item, err := f.get(ctx, fmt.Sprintf(hnItemURL, h.ObjectID))
			if err != nil {
				return nil, err
			}
			return ParseHNThread(item)
		}
	}
	return []Job{}, nil
}
