// Package main — security.go
// Replicates services/playwright/security.py:
//   - ParseCookieStrToStorageState
//   - DomainMatches
//   - SanitizeStorageState
package main

import (
	"log"
	"net/url"
	"strings"
)

// StorageState mirrors Playwright's storage_state JSON structure.
type StorageState struct {
	Cookies []CookieEntry  `json:"cookies"`
	Origins []OriginEntry  `json:"origins"`
}

// CookieEntry is a single cookie in a storage_state.
type CookieEntry struct {
	Name     string `json:"name"`
	Value    string `json:"value"`
	Domain   string `json:"domain"`
	Path     string `json:"path"`
	HTTPOnly bool   `json:"httpOnly"`
	Secure   bool   `json:"secure"`
	SameSite string `json:"sameSite"`
}

// OriginEntry holds localStorage for a given origin.
type OriginEntry struct {
	Origin       string                   `json:"origin"`
	LocalStorage []map[string]interface{} `json:"localStorage"`
}

// ParseCookieStrToStorageState converts a "k1=v1; k2=v2" cookie string into
// a StorageState. Replicates security.py:parse_cookie_str_to_storage_state.
func ParseCookieStrToStorageState(cookieStr, rawURL string) StorageState {
	parsed, err := url.Parse(rawURL)
	domain := ""
	isHTTPS := false
	if err == nil {
		domain = parsed.Hostname()
		isHTTPS = parsed.Scheme == "https"
	}
	cookieDomain := domain
	if cookieDomain != "" && !strings.HasPrefix(cookieDomain, ".") {
		cookieDomain = "." + cookieDomain
	}

	var cookies []CookieEntry
	for _, part := range strings.Split(cookieStr, ";") {
		part = strings.TrimSpace(part)
		if part == "" || !strings.Contains(part, "=") {
			continue
		}
		idx := strings.Index(part, "=")
		name := strings.TrimSpace(part[:idx])
		value := strings.TrimSpace(part[idx+1:])
		if name == "" {
			continue
		}
		cookies = append(cookies, CookieEntry{
			Name:     name,
			Value:    value,
			Domain:   cookieDomain,
			Path:     "/",
			HTTPOnly: false,
			Secure:   isHTTPS,
			SameSite: "Lax",
		})
	}
	if cookies == nil {
		cookies = []CookieEntry{}
	}
	return StorageState{
		Cookies: cookies,
		Origins: []OriginEntry{},
	}
}

// DomainMatches reports whether cookieDomain applies to requestHost.
// Replicates security.py:domain_matches.
// e.g. ".example.com" matches "api.example.com" and "example.com",
// but NOT "notexample.com".
func DomainMatches(cookieDomain, requestHost string) bool {
	if cookieDomain == "" || requestHost == "" {
		return false
	}
	cd := strings.TrimLeft(strings.ToLower(cookieDomain), ".")
	rh := strings.ToLower(requestHost)
	return rh == cd || strings.HasSuffix(rh, "."+cd)
}

// SanitizeStorageState removes cookies/origins whose domain doesn't match url.
// Replicates security.py:sanitize_storage_state.
func SanitizeStorageState(state StorageState, rawURL string) StorageState {
	parsed, err := url.Parse(rawURL)
	requestHost := ""
	if err == nil {
		requestHost = strings.ToLower(parsed.Hostname())
	}

	var safeCookies []CookieEntry
	var droppedCookies []string
	for _, c := range state.Cookies {
		if DomainMatches(c.Domain, requestHost) {
			safeCookies = append(safeCookies, c)
		} else {
			droppedCookies = append(droppedCookies, c.Domain)
		}
	}

	var safeOrigins []OriginEntry
	var droppedOrigins []string
	for _, o := range state.Origins {
		originHost := ""
		if op, err2 := url.Parse(strings.ToLower(o.Origin)); err2 == nil {
			originHost = op.Hostname()
		}
		if originHost != "" && DomainMatches(originHost, requestHost) {
			safeOrigins = append(safeOrigins, o)
		} else {
			droppedOrigins = append(droppedOrigins, o.Origin)
		}
	}

	if len(droppedCookies) > 0 || len(droppedOrigins) > 0 {
		log.Printf("[security] storage_state 剔除了与 %s 不匹配的 cookies=%v origins=%v",
			requestHost, droppedCookies, droppedOrigins)
	}

	if safeCookies == nil {
		safeCookies = []CookieEntry{}
	}
	if safeOrigins == nil {
		safeOrigins = []OriginEntry{}
	}
	return StorageState{
		Cookies: safeCookies,
		Origins: safeOrigins,
	}
}
