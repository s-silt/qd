// Package main — har.go
// HAR 1.2 data structures and assembler.
// We capture network events via CDP (chromedp/cdproto) and assemble them
// into a HAR-compatible JSON object.
package main

import (
	"encoding/base64"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/network"
)

// ---- HAR data structures (subset of HAR 1.2) ----

type HARLog struct {
	Version string     `json:"version"`
	Creator HARCreator `json:"creator"`
	Entries []HAREntry `json:"entries"`
}

type HARCreator struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

type HARHAR struct {
	Log HARLog `json:"log"`
}

type HAREntry struct {
	StartedDateTime string      `json:"startedDateTime"`
	Time            float64     `json:"time"`
	Request         HARRequest  `json:"request"`
	Response        HARResponse `json:"response"`
	Timings         HARTimings  `json:"timings"`
}

type HARRequest struct {
	Method      string       `json:"method"`
	URL         string       `json:"url"`
	HTTPVersion string       `json:"httpVersion"`
	Headers     []HARNameVal `json:"headers"`
	QueryString []HARNameVal `json:"queryString"`
	PostData    *HARPostData `json:"postData,omitempty"`
	Cookies     []HARNameVal `json:"cookies"`
	HeadersSize int          `json:"headersSize"`
	BodySize    int          `json:"bodySize"`
}

type HARResponse struct {
	Status      int          `json:"status"`
	StatusText  string       `json:"statusText"`
	HTTPVersion string       `json:"httpVersion"`
	Headers     []HARNameVal `json:"headers"`
	Cookies     []HARNameVal `json:"cookies"`
	Content     HARContent   `json:"content"`
	RedirectURL string       `json:"redirectURL"`
	HeadersSize int          `json:"headersSize"`
	BodySize    int          `json:"bodySize"`
}

type HARContent struct {
	Size     int    `json:"size"`
	MimeType string `json:"mimeType"`
	Text     string `json:"text,omitempty"`
	Encoding string `json:"encoding,omitempty"`
}

type HARPostData struct {
	MimeType string       `json:"mimeType"`
	Text     string       `json:"text,omitempty"`
	Params   []HARNameVal `json:"params,omitempty"`
}

type HARNameVal struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type HARTimings struct {
	Send    float64 `json:"send"`
	Wait    float64 `json:"wait"`
	Receive float64 `json:"receive"`
}

// ---- Internal event recorder ----

type requestRecord struct {
	requestID  network.RequestID
	startedAt  time.Time
	request    *network.Request
	response   *network.Response
	responseAt time.Time
	bodyBytes  []byte
	finished   bool
	finishedAt time.Time
}

// HARRecorder listens to CDP network events and builds a HAR.
type HARRecorder struct {
	mu      sync.Mutex
	records map[network.RequestID]*requestRecord
}

func NewHARRecorder() *HARRecorder {
	return &HARRecorder{
		records: make(map[network.RequestID]*requestRecord),
	}
}

// OnRequestWillBeSent handles EventRequestWillBeSent.
func (h *HARRecorder) OnRequestWillBeSent(ev *network.EventRequestWillBeSent) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records[ev.RequestID] = &requestRecord{
		requestID: ev.RequestID,
		startedAt: time.Now(),
		request:   ev.Request,
	}
}

// OnResponseReceived handles EventResponseReceived.
func (h *HARRecorder) OnResponseReceived(ev *network.EventResponseReceived) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r, ok := h.records[ev.RequestID]; ok {
		r.response = ev.Response
		r.responseAt = time.Now()
	}
}

// OnLoadingFinished handles EventLoadingFinished.
func (h *HARRecorder) OnLoadingFinished(ev *network.EventLoadingFinished) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r, ok := h.records[ev.RequestID]; ok {
		r.finished = true
		r.finishedAt = time.Now()
	}
}

// SetBody stores the response body bytes for a request.
func (h *HARRecorder) SetBody(requestID network.RequestID, body []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r, ok := h.records[requestID]; ok {
		r.bodyBytes = body
	}
}

// Build assembles the recorded events into a HAR structure.
func (h *HARRecorder) Build() HARHAR {
	h.mu.Lock()
	defer h.mu.Unlock()

	var entries []HAREntry
	for _, r := range h.records {
		if r.request == nil {
			continue
		}
		entry := buildEntry(r)
		entries = append(entries, entry)
	}
	// Sort by startedDateTime for determinism
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].StartedDateTime < entries[j].StartedDateTime
	})

	if entries == nil {
		entries = []HAREntry{}
	}

	return HARHAR{
		Log: HARLog{
			Version: "1.2",
			Creator: HARCreator{Name: "qd-playwright-go", Version: "1.0.0"},
			Entries: entries,
		},
	}
}

