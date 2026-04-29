// security_test.go — unit tests for security.go
// Mirrors services/playwright/test_button_finder.py::TestParseCookieStr
// and TestSanitizeStorageState.
package main

import (
	"testing"
)

// ---- ParseCookieStrToStorageState tests ----

func TestParseCookieStrBasic(t *testing.T) {
	st := ParseCookieStrToStorageState("session=abc; token=xyz", "https://example.com/sign")
	if len(st.Cookies) != 2 {
		t.Fatalf("expected 2 cookies, got %d", len(st.Cookies))
	}
	names := map[string]bool{}
	for _, c := range st.Cookies {
		names[c.Name] = true
	}
	if !names["session"] || !names["token"] {
		t.Errorf("expected session and token, got %v", names)
	}
	for _, c := range st.Cookies {
		if c.Domain != ".example.com" {
			t.Errorf("expected domain .example.com, got %q", c.Domain)
		}
		if !c.Secure {
			t.Errorf("expected secure=true for https URL")
		}
		if c.Path != "/" {
			t.Errorf("expected path /, got %q", c.Path)
		}
		if c.SameSite != "Lax" {
			t.Errorf("expected SameSite=Lax, got %q", c.SameSite)
		}
	}
}

func TestParseCookieStrEmptyPairsIgnored(t *testing.T) {
	st := ParseCookieStrToStorageState(";; key=val ; ;= ;", "http://x.com/")
	if len(st.Cookies) != 1 {
		t.Fatalf("expected 1 cookie, got %d", len(st.Cookies))
	}
	if st.Cookies[0].Name != "key" {
		t.Errorf("expected name=key, got %q", st.Cookies[0].Name)
	}
	// http URL → secure=false
	if st.Cookies[0].Secure {
		t.Errorf("expected secure=false for http URL")
	}
}

func TestParseCookieStrHTTP(t *testing.T) {
	st := ParseCookieStrToStorageState("a=1", "http://test.example.org/path")
	if len(st.Cookies) != 1 {
		t.Fatalf("expected 1 cookie, got %d", len(st.Cookies))
	}
	if st.Cookies[0].Secure {
		t.Errorf("http URL must not set secure=true")
	}
	if st.Cookies[0].Domain != ".test.example.org" {
		t.Errorf("unexpected domain: %q", st.Cookies[0].Domain)
	}
}

func TestParseCookieStrEmptyOrigins(t *testing.T) {
	st := ParseCookieStrToStorageState("x=1", "https://foo.com/")
	if st.Origins == nil {
		t.Error("Origins should not be nil")
	}
	if len(st.Origins) != 0 {
		t.Errorf("expected empty origins, got %d", len(st.Origins))
	}
}

// ---- DomainMatches tests ----

func TestDomainMatchesSameDomain(t *testing.T) {
	if !DomainMatches(".example.com", "example.com") {
		t.Error(".example.com should match example.com")
	}
}

func TestDomainMatchesSubdomain(t *testing.T) {
	if !DomainMatches(".example.com", "api.example.com") {
		t.Error(".example.com should match api.example.com")
	}
}

func TestDomainMatchesNoSubstringAttack(t *testing.T) {
	// "notexample.com" must NOT match ".example.com"
	if DomainMatches(".example.com", "notexample.com") {
		t.Error(".example.com must NOT match notexample.com (substring attack)")
	}
}

func TestDomainMatchesEmpty(t *testing.T) {
	if DomainMatches("", "example.com") {
		t.Error("empty cookie domain must not match")
	}
	if DomainMatches(".example.com", "") {
		t.Error("empty request host must not match")
	}
}

// ---- SanitizeStorageState tests ----

