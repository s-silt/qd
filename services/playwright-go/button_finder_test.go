// button_finder_test.go — unit tests for button_finder.go
// Mirrors services/playwright/test_button_finder.py:TestScore / TestPickButton.
package main

import (
	"strings"
	"testing"
)

// ---- ScoreCandidate tests ----

func TestScoreHighPriorityChinese(t *testing.T) {
	// 立即签到 has more high keywords than 领取 alone
	immediate := ScoreCandidate("立即签到", "")
	claim := ScoreCandidate("领取", "")
	if immediate <= claim {
		t.Errorf("立即签到 (%d) should score higher than 领取 (%d)", immediate, claim)
	}

	daily := ScoreCandidate("每日打卡", "")
	dailyEn := ScoreCandidate("daily", "")
	if daily <= dailyEn {
		t.Errorf("每日打卡 (%d) should score higher than daily (%d)", daily, dailyEn)
	}
}

func TestScoreNegativeLoginButton(t *testing.T) {
	loginScore := ScoreCandidate("登录", "")
	if loginScore >= 0 {
		t.Errorf("登录 should score < 0, got %d", loginScore)
	}

	signupScore := ScoreCandidate("Sign Up", "")
	if signupScore >= 0 {
		t.Errorf("Sign Up should score < 0, got %d", signupScore)
	}
}

func TestScoreHintBoost(t *testing.T) {
	withHint := ScoreCandidate("领取奖品", "领取奖品")
	noHint := ScoreCandidate("领取奖品", "")
	if withHint <= noHint {
		t.Errorf("hint match should boost score: with=%d no=%d", withHint, noHint)
	}
}

func TestScoreEmpty(t *testing.T) {
	if ScoreCandidate("", "") != 0 {
		t.Error("empty text should return 0")
	}
	if ScoreCandidate("   ", "") != 0 {
		t.Error("whitespace-only text should return 0")
	}
}

func TestScoreEnglishSignin(t *testing.T) {
	checkIn := ScoreCandidate("Check In", "")
	if checkIn <= 0 {
		t.Errorf("Check In should score > 0, got %d", checkIn)
	}

	dailySignIn := ScoreCandidate("Daily Sign In", "")
	if dailySignIn <= 0 {
		t.Errorf("Daily Sign In should score > 0, got %d", dailySignIn)
	}
}

func TestScoreHighKeywordsWeight(t *testing.T) {
	// Verify high keywords get +20
	score := ScoreCandidate("签到", "")
	if score != 20 {
		// 签到 appears in KEYWORDS_HIGH list once, expect +20
		// also medium keyword 签 hits +5, so total might be 25
		if score < 20 {
			t.Errorf("签到 should score >= 20 (high keyword), got %d", score)
		}
	}
}

func TestScoreMediumKeywordsWeight(t *testing.T) {
	// "attend" is in KEYWORDS_MEDIUM (+5), not in KEYWORDS_HIGH or KEYWORDS_NEGATIVE
	score := ScoreCandidate("attend", "")
	if score != 5 {
		t.Errorf("attend should score 5, got %d", score)
	}
}

func TestScoreNegativeKeywordsWeight(t *testing.T) {
	// "logout" is in KEYWORDS_NEGATIVE (-30)
	score := ScoreCandidate("logout", "")
	if score >= 0 {
		t.Errorf("logout should score < 0, got %d", score)
	}
}

func TestScoreHintSplitOnPunctuation(t *testing.T) {
	// hint with Chinese comma separators
	score1 := ScoreCandidate("每日签到", "每日签到")
	score2 := ScoreCandidate("每日签到", "每日，签到")
	// both hints contain 签到 so should both boost
	if score1 <= ScoreCandidate("每日签到", "") {
		t.Error("hint '每日签到' should boost score")
	}
	if score2 <= ScoreCandidate("每日签到", "") {
		t.Error("hint '每日，签到' should boost score")
	}
	_ = score1
	_ = score2
}

// ---- PickButton tests ----

func makeCands(texts ...string) []ButtonCandidate {
	cands := make([]ButtonCandidate, len(texts))
	for i, t := range texts {
		cands[i] = ButtonCandidate{
			Text:     t,
			Tag:      "button",
			Selector: "#b" + strings.Repeat("x", i),
			Quality:  "stable",
			Href:     "",
		}
	}
	return cands
}

