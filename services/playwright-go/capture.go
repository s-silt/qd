// Package main — capture.go
// PerformCapture: core headless Chrome capture logic via chromedp.
// Mirrors services/playwright/app.py:perform_capture() behaviour.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"regexp"
	"strings"
	"time"

	"github.com/chromedp/cdproto/emulation"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/chromedp"
)

const stealthInitJS = `
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
`

const defaultUA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
	"(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

var loginRedirectRe = regexp.MustCompile(`(?i)login|signin|sign-in|auth`)

// PerformCapture is the core capture function. allocCtx is the chromedp allocator context.
// It is exported so tests can inject a test allocator.
func PerformCapture(allocCtx context.Context, req CaptureRequest) CaptureResponse {
	started := time.Now()
	actions := []map[string]interface{}{}

	// 1. Resolve storage state
	storageState := resolveStorageState(req)

	// 2. Build per-session context
	ctx, cancel := chromedp.NewContext(allocCtx,
		chromedp.WithLogf(log.Printf),
	)
	defer cancel()

	timeout := time.Duration(req.TimeoutMs) * time.Millisecond
	ctx, cancelTimeout := context.WithTimeout(ctx, timeout+5*time.Second)
	defer cancelTimeout()

	// 3. HAR recorder
	recorder := NewHARRecorder()

	// 4. Register CDP network event listeners.
	// Note: body-fetch goroutines capture the session ctx (not allocCtx) so CDP
	// commands are routed to the correct browser tab.
	chromedp.ListenTarget(ctx, func(ev interface{}) {
		switch e := ev.(type) {
		case *network.EventRequestWillBeSent:
			recorder.OnRequestWillBeSent(e)
		case *network.EventResponseReceived:
			recorder.OnResponseReceived(e)
		case *network.EventLoadingFinished:
			recorder.OnLoadingFinished(e)
			// Fetch body asynchronously using the session context.
			go func(reqID network.RequestID, sessionCtx context.Context) {
				bodyCtx, bodyCancel := context.WithTimeout(sessionCtx, 5*time.Second)
				defer bodyCancel()
				var body []byte
				err := chromedp.Run(bodyCtx,
					chromedp.ActionFunc(func(bCtx context.Context) error {
						bodyBytes, err := network.GetResponseBody(reqID).Do(bCtx)
						if err != nil {
							return err
						}
						body = bodyBytes
						return nil
					}),
				)
				if err == nil && len(body) > 0 {
					recorder.SetBody(reqID, body)
				}
			}(e.RequestID, ctx)
		}
	})

	// 5. Set up browser: enable network, inject stealth + cookies
	ua := req.UserAgent
	if ua == "" {
		ua = defaultUA
	}

	var setupActions []chromedp.Action

	// Enable network domain
	setupActions = append(setupActions, network.Enable())

	// Set user agent and viewport
	setupActions = append(setupActions, chromedp.ActionFunc(func(ctx context.Context) error {
		if err := chromedp.EmulateViewport(
			int64(viewportWidth(req.Viewport)),
			int64(viewportHeight(req.Viewport)),
		).Do(ctx); err != nil {
			return err
		}
		return emulation.SetUserAgentOverride(ua).Do(ctx)
	}))

	// Add init script (stealth)
	setupActions = append(setupActions, chromedp.ActionFunc(func(ctx context.Context) error {
		_, err := page.AddScriptToEvaluateOnNewDocument(stealthInitJS).Do(ctx)
		return err
	}))

	// Inject cookies / storage state
	if storageState != nil {
		setupActions = append(setupActions, chromedp.ActionFunc(func(ctx context.Context) error {
			return injectStorageState(ctx, storageState, req.URL)
		}))
	}

	// Navigate to URL
	setupActions = append(setupActions, chromedp.ActionFunc(func(ctx context.Context) error {
		actions = append(actions, map[string]interface{}{"type": "navigate", "url": req.URL})
		navCtx, navCancel := context.WithTimeout(ctx, time.Duration(req.TimeoutMs)*time.Millisecond)
		defer navCancel()
		if err := chromedp.Navigate(req.URL).Do(navCtx); err != nil {
			if strings.Contains(err.Error(), "timeout") || strings.Contains(err.Error(), "context deadline") {
				actions = append(actions, map[string]interface{}{"type": "navigate_timeout"})
				return nil // tolerate timeout like Python version
			}
			return err
		}
		return nil
	}))

	if err := chromedp.Run(ctx, setupActions...); err != nil {
		return CaptureResponse{
			OK:         false,
			Actions:    actions,
			Candidates: []map[string]interface{}{},
			Error:      fmt.Sprintf("browser setup failed: %v", err),
			ElapsedMs:  elapsedMs(started),
		}
	}

	// 6. Wait for network idle (up to 10 s, tolerate timeout)
	idleCtx, idleCancel := context.WithTimeout(ctx, 10*time.Second)
	_ = chromedp.Run(idleCtx, chromedp.WaitReady("body", chromedp.ByQuery))
	idleCancel()

	// 7. Check for login redirect
	var currentURL string
	if err := chromedp.Run(ctx, chromedp.Location(&currentURL)); err == nil {
		if loginRedirectRe.MatchString(currentURL) && !loginRedirectRe.MatchString(req.URL) {
			return CaptureResponse{
				OK:         false,
				Actions:    actions,
				Candidates: []map[string]interface{}{},
				Error:      fmt.Sprintf("页面被重定向到 %s, 登录态可能已失效, 请重新提供 storage_state", currentURL),
				ElapsedMs:  elapsedMs(started),
			}
		}
	}

	// 8. Find & click button (or use explicit selector)
	var chosen map[string]interface{}
	var candidates []map[string]interface{}

	if req.Selector != "" {
		// Explicit selector mode
		clickCtx, clickCancel := context.WithTimeout(ctx, 10*time.Second)
		err := chromedp.Run(clickCtx, chromedp.Click(req.Selector, chromedp.ByQuery))
		clickCancel()
		if err != nil {
			return CaptureResponse{
				OK:         false,
				Actions:    actions,
				Candidates: []map[string]interface{}{},
				Error:      fmt.Sprintf("用户指定的 selector 点击失败: %v", err),
				ElapsedMs:  elapsedMs(started),
			}
		}
		actions = append(actions, map[string]interface{}{
			"type":     "click",
			"selector": req.Selector,
			"manual":   true,
		})
		chosen = map[string]interface{}{"selector": req.Selector, "text": "(用户指定)"}
		candidates = []map[string]interface{}{}
	} else {
		// Heuristic mode
		var jsResult []interface{}
		evalCtx, evalCancel := context.WithTimeout(ctx, 10*time.Second)
		err := chromedp.Run(evalCtx, chromedp.Evaluate(JSFindCandidates+"()", &jsResult, func(p *runtime.EvaluateParams) *runtime.EvaluateParams {
			return p.WithAwaitPromise(false)
		}))
		evalCancel()
		if err != nil {
			log.Printf("JS candidate evaluation error: %v", err)
		}

		// Convert JS result to ButtonCandidate slice
		rawCandidates := convertJSCandidates(jsResult)
		pickedBtn, top := PickButton(rawCandidates, req.Hint)

		// Convert top to []map for response
		for _, c := range top {
			candidates = append(candidates, candidateToMap(c))
		}
		if candidates == nil {
			candidates = []map[string]interface{}{}
		}

		if pickedBtn == nil {
			return CaptureResponse{
				OK:         false,
				Actions:    actions,
				Candidates: candidates,
				Error:      "未找到匹配的签到按钮, 请检查 hint 或手动指定 selector",
				ElapsedMs:  elapsedMs(started),
			}
		}

		// Human-like delay 100-400 ms
		delay := time.Duration(100+rand.Intn(300)) * time.Millisecond
		time.Sleep(delay)

		// Try clicking by selector, fall back to text
		clickCtx, clickCancel := context.WithTimeout(ctx, 10*time.Second)
		clickErr := chromedp.Run(clickCtx, chromedp.Click(pickedBtn.Selector, chromedp.ByQuery))
		clickCancel()

		if clickErr != nil {
			// Fall back: click by visible text using JS
			fallbackCtx, fallbackCancel := context.WithTimeout(ctx, 10*time.Second)
			fallbackJS := fmt.Sprintf(`
				(() => {
					const text = %q;
					const els = document.querySelectorAll('button,a,[role="button"],input[type="submit"]');
					for (const el of els) {
						if ((el.innerText || el.value || '').toLowerCase().includes(text.toLowerCase())) {
							el.click();
							return true;
						}
					}
					return false;
				})()
			`, pickedBtn.Text)
			var clicked bool
			fallbackErr := chromedp.Run(fallbackCtx, chromedp.Evaluate(fallbackJS, &clicked))
			fallbackCancel()
			if fallbackErr != nil || !clicked {
				actions = append(actions, map[string]interface{}{
					"type":     "click_failed",
					"selector": pickedBtn.Selector,
					"error":    fmt.Sprintf("selector: %v / fallback: %v", clickErr, fallbackErr),
				})
				return CaptureResponse{
					OK:          false,
					Actions:     actions,
					FoundButton: candidateToMap(*pickedBtn),
					Candidates:  candidates,
					Error:       fmt.Sprintf("按钮点击失败: %v", clickErr),
					ElapsedMs:   elapsedMs(started),
				}
			}
		}

		actions = append(actions, map[string]interface{}{
			"type":     "click",
			"selector": pickedBtn.Selector,
			"text":     pickedBtn.Text,
		})
		chosen = candidateToMap(*pickedBtn)
	}

	// 9. Wait after click
	if req.WaitAfterClickMs > 0 {
		time.Sleep(time.Duration(req.WaitAfterClickMs) * time.Millisecond)
	}

	// 10. Wait for network idle after click (up to 5 s, tolerate timeout)
	idleCtx2, idleCancel2 := context.WithTimeout(ctx, 5*time.Second)
	_ = chromedp.Run(idleCtx2, chromedp.WaitReady("body", chromedp.ByQuery))
	idleCancel2()

	// 11. Give body-fetch goroutines a moment to complete
	time.Sleep(500 * time.Millisecond)

	// 12. Build HAR
	harData := recorder.Build()
	harMap, err := structToMap(harData)
	if err != nil {
		log.Printf("HAR serialization error: %v", err)
		harMap = map[string]interface{}{}
	}

	return CaptureResponse{
		OK:          true,
		HAR:         harMap,
		Actions:     actions,
		FoundButton: chosen,
		Candidates:  candidates,
		ElapsedMs:   elapsedMs(started),
	}
}

