package ingest

import (
	"bytes"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/text/encoding/charmap"
)

// The board APIs are loosely typed (fields go missing, null or change type), so parsers
// walk generic JSON with the helpers below rather than strict structs.
type obj = map[string]any

func get(m obj, key string) obj {
	v, _ := m[key].(obj)
	if v == nil {
		return obj{}
	}
	return v
}

func list(m obj, key string) []any {
	v, _ := m[key].([]any)
	return v
}

func str(m obj, key string) string {
	switch v := m[key].(type) {
	case string:
		return v
	case float64:
		return fmt.Sprint(int64(v))
	}
	return ""
}

func truthy(m obj, key string) bool {
	switch v := m[key].(type) {
	case bool:
		return v
	case string:
		return v != ""
	case float64:
		return v != 0
	case nil:
		return false
	}
	return true
}

func objs(xs []any) []obj {
	out := make([]obj, 0, len(xs))
	for _, x := range xs {
		if m, ok := x.(obj); ok {
			out = append(out, m)
		}
	}
	return out
}

func strs(xs []any) []string {
	out := []string{}
	for _, x := range xs {
		if s, ok := x.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func or(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func decode[T any](raw []byte) (T, error) {
	var v T
	err := json.Unmarshal(raw, &v)
	return v, err
}

// ---- ATS boards ----

// BoardURLs are the public, unauthenticated job board endpoints (docs/ENDPOINTS.md).
var BoardURLs = map[string]string{
	"greenhouse":      "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true",
	"lever":           "https://api.lever.co/v0/postings/%s?mode=json",
	"ashby":           "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true",
	"smartrecruiters": "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100",
	"workable":        "https://apply.workable.com/api/v1/widget/accounts/%s",
}

// BoardParsers turn one board's raw JSON into jobs; company is the display name to use
// when the payload does not carry one.
var BoardParsers = map[string]func(raw []byte, company string) ([]Job, error){
	"greenhouse":      ParseGreenhouse,
	"lever":           ParseLever,
	"ashby":           ParseAshby,
	"smartrecruiters": ParseSmartRecruiters,
	"workable":        ParseWorkable,
}

func ParseGreenhouse(raw []byte, company string) ([]Job, error) {
	data, err := decode[obj](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(list(data, "jobs")) {
		loc := str(get(j, "location"), "name")
		out = append(out, Job{
			Source:      "greenhouse",
			ExternalID:  str(j, "id"),
			Company:     or(str(j, "company_name"), company),
			Title:       str(j, "title"),
			Location:    loc,
			Remote:      trueOrNil(strings.Contains(strings.ToLower(loc), "remote")),
			URL:         str(j, "absolute_url"),
			Description: HTMLToText(str(j, "content")),
			// updated_at is edit time; first_published is the real post date
			PostedAt: FromISO(or(str(j, "first_published"), str(j, "updated_at"))),
			Tags:     []string{}, Emails: []string{}, Phones: []string{},
		})
	}
	return out, nil
}

func ParseLever(raw []byte, company string) ([]Job, error) {
	data, err := decode[[]any](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(data) {
		cat := get(j, "categories")
		locs := strs(list(cat, "allLocations"))
		if len(locs) == 0 {
			locs = []string{str(cat, "location")}
		}
		parts := []string{str(j, "descriptionPlain")}
		for _, x := range objs(list(j, "lists")) {
			parts = append(parts, str(x, "text")+"\n"+HTMLToText(str(x, "content")))
		}
		parts = append(parts, str(j, "additionalPlain"))
		out = append(out, Job{
			Source:      "lever",
			ExternalID:  str(j, "id"),
			Company:     company,
			Title:       str(j, "text"),
			Location:    strings.Join(nonEmpty(locs...), "; "),
			Remote:      trueOrNil(str(j, "workplaceType") == "remote"),
			URL:         str(j, "hostedUrl"),
			ApplyURL:    str(j, "applyUrl"),
			Description: strings.TrimFunc(strings.Join(parts, "\n\n"), isPySpace),
			PostedAt:    FromEpoch(j["createdAt"], true),
			Tags:        nonEmpty(str(cat, "team"), str(cat, "commitment")),
			Emails:      []string{}, Phones: []string{},
		})
	}
	return out, nil
}

func ParseAshby(raw []byte, company string) ([]Job, error) {
	data, err := decode[obj](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(list(data, "jobs")) {
		if v, ok := j["isListed"].(bool); ok && !v {
			continue
		}
		locs := []string{str(j, "location")}
		for _, s := range objs(list(j, "secondaryLocations")) {
			locs = append(locs, str(s, "location"))
		}
		desc := str(j, "descriptionPlain")
		if desc == "" {
			desc = HTMLToText(str(j, "descriptionHtml"))
		}
		out = append(out, Job{
			Source:      "ashby",
			ExternalID:  str(j, "id"),
			Company:     company,
			Title:       str(j, "title"),
			Location:    strings.Join(nonEmpty(locs...), "; "),
			Remote:      trueOrNil(truthy(j, "isRemote") || str(j, "workplaceType") == "Remote"),
			URL:         str(j, "jobUrl"),
			ApplyURL:    str(j, "applyUrl"),
			Description: desc,
			PostedAt:    FromISO(str(j, "publishedAt")),
			Salary:      str(get(j, "compensation"), "scrapeableCompensationSalarySummary"),
			Tags:        nonEmpty(str(j, "department"), str(j, "employmentType")),
			Emails:      []string{}, Phones: []string{},
		})
	}
	return out, nil
}

func ParseSmartRecruiters(raw []byte, company string) ([]Job, error) {
	data, err := decode[obj](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(list(data, "content")) {
		loc := get(j, "location")
		co := get(j, "company")
		cid := company
		if _, ok := co["identifier"]; ok {
			cid = str(co, "identifier")
		}
		id := str(j, "id")
		out = append(out, Job{
			Source:     "smartrecruiters",
			ExternalID: id,
			Company:    or(str(co, "name"), company),
			Title:      str(j, "name"),
			Location:   str(loc, "fullLocation"),
			Remote:     trueOrNil(truthy(loc, "remote")),
			URL:        fmt.Sprintf("https://jobs.smartrecruiters.com/%s/%s", cid, id),
			PostedAt:   FromISO(str(j, "releasedDate")),
			Tags:       nonEmpty(str(get(j, "experienceLevel"), "label")),
			Emails:     []string{}, Phones: []string{},
		})
	}
	return out, nil
}

func ParseWorkable(raw []byte, company string) ([]Job, error) {
	data, err := decode[obj](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(list(data, "jobs")) {
		locs := []string{}
		for _, x := range objs(list(j, "locations")) {
			locs = append(locs, strings.Join(nonEmpty(str(x, "city"), str(x, "country")), ", "))
		}
		if len(locs) == 0 {
			locs = []string{strings.Join(nonEmpty(str(j, "city"), str(j, "country")), ", ")}
		}
		out = append(out, Job{
			Source:     "workable",
			ExternalID: str(j, "shortcode"),
			Company:    or(str(data, "name"), company),
			Title:      str(j, "title"),
			Location:   strings.Join(nonEmpty(locs...), "; "),
			Remote:     trueOrNil(truthy(j, "telecommuting")),
			URL:        str(j, "url"),
			ApplyURL:   str(j, "application_url"),
			PostedAt:   FromISO(str(j, "published_on")),
			Tags:       nonEmpty(str(j, "department"), str(j, "experience")),
			Emails:     []string{}, Phones: []string{},
		})
	}
	return out, nil
}

// ---- Aggregator feeds ----

func ParseRemoteOK(raw []byte) ([]Job, error) {
	data, err := decode[[]any](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, j := range objs(data) {
		_, legal := j["legal"]
		_, hasID := j["id"]
		if legal || !hasID { // first element is the API terms notice
			continue
		}
		lo, _ := j["salary_min"].(float64)
		hi, _ := j["salary_max"].(float64)
		salary := ""
		if hi != 0 {
			salary = fmt.Sprintf("$%s-$%s", commas(int64(lo)), commas(int64(hi)))
		}
		posted := FromISO(str(j, "date"))
		if posted == nil {
			posted = FromEpoch(j["epoch"], false)
		}
		out = append(out, withContacts(Job{
			Source:      "remoteok",
			ExternalID:  str(j, "id"),
			Company:     str(j, "company"),
			Title:       str(j, "position"),
			Location:    or(str(j, "location"), "Remote"),
			Remote:      truePtr(),
			URL:         str(j, "url"),
			ApplyURL:    str(j, "apply_url"),
			Description: HTMLToText(str(j, "description")),
			PostedAt:    posted,
			Salary:      salary,
			Tags:        strs(list(j, "tags")),
		}))
	}
	return out, nil
}

type rssItem struct {
	Title       string `xml:"title"`
	Link        string `xml:"link"`
	GUID        string `xml:"guid"`
	Description string `xml:"description"`
	PubDate     string `xml:"pubDate"`
	Region      string `xml:"region"`
	Category    string `xml:"category"`
	Skills      string `xml:"skills"`
}

func ParseWWR(raw []byte) ([]Job, error) {
	// Stream items so a truncated or slightly broken feed still yields every complete
	// item before the damage (feedparser's behaviour on the Python side).
	// A feed that is not valid UTF-8 is really Windows-1252 (smart quotes, dashes); decode it
	// as such, like feedparser's fallback.
	if !utf8.Valid(raw) {
		if dec, err := charmap.Windows1252.NewDecoder().Bytes(raw); err == nil {
			raw = dec
		}
	}
	dec := xml.NewDecoder(bytes.NewReader(raw))
	dec.Strict = false
	items := []rssItem{}
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		if se, ok := tok.(xml.StartElement); ok && se.Name.Local == "item" {
			var it rssItem
			if err := dec.DecodeElement(&it, &se); err != nil {
				break
			}
			items = append(items, it)
		}
	}
	if len(items) == 0 && !bytes.Contains(raw, []byte("<rss")) {
		return nil, fmt.Errorf("wwr: not an RSS feed")
	}
	out := []Job{}
	for _, e := range items {
		link := strings.TrimSpace(e.Link)
		if link == "" { // truncated/partial entry
			continue
		}
		company, title, found := strings.Cut(strings.TrimSpace(e.Title), ": ")
		if !found {
			company, title = "", company
		}
		var posted *time.Time
		if t, err := time.Parse(time.RFC1123Z, strings.TrimSpace(e.PubDate)); err == nil {
			t = t.UTC()
			posted = &t
		}
		out = append(out, withContacts(Job{
			Source:      "wwr",
			ExternalID:  or(strings.TrimSpace(e.GUID), link),
			Company:     strings.TrimSpace(company),
			Title:       strings.TrimSpace(title),
			Location:    or(strings.TrimSpace(e.Region), "Remote"),
			Remote:      truePtr(),
			URL:         link,
			Description: HTMLToText(e.Description),
			PostedAt:    posted,
			Tags:        nonEmpty(strings.TrimSpace(e.Category), strings.TrimSpace(e.Skills)),
		}))
	}
	return out, nil
}

func ParseHimalayas(raw []byte) (jobs []Job, nextCursor string, err error) {
	data, err := decode[obj](raw)
	if err != nil {
		return nil, "", err
	}
	out := []Job{}
	for _, j := range objs(list(data, "jobs")) {
		loc := "Remote (Anywhere)"
		if restr := strs(list(j, "locationRestrictions")); len(restr) > 0 {
			loc = "Remote (" + strings.Join(restr, ", ") + ")"
		}
		lo, _ := j["minSalary"].(float64)
		hi, _ := j["maxSalary"].(float64)
		salary := ""
		if lo != 0 && hi != 0 {
			salary = strings.TrimSpace(fmt.Sprintf("%s %s-%s", str(j, "currency"),
				commas(int64(lo)), commas(int64(hi))))
		}
		out = append(out, withContacts(Job{
			Source:      "himalayas",
			ExternalID:  str(j, "guid"),
			Company:     str(j, "companyName"),
			Title:       str(j, "title"),
			Location:    loc,
			Remote:      truePtr(),
			URL:         str(j, "guid"),
			ApplyURL:    str(j, "applicationLink"),
			Description: HTMLToText(or(str(j, "description"), str(j, "excerpt"))),
			PostedAt:    FromEpoch(j["pubDate"], false),
			Salary:      salary,
			Tags:        append(strs(list(j, "seniority")), strs(list(j, "categories"))...),
		}))
	}
	return out, str(data, "nextCursor"), nil
}

var (
	reHNSplit  = regexp.MustCompile(`\s+[|—–]\s+|\s+-\s+`)
	reParens   = regexp.MustCompile(`\s*\(.*?\)\s*`)
	reWorkMode = regexp.MustCompile(`(?i)remote|onsite|on-site|hybrid`)
	reRemote   = regexp.MustCompile(`(?i)remote`)
)

// ParseHNThread reads an "Ask HN: Who is hiring?" thread. Each top-level comment is one
// posting whose first line is usually "Company | Role | Location | ...".
func ParseHNThread(raw []byte) ([]Job, error) {
	item, err := decode[obj](raw)
	if err != nil {
		return nil, err
	}
	out := []Job{}
	for _, c := range objs(list(item, "children")) {
		text := str(c, "text")
		if text == "" || str(c, "parent_id") != str(item, "id") {
			continue
		}
		body := HTMLToText(text)
		first, _, _ := strings.Cut(body, "\n")
		parts := []string{}
		for _, p := range reHNSplit.Split(first, -1) {
			if p = strings.TrimFunc(p, isPySpace); p != "" {
				parts = append(parts, p)
			}
		}
		company, title := "", truncRunes(first, 120)
		if len(parts) > 0 {
			company = strings.TrimFunc(reParens.ReplaceAllString(parts[0], " "), isPySpace)
		}
		if len(parts) > 1 {
			title = parts[1]
		}
		loc := ""
		for _, p := range parts {
			if reWorkMode.MatchString(p) {
				loc = p
				break
			}
		}
		if loc == "" && len(parts) > 0 && strings.Contains(parts[0], "(") {
			loc = pySlice(parts[0], strings.Index(parts[0], "(")+1, strings.LastIndex(parts[0], ")"))
		}
		id := str(c, "id")
		out = append(out, withContacts(Job{
			Source:      "hn",
			ExternalID:  id,
			Company:     truncRunes(company, 120),
			Title:       truncRunes(title, 200),
			Location:    loc,
			Remote:      trueOrNil(reRemote.MatchString(first)),
			URL:         "https://news.ycombinator.com/item?id=" + id,
			Description: body,
			PostedAt:    FromEpoch(c["created_at_i"], false),
			Tags:        []string{},
		}))
	}
	return out, nil
}

// pySlice is s[start:end] with Python's negative-end semantics (byte offsets from Index).
func pySlice(s string, start, end int) string {
	if end < 0 {
		end += len(s)
	}
	if start > len(s) || end <= start {
		return ""
	}
	return s[start:end]
}