func TestPickButtonSigninAmongNoise(t *testing.T) {
	cands := makeCands("登录", "退出", "立即签到", "搜索", "首页")
	chosen, top := PickButton(cands, "")
	if chosen == nil {
		t.Fatal("expected chosen button, got nil")
	}
	if chosen.Text != "立即签到" {
		t.Errorf("expected 立即签到, got %q", chosen.Text)
	}
	if len(top) > 10 {
		t.Errorf("top should be at most 10, got %d", len(top))
	}
}

func TestPickButtonNoSigninReturnsNil(t *testing.T) {
	cands := makeCands("登录", "退出", "搜索")
	chosen, top := PickButton(cands, "")
	if chosen != nil {
		t.Errorf("expected nil, got %v", chosen.Text)
	}
	// Should still return the sorted candidates
	if len(top) != 3 {
		t.Errorf("expected 3 top candidates, got %d", len(top))
	}
}

func TestPickButtonHintOverrides(t *testing.T) {
	cands := makeCands("立即签到", "领取奖品", "登录")
	chosen, _ := PickButton(cands, "领取奖品")
	if chosen == nil {
		t.Fatal("expected chosen button, got nil")
	}
	if chosen.Text != "领取奖品" {
		t.Errorf("hint should override, expected '领取奖品', got %q", chosen.Text)
	}
}

func TestPickButtonPriorityOrder(t *testing.T) {
	// 立即签到 should score higher than 打卡 alone
	cands := makeCands("打卡", "立即签到")
	chosen, _ := PickButton(cands, "")
	if chosen == nil {
		t.Fatal("expected chosen button")
	}
	if chosen.Text != "立即签到" {
		t.Errorf("expected 立即签到 > 打卡, got %q", chosen.Text)
	}
}

func TestPickButtonQualityFieldPassesThrough(t *testing.T) {
	cands := []ButtonCandidate{
		{Text: "立即签到", Tag: "button", Selector: `[data-testid="sign"]`, Quality: "stable", Href: ""},
		{Text: "打卡", Tag: "a", Selector: "body > div:nth-of-type(3) > a:nth-of-type(1)", Quality: "fragile", Href: ""},
	}
	chosen, top := PickButton(cands, "")
	if chosen == nil {
		t.Fatal("expected chosen button")
	}
	if chosen.Text != "立即签到" {
		t.Errorf("expected 立即签到, got %q", chosen.Text)
	}
	if chosen.Quality != "stable" {
		t.Errorf("quality should be stable, got %q", chosen.Quality)
	}
	// top should preserve quality
	if len(top) == 0 || top[0].Quality == "" {
		t.Error("top candidates should have quality field")
	}
}

func TestPickButtonEmptyCandidates(t *testing.T) {
	chosen, top := PickButton(nil, "")
	if chosen != nil {
		t.Error("nil candidates should return nil chosen")
	}
	if len(top) != 0 {
		t.Error("nil candidates should return empty top")
	}
}

func TestPickButtonScoreFieldInTop(t *testing.T) {
	cands := makeCands("立即签到", "登录")
	_, top := PickButton(cands, "")
	if len(top) == 0 {
		t.Fatal("expected top candidates")
	}
	// Score should be set
	if top[0].Score == 0 {
		// 立即签到 has score > 0
		t.Errorf("top[0] score should be non-zero, got %d", top[0].Score)
	}
}

// ---- JSFindCandidates static checks ----

func TestJSFindCandidatesDataTestidFirst(t *testing.T) {
	idxTestid := strings.Index(JSFindCandidates, "data-testid")
	idxElId := strings.Index(JSFindCandidates, "el.id")
	if idxTestid < 0 {
		t.Error("JSFindCandidates must reference data-testid")
	}
	if idxElId < 0 {
		t.Error("JSFindCandidates must reference el.id")
	}
	if idxTestid >= idxElId {
		t.Error("data-testid must appear before el.id (priority order)")
	}
}

func TestJSFindCandidatesMaxDepth4(t *testing.T) {
	if !strings.Contains(JSFindCandidates, "MAX_DEPTH = 4") {
		t.Error("JSFindCandidates must define MAX_DEPTH = 4")
	}
}

func TestJSFindCandidatesQualityField(t *testing.T) {
	if !strings.Contains(JSFindCandidates, "quality") {
		t.Error("JSFindCandidates must emit quality field")
	}
	if !strings.Contains(JSFindCandidates, "'stable'") {
		t.Error("JSFindCandidates must use 'stable' quality value")
	}
	if !strings.Contains(JSFindCandidates, "'fragile'") {
		t.Error("JSFindCandidates must use 'fragile' quality value")
	}
}