func TestSanitizeDropsUnrelatedCookies(t *testing.T) {
	state := StorageState{
		Cookies: []CookieEntry{
			{Name: "a", Value: "1", Domain: ".example.com"},
			{Name: "b", Value: "2", Domain: ".attacker.com"},
			// sub.example.com cookie should NOT match parent-domain request
			{Name: "c", Value: "3", Domain: "sub.example.com"},
		},
		Origins: []OriginEntry{},
	}
	out := SanitizeStorageState(state, "https://example.com/sign")
	if len(out.Cookies) != 1 {
		t.Fatalf("expected 1 cookie after sanitize, got %d (%v)", len(out.Cookies), cookieNames(out.Cookies))
	}
	if out.Cookies[0].Name != "a" {
		t.Errorf("expected cookie 'a' to survive, got %q", out.Cookies[0].Name)
	}
}

func TestSanitizeParentCookieAppliesToSubdomain(t *testing.T) {
	// .example.com cookie should match api.example.com request
	state := StorageState{
		Cookies: []CookieEntry{
			{Name: "a", Value: "1", Domain: ".example.com"},
		},
		Origins: []OriginEntry{},
	}
	out := SanitizeStorageState(state, "https://api.example.com/")
	if len(out.Cookies) != 1 {
		t.Fatalf("expected 1 cookie, got %d", len(out.Cookies))
	}
}

func TestSanitizeDropsUnrelatedOrigins(t *testing.T) {
	state := StorageState{
		Cookies: []CookieEntry{},
		Origins: []OriginEntry{
			{Origin: "https://example.com", LocalStorage: nil},
			{Origin: "https://evil.com", LocalStorage: nil},
		},
	}
	out := SanitizeStorageState(state, "https://example.com/")
	if len(out.Origins) != 1 {
		t.Fatalf("expected 1 origin, got %d", len(out.Origins))
	}
	if out.Origins[0].Origin != "https://example.com" {
		t.Errorf("unexpected origin: %q", out.Origins[0].Origin)
	}
}

func TestSanitizeSubdomainMatch(t *testing.T) {
	// sub.example.com request, cookie domain=.example.com should be kept
	state := StorageState{
		Cookies: []CookieEntry{{Name: "a", Value: "1", Domain: ".example.com"}},
		Origins: []OriginEntry{},
	}
	out := SanitizeStorageState(state, "https://sub.example.com/")
	if len(out.Cookies) != 1 {
		t.Fatalf("expected 1 cookie, got %d", len(out.Cookies))
	}
}

func TestSanitizeNoMatchURL(t *testing.T) {
	// URL has no hostname (file://) → no cookies allowed
	state := StorageState{
		Cookies: []CookieEntry{{Name: "a", Value: "1", Domain: ".example.com"}},
		Origins: []OriginEntry{},
	}
	out := SanitizeStorageState(state, "file:///etc/passwd")
	if len(out.Cookies) != 0 {
		t.Errorf("expected 0 cookies for file:// URL, got %d", len(out.Cookies))
	}
}

func TestSanitizeAttackerDomainSubstring(t *testing.T) {
	// .notexample.com must NOT match example.com (substring attack defense)
	state := StorageState{
		Cookies: []CookieEntry{
			{Name: "a", Value: "1", Domain: ".notexample.com"},
		},
		Origins: []OriginEntry{},
	}
	out := SanitizeStorageState(state, "https://example.com/")
	if len(out.Cookies) != 0 {
		t.Errorf("expected 0 cookies (substring attack defended), got %d", len(out.Cookies))
	}
}

func TestSanitizePreservesNilSlices(t *testing.T) {
	// Empty input → empty (not nil) slices
	state := StorageState{
		Cookies: nil,
		Origins: nil,
	}
	out := SanitizeStorageState(state, "https://example.com/")
	if out.Cookies == nil {
		t.Error("Cookies must not be nil after sanitize")
	}
	if out.Origins == nil {
		t.Error("Origins must not be nil after sanitize")
	}
}

// ---- Helpers ----

func cookieNames(cs []CookieEntry) []string {
	names := make([]string, len(cs))
	for i, c := range cs {
		names[i] = c.Name
	}
	return names
}