func buildEntry(r *requestRecord) HAREntry {
	// Timing
	sendDur := 0.0
	waitDur := 0.0
	receiveDur := 0.0
	totalMs := 0.0
	if r.finished && !r.startedAt.IsZero() {
		totalMs = float64(r.finishedAt.Sub(r.startedAt).Milliseconds())
		if !r.responseAt.IsZero() {
			waitDur = float64(r.responseAt.Sub(r.startedAt).Milliseconds())
			receiveDur = float64(r.finishedAt.Sub(r.responseAt).Milliseconds())
		} else {
			waitDur = totalMs
		}
	}

	// Request
	harReq := HARRequest{
		Method:      r.request.Method,
		URL:         r.request.URL,
		HTTPVersion: "HTTP/1.1",
		Headers:     headersFromMap(r.request.Headers),
		QueryString: []HARNameVal{},
		Cookies:     []HARNameVal{},
		HeadersSize: -1,
		BodySize:    -1,
	}

	// PostData: build from PostDataEntries if present
	if r.request.HasPostData && len(r.request.PostDataEntries) > 0 {
		var sb strings.Builder
		for _, entry := range r.request.PostDataEntries {
			if entry != nil && entry.Bytes != "" {
				// PostDataEntry.Bytes is base64-encoded
				decoded, err := base64.StdEncoding.DecodeString(entry.Bytes)
				if err == nil {
					sb.Write(decoded)
				} else {
					sb.WriteString(entry.Bytes)
				}
			}
		}
		postText := sb.String()
		harReq.PostData = &HARPostData{
			MimeType: contentTypeFromHeaders(harReq.Headers),
			Text:     postText,
		}
		harReq.BodySize = len(postText)
	}

	// Response
	harResp := HARResponse{
		Status:      0,
		StatusText:  "",
		HTTPVersion: "HTTP/1.1",
		Headers:     []HARNameVal{},
		Cookies:     []HARNameVal{},
		Content:     HARContent{Size: 0, MimeType: "application/octet-stream"},
		RedirectURL: "",
		HeadersSize: -1,
		BodySize:    -1,
	}
	if r.response != nil {
		harResp.Status = int(r.response.Status)
		harResp.StatusText = r.response.StatusText
		harResp.Headers = headersFromMap(r.response.Headers)
		mimeType := r.response.MimeType
		if mimeType == "" {
			mimeType = contentTypeFromHeaders(harResp.Headers)
		}
		body := r.bodyBytes
		bodySize := len(body)
		content := HARContent{
			Size:     bodySize,
			MimeType: mimeType,
		}
		if bodySize > 0 {
			if isTextMIME(mimeType) {
				content.Text = string(body)
			} else {
				content.Text = base64.StdEncoding.EncodeToString(body)
				content.Encoding = "base64"
			}
		}
		harResp.Content = content
		harResp.BodySize = bodySize
	}

	startedAt := r.startedAt.UTC().Format(time.RFC3339Nano)

	return HAREntry{
		StartedDateTime: startedAt,
		Time:            totalMs,
		Request:         harReq,
		Response:        harResp,
		Timings: HARTimings{
			Send:    sendDur,
			Wait:    waitDur,
			Receive: receiveDur,
		},
	}
}

func headersFromMap(m network.Headers) []HARNameVal {
	if m == nil {
		return []HARNameVal{}
	}
	var out []HARNameVal
	for k, v := range m {
		out = append(out, HARNameVal{Name: k, Value: fmt.Sprintf("%v", v)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func contentTypeFromHeaders(headers []HARNameVal) string {
	for _, h := range headers {
		if strings.EqualFold(h.Name, "content-type") {
			// strip params like "; charset=utf-8"
			parts := strings.SplitN(h.Value, ";", 2)
			return strings.TrimSpace(parts[0])
		}
	}
	return "application/octet-stream"
}

func isTextMIME(mimeType string) bool {
	m := strings.ToLower(mimeType)
	return strings.HasPrefix(m, "text/") ||
		strings.Contains(m, "json") ||
		strings.Contains(m, "javascript") ||
		strings.Contains(m, "xml") ||
		strings.Contains(m, "form-urlencoded")
}
