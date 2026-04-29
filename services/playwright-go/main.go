// Package main — QD Playwright-Go Sidecar.
//
// Lightweight Go replacement for services/playwright (Python/Playwright).
// Provides the same HTTP API surface so QD main-end code needs no changes.
//
// Endpoints:
//
//	POST /capture  — launch headless Chrome, find sign-in button, click, return HAR
//	GET  /health   — liveness + browser ready status
//
// Environment variables:
//
//	HEADLESS           default true; set "false" for debugging
//	MAX_CONCURRENT     simultaneous browser sessions, default 2
//	DEFAULT_TIMEOUT_MS capture timeout, default 60000
//	ALLOW_HOSTS        comma-separated host whitelist; empty = allow any (SSRF risk)
//	PORT               listen port, default 8924
package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/chromedp"
)

// ---- Configuration ----

var (
	headless          = envBool("HEADLESS", true)
	maxConcurrent     = envInt("MAX_CONCURRENT", 2)
	defaultTimeoutMs  = envInt("DEFAULT_TIMEOUT_MS", 60000)
	allowHosts        = envList("ALLOW_HOSTS")
	listenPort        = envStr("PORT", "8924")
)

// ---- Request / Response models (mirror Python Pydantic schemas exactly) ----

// CaptureRequest matches app.py:CaptureRequest field-for-field.
type CaptureRequest struct {
	URL              string                 `json:"url"`
	StorageState     map[string]interface{} `json:"storage_state"`
	Cookies          string                 `json:"cookies"`
	Hint             string                 `json:"hint"`
	Selector         string                 `json:"selector"`
	UserAgent        string                 `json:"user_agent"`
	Viewport         map[string]int         `json:"viewport"`
	Locale           string                 `json:"locale"`
	TimezoneID       string                 `json:"timezone_id"`
	TimeoutMs        int                    `json:"timeout_ms"`
	WaitAfterClickMs int                    `json:"wait_after_click_ms"`
}

// CaptureResponse matches app.py:CaptureResponse field-for-field.
type CaptureResponse struct {
	OK          bool                     `json:"ok"`
	HAR         map[string]interface{}   `json:"har,omitempty"`
	Actions     []map[string]interface{} `json:"actions"`
	FoundButton map[string]interface{}   `json:"found_button,omitempty"`
	Candidates  []map[string]interface{} `json:"candidates"`
	Error       string                   `json:"error,omitempty"`
	ElapsedMs   int                      `json:"elapsed_ms"`
}

// ---- Global state ----

var (
	allocCtx    context.Context
	allocCancel context.CancelFunc
	browserMu   sync.RWMutex
	browserReady bool
	semaphore   chan struct{}
)

// ---- Main ----

func main() {
	semaphore = make(chan struct{}, maxConcurrent)

	if len(allowHosts) == 0 {
		log.Printf("[security] ALLOW_HOSTS not set — accepting any hostname. Set ALLOW_HOSTS=example.com in production to prevent SSRF.")
	}

	// Start browser allocator
	if err := startBrowser(); err != nil {
		log.Fatalf("Failed to start browser: %v", err)
	}
	defer stopBrowser()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/capture", handleCapture)

	addr := "0.0.0.0:" + listenPort
	log.Printf("QD playwright-go listening on %s (headless=%v, max_concurrent=%d)", addr, headless, maxConcurrent)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Server error: %v", err)
	}
}

// startBrowser initialises the chromedp allocator.
func startBrowser() error {
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.Flag("headless", headless),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-dev-shm-usage", true),
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
		chromedp.Flag("disable-gpu", true),
		chromedp.Flag("no-first-run", true),
		chromedp.Flag("disable-extensions", true),
	)
	aCtx, aCancel := chromedp.NewExecAllocator(context.Background(), opts...)
	allocCtx = aCtx
	allocCancel = aCancel

	// Verify browser starts correctly
	ctx, cancel := chromedp.NewContext(allocCtx)
	defer cancel()
	if err := chromedp.Run(ctx); err != nil {
		allocCancel()
		return err
	}
	browserMu.Lock()
	browserReady = true
	browserMu.Unlock()
	log.Printf("Browser ready")
	return nil
}

func stopBrowser() {
	browserMu.Lock()
	browserReady = false
	browserMu.Unlock()
	if allocCancel != nil {
		allocCancel()
	}
}

// ---- HTTP handlers ----

func handleHealth(w http.ResponseWriter, r *http.Request) {
	browserMu.RLock()
	ready := browserReady
	browserMu.RUnlock()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"ok":             true,
		"browser_ready":  ready,
		"max_concurrent": maxConcurrent,
	})
}

func handleCapture(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req CaptureRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, CaptureResponse{
			OK:         false,
			Actions:    []map[string]interface{}{},
			Candidates: []map[string]interface{}{},
			Error:      "invalid JSON: " + err.Error(),
		})
		return
	}

	// Apply defaults
	if req.TimeoutMs == 0 {
		req.TimeoutMs = defaultTimeoutMs
	}
	if req.TimeoutMs < 5000 {
		req.TimeoutMs = 5000
	}
	if req.TimeoutMs > 300000 {
		req.TimeoutMs = 300000
	}
	if req.WaitAfterClickMs > 60000 {
		req.WaitAfterClickMs = 60000
	}
	if req.Locale == "" {
		req.Locale = "zh-CN"
	}
	if req.TimezoneID == "" {
		req.TimezoneID = "Asia/Shanghai"
	}
	if req.Viewport == nil {
		req.Viewport = map[string]int{"width": 1280, "height": 800}
	}

	// URL validation
	if err := validateURL(req.URL); err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, CaptureResponse{
			OK:         false,
			Actions:    []map[string]interface{}{},
			Candidates: []map[string]interface{}{},
			Error:      err.Error(),
		})
		return
	}

	// Check browser ready
	browserMu.RLock()
	ready := browserReady
	browserMu.RUnlock()
	if !ready {
		http.Error(w, "Browser not ready", http.StatusServiceUnavailable)
		return
	}

	// Acquire semaphore
	semaphore <- struct{}{}
	defer func() { <-semaphore }()

	resp := PerformCapture(allocCtx, req)
	writeJSON(w, http.StatusOK, resp)
}

// validateURL checks scheme and optional ALLOW_HOSTS whitelist.
func validateURL(rawURL string) error {
	u, err := url.Parse(rawURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return &validationError{"URL must be http(s)://"}
	}
	if u.Hostname() == "" {
		return &validationError{"URL missing hostname"}
	}
	if len(allowHosts) > 0 {
		host := strings.ToLower(u.Hostname())
		for _, h := range allowHosts {
			if host == h || strings.HasSuffix(host, "."+h) {
				return nil
			}
		}
		return &validationError{"hostname not in ALLOW_HOSTS whitelist: " + host}
	}
	return nil
}

type validationError struct{ msg string }

func (e *validationError) Error() string { return e.msg }

// ---- Helpers ----

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("writeJSON error: %v", err)
	}
}

func envBool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	return v != "0" && strings.ToLower(v) != "false" && strings.ToLower(v) != "no"
}

func envInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envList(key string) []string {
	v := os.Getenv(key)
	if v == "" {
		return nil
	}
	var out []string
	for _, h := range strings.Split(v, ",") {
		h = strings.TrimSpace(h)
		if h != "" {
			out = append(out, h)
		}
	}
	return out
}

// elapsedMs returns milliseconds since t.
func elapsedMs(t time.Time) int {
	return int(time.Since(t).Milliseconds())
}
