// Package ingest fetches job boards and feeds and normalizes them into Job values.
//
// It is a port of the Python parsers in jobradar/ats and jobradar/sources. Both
// implementations are tested against the same fixtures and golden outputs
// (tests/fixtures/golden), so a job parsed by either side has the same uid and fields.
package ingest

import (
	"crypto/sha1"
	"encoding/hex"
	"html"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Job is the common shape every parser produces.
type Job struct {
	Source      string     `json:"source"`
	ExternalID  string     `json:"external_id"`
	Company     string     `json:"company"`
	Title       string     `json:"title"`
	Location    string     `json:"location"`
	Remote      *bool      `json:"remote"`
	URL         string     `json:"url"`
	ApplyURL    string     `json:"apply_url"`
	Description string     `json:"description"`
	PostedAt    *time.Time `json:"posted_at"`
	Salary      string     `json:"salary"`
	Tags        []string   `json:"tags"`
	Emails      []string   `json:"emails"`
	Phones      []string   `json:"phones"`
}

// UID is stable across runs and languages: sha1("source:external_id")[:16].
func (j Job) UID() string {
	sum := sha1.Sum([]byte(j.Source + ":" + j.ExternalID))
	return hex.EncodeToString(sum[:])[:16]
}

var nonAlnum = regexp.MustCompile(`[^a-z0-9]+`)

// Fingerprint collapses the same role cross-posted on several boards.
func Fingerprint(company, title string) string {
	norm := func(s string) string {
		return strings.TrimSpace(nonAlnum.ReplaceAllString(strings.ToLower(s), " "))
	}
	return norm(company) + "|" + norm(title)
}

var (
	reTag   = regexp.MustCompile(`<[^>]+>`)
	reWS    = regexp.MustCompile(`[ \t\r\f\v]+`)
	reNL    = regexp.MustCompile(`\n{3,}`)
	reBlock = regexp.MustCompile(`(?i)<\s*(br|/p|p|/div|/li|li|/h\d)[^>]*>`)
	// Python's str.splitlines boundaries that survive the whitespace pass.
	lineBreaks = strings.NewReplacer("\x1c", "\n", "\x1d", "\n", "\x1e", "\n",
		"\u0085", "\n", " ", "\n", " ", "\n")
)

// HTMLToText turns posting HTML into readable plain text with paragraph breaks.
func HTMLToText(s string) string {
	if s == "" {
		return ""
	}
	s = html.UnescapeString(s) // some feeds double-escape
	s = reBlock.ReplaceAllString(s, "\n")
	s = html.UnescapeString(reTag.ReplaceAllString(s, ""))
	s = reWS.ReplaceAllString(s, " ")
	lines := strings.Split(lineBreaks.Replace(s), "\n")
	for i, l := range lines {
		lines[i] = strings.TrimFunc(l, isPySpace)
	}
	return strings.TrimFunc(reNL.ReplaceAllString(strings.Join(lines, "\n"), "\n\n"), isPySpace)
}

// isPySpace matches Python's str.isspace, which also counts the ASCII separators.
func isPySpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

var (
	reEmail   = regexp.MustCompile(`(?i)[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}`)
	rePhone   = regexp.MustCompile(`(?:\+|00)\d[\d\s().-]{7,16}\d`)
	reNonDig  = regexp.MustCompile(`\D`)
	junkEmail = []string{"example.com", "sentry.io", "wixpress.com", ".png", ".jpg"}
)

// ExtractContacts finds hiring emails and phone numbers published inside a posting.
func ExtractContacts(text string) (emails, phones []string) {
	emails, phones = []string{}, []string{}
	for _, e := range reEmail.FindAllString(text, -1) {
		e = strings.ToLower(strings.Trim(e, "."))
		if !contains(emails, e) && !containsAny(e, junkEmail) {
			emails = append(emails, e)
		}
	}
	for _, p := range rePhone.FindAllString(text, -1) {
		norm := "+" + strings.TrimPrefix(reNonDig.ReplaceAllString(p, ""), "00")
		if n := len(norm); n >= 10 && n <= 16 && !contains(phones, norm) {
			phones = append(phones, norm)
		}
	}
	return emails, phones
}

func withContacts(j Job) Job {
	j.Emails, j.Phones = ExtractContacts(j.Description)
	return j
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func containsAny(s string, subs []string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// nonEmpty keeps the truthy strings, like Python's filter(None, ...).
func nonEmpty(xs ...string) []string {
	out := []string{}
	for _, x := range xs {
		if x != "" {
			out = append(out, x)
		}
	}
	return out
}

func truePtr() *bool { t := true; return &t }

// trueOrNil mirrors `cond or None`: the boards only ever say "yes, remote" reliably.
func trueOrNil(cond bool) *bool {
	if cond {
		return truePtr()
	}
	return nil
}

var isoLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02 15:04:05.999999999",
	"2006-01-02T15:04Z07:00",
	"2006-01-02T15:04",
	"2006-01-02",
}

// FromISO parses the ISO-8601 variants the boards emit; naive times are UTC.
func FromISO(v string) *time.Time {
	if v == "" {
		return nil
	}
	for _, layout := range isoLayouts {
		if t, err := time.Parse(layout, v); err == nil {
			return &t
		}
	}
	return nil
}

// FromEpoch parses unix seconds (or milliseconds) given as a number or a string.
func FromEpoch(v any, ms bool) *time.Time {
	var n float64
	switch x := v.(type) {
	case nil:
		return nil
	case float64:
		n = x
	case string:
		f, err := strconv.ParseFloat(x, 64)
		if err != nil {
			return nil
		}
		n = f
	default:
		return nil
	}
	if n == 0 {
		return nil
	}
	// Round to microseconds like Python's datetime.fromtimestamp.
	scale := 1e6
	if ms {
		scale = 1e3
	}
	t := time.UnixMicro(int64(math.Round(n * scale))).UTC()
	return &t
}

// truncRunes cuts to n characters (not bytes), like a Python slice.
func truncRunes(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// commas formats 99000 as 99,000 (Python's "{:,}").
func commas(n int64) string {
	s := strconv.FormatInt(n, 10)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	var b strings.Builder
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	if neg {
		return "-" + b.String()
	}
	return b.String()
}
