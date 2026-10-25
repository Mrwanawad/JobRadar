package ingest

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

const fixtures = "../../../tests/fixtures"

type golden struct {
	Job
	UID string `json:"uid"`
}

func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(fixtures, name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// TestParityWithPython checks every Go parser against the Python parsers' output on the
// same captured API responses (scripts/golden_jobs.py).
func TestParityWithPython(t *testing.T) {
	cases := map[string]func() ([]Job, error){
		"remoteok": func() ([]Job, error) { return ParseRemoteOK(readFixture(t, "remoteok.json")) },
		"wwr":      func() ([]Job, error) { return ParseWWR(readFixture(t, "wwr_rss.xml")) },
		"hn":       func() ([]Job, error) { return ParseHNThread(readFixture(t, "hn_item.json")) },
		"himalayas": func() ([]Job, error) {
			jobs, _, err := ParseHimalayas(readFixture(t, "himalayas.json"))
			return jobs, err
		},
	}
	for ats, parse := range BoardParsers {
		cases[ats] = func() ([]Job, error) { return parse(readFixture(t, ats+".json"), "Fixture Co") }
	}

	for name, parse := range cases {
		t.Run(name, func(t *testing.T) {
			got, err := parse()
			if err != nil {
				t.Fatal(err)
			}
			var want []golden
			if err := json.Unmarshal(readFixture(t, "golden/"+name+".json"), &want); err != nil {
				t.Fatal(err)
			}
			if len(got) != len(want) {
				t.Fatalf("got %d jobs, want %d", len(got), len(want))
			}
			for i := range want {
				g, w := got[i], want[i]
				if g.UID() != w.UID {
					t.Errorf("[%d] uid %s, want %s", i, g.UID(), w.UID)
				}
				if !sameTime(g.PostedAt, w.PostedAt) {
					t.Errorf("[%d] posted_at %v, want %v", i, g.PostedAt, w.PostedAt)
				}
				g.PostedAt, w.PostedAt = nil, nil
				if !reflect.DeepEqual(g, w.Job) {
					diff(t, i, g, w.Job)
				}
			}
		})
	}
}

func sameTime(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Equal(*b)
}

func diff(t *testing.T, i int, got, want Job) {
	t.Helper()
	gv, wv := reflect.ValueOf(got), reflect.ValueOf(want)
	for f := 0; f < gv.NumField(); f++ {
		if !reflect.DeepEqual(gv.Field(f).Interface(), wv.Field(f).Interface()) {
			t.Errorf("[%d] %s:\n got  %#v\n want %#v", i, gv.Type().Field(f).Name,
				gv.Field(f).Interface(), wv.Field(f).Interface())
		}
	}
}

func TestExtractContacts(t *testing.T) {
	e, p := ExtractContacts("mail jobs@acme.io or +20 100 000 0000, not logo@2x.png")
	if !reflect.DeepEqual(e, []string{"jobs@acme.io"}) || !reflect.DeepEqual(p, []string{"+201000000000"}) {
		t.Fatalf("got %v %v", e, p)
	}
}

func TestFingerprint(t *testing.T) {
	if got := Fingerprint("ACME, Inc.", "Senior  AI/ML Engineer"); got != "acme inc|senior ai ml engineer" {
		t.Fatal(got)
	}
}