// injectStorageState sets cookies from a StorageState into the browser.
// storageState is the raw map[string]interface{} from JSON.
func injectStorageState(ctx context.Context, state *StorageState, rawURL string) error {
	cookies := state.Cookies
	if len(cookies) == 0 {
		return nil
	}

	// Build CDP SetCookies params
	for _, c := range cookies {
		expr := network.SetCookie(c.Name, c.Value).
			WithDomain(c.Domain).
			WithPath(c.Path).
			WithHTTPOnly(c.HTTPOnly).
			WithSecure(c.Secure)
		if err := expr.Do(ctx); err != nil {
			log.Printf("Warning: failed to set cookie %q: %v", c.Name, err)
		}
	}

	// Inject localStorage via JS
	for _, origin := range state.Origins {
		if len(origin.LocalStorage) == 0 {
			continue
		}
		for _, item := range origin.LocalStorage {
			k, _ := item["name"].(string)
			v, _ := item["value"].(string)
			if k == "" {
				continue
			}
			js := fmt.Sprintf("localStorage.setItem(%q, %q)", k, v)
			var result interface{}
			if err := chromedp.Run(ctx, chromedp.Evaluate(js, &result)); err != nil {
				log.Printf("Warning: localStorage.setItem(%q) failed: %v", k, err)
			}
		}
	}
	return nil
}

// resolveStorageState converts CaptureRequest's storage_state / cookies fields
// into a sanitized *StorageState, or nil if none provided.
func resolveStorageState(req CaptureRequest) *StorageState {
	var state *StorageState

	if req.StorageState != nil {
		// Convert map[string]interface{} to StorageState via JSON round-trip
		b, err := json.Marshal(req.StorageState)
		if err == nil {
			var ss StorageState
			if err2 := json.Unmarshal(b, &ss); err2 == nil {
				state = &ss
			}
		}
	} else if req.Cookies != "" {
		ss := ParseCookieStrToStorageState(req.Cookies, req.URL)
		state = &ss
	}

	if state != nil {
		sanitized := SanitizeStorageState(*state, req.URL)
		state = &sanitized
	}
	return state
}

// convertJSCandidates converts the raw JS evaluation result ([]interface{}) to []ButtonCandidate.
func convertJSCandidates(raw []interface{}) []ButtonCandidate {
	var out []ButtonCandidate
	for _, item := range raw {
		m, ok := item.(map[string]interface{})
		if !ok {
			continue
		}
		c := ButtonCandidate{
			Text:     strFromMap(m, "text"),
			Tag:      strFromMap(m, "tag"),
			Selector: strFromMap(m, "selector"),
			Quality:  strFromMap(m, "quality"),
			Href:     strFromMap(m, "href"),
		}
		if c.Text != "" && c.Selector != "" {
			out = append(out, c)
		}
	}
	return out
}

func candidateToMap(c ButtonCandidate) map[string]interface{} {
	m := map[string]interface{}{
		"text":     c.Text,
		"tag":      c.Tag,
		"selector": c.Selector,
		"quality":  c.Quality,
		"href":     c.Href,
	}
	if c.Score != 0 {
		m["_score"] = c.Score
	}
	return m
}

func strFromMap(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func structToMap(v interface{}) (map[string]interface{}, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var m map[string]interface{}
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return m, nil
}

func viewportWidth(vp map[string]int) int {
	if vp != nil {
		if w, ok := vp["width"]; ok && w > 0 {
			return w
		}
	}
	return 1280
}

func viewportHeight(vp map[string]int) int {
	if vp != nil {
		if h, ok := vp["height"]; ok && h > 0 {
			return h
		}
	}
	return 800
}
