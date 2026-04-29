// Package main — button_finder.go
// Replicates services/playwright/button_finder.py:
//   - ScoreCandidate  (keyword scoring)
//   - PickButton      (pick highest-score > 0)
//   - JSFindCandidates (the embedded JS run inside the browser)
package main

import (
	"regexp"
	"strings"
)

// Keyword lists — must match button_finder.py exactly.

var keywordsHigh = []string{
	"立即签到", "每日签到", "每日打卡", "签到领取",
	"签到", "打卡", "领取奖励",
	"立即领取", "每日领取", "今日签到", "去签到",
}

var keywordsMedium = []string{
	"领取", "签", "打", "check in", "check-in", "checkin",
	"sign in", "sign-in", "signin", "daily", "attend", "claim",
	"punch", "clock in",
}

var keywordsNegative = []string{
	"登录", "登出", "退出", "注销", "注册", "log in", "log out",
	"login", "logout", "register", "sign up", "signup",
}

// hintSplitRe splits hint on whitespace/CJK punctuation, matching Python re.split.
var hintSplitRe = regexp.MustCompile(`[\s,，、]+`)

// ScoreCandidate scores a button text. Replicates button_finder.py:score_candidate.
func ScoreCandidate(text, hint string) int {
	if strings.TrimSpace(text) == "" {
		return 0
	}
	t := strings.ToLower(text)
	score := 0

	// hint words: +50 each
	if hint != "" {
		for _, word := range hintSplitRe.Split(strings.ToLower(hint), -1) {
			if word != "" && strings.Contains(t, word) {
				score += 50
			}
		}
	}

	// high keywords: +20 each
	for _, kw := range keywordsHigh {
		if strings.Contains(text, kw) || strings.Contains(t, strings.ToLower(kw)) {
			score += 20
		}
	}

	// medium keywords: +5 each
	for _, kw := range keywordsMedium {
		if strings.Contains(text, kw) || strings.Contains(t, strings.ToLower(kw)) {
			score += 5
		}
	}

	// negative keywords: -30 each
	for _, kw := range keywordsNegative {
		if strings.Contains(text, kw) || strings.Contains(t, strings.ToLower(kw)) {
			score -= 30
		}
	}

	return score
}

// ButtonCandidate represents one clickable element found by JSFindCandidates.
type ButtonCandidate struct {
	Text     string `json:"text"`
	Tag      string `json:"tag"`
	Selector string `json:"selector"`
	Quality  string `json:"quality"`
	Href     string `json:"href"`
	Score    int    `json:"_score,omitempty"`
}

// PickButton picks the candidate with the highest score > 0.
// Returns (chosen, top10_sorted). Replicates button_finder.py:pick_button.
func PickButton(candidates []ButtonCandidate, hint string) (*ButtonCandidate, []ButtonCandidate) {
	type scored struct {
		score int
		c     ButtonCandidate
	}
	var list []scored
	for _, c := range candidates {
		list = append(list, scored{ScoreCandidate(c.Text, hint), c})
	}
	// sort descending
	for i := 0; i < len(list); i++ {
		for j := i + 1; j < len(list); j++ {
			if list[j].score > list[i].score {
				list[i], list[j] = list[j], list[i]
			}
		}
	}
	// top 10
	end := len(list)
	if end > 10 {
		end = 10
	}
	top := make([]ButtonCandidate, end)
	for i, s := range list[:end] {
		top[i] = s.c
		top[i].Score = s.score
	}
	if len(list) > 0 && list[0].score > 0 {
		chosen := list[0].c
		return &chosen, top
	}
	return nil, top
}

// JSFindCandidates is the JS snippet run inside the browser page via chromedp.Evaluate.
// Directly mirrors button_finder.py:JS_FIND_CANDIDATES.
const JSFindCandidates = `
() => {
    const MAX_DEPTH = 4;
    const candidates = [];
    const seen = new Set();
    const selectors = ['button', 'a', '[role="button"]', 'input[type="submit"]', 'input[type="button"]'];
    function buildSelector(el) {
        const testid = el.getAttribute && el.getAttribute('data-testid');
        if (testid) return ` + "`" + `[data-testid="${CSS.escape(testid)}"]` + "`" + `;
        if (el.id) return '#' + CSS.escape(el.id);
        const name = el.getAttribute && el.getAttribute('name');
        if (name && (el.tagName === 'INPUT' || el.tagName === 'BUTTON')) {
            return ` + "`" + `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]` + "`" + `;
        }
        // 兜底: nth-of-type 路径, 限制 4 层
        let cur = el;
        const parts = [];
        for (let depth = 0; depth < MAX_DEPTH && cur && cur.nodeType === 1 && cur.tagName !== 'HTML'; depth++) {
            const tag = cur.tagName.toLowerCase();
            let nth = 1;
            let sib = cur.previousElementSibling;
            while (sib) {
                if (sib.tagName === cur.tagName) nth++;
                sib = sib.previousElementSibling;
            }
            parts.unshift(` + "`" + `${tag}:nth-of-type(${nth})` + "`" + `);
            cur = cur.parentElement;
        }
        return parts.join(' > ');
    }
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);
            const rect = el.getBoundingClientRect();
            const visible = rect.width > 0 && rect.height > 0 &&
                window.getComputedStyle(el).visibility !== 'hidden' &&
                window.getComputedStyle(el).display !== 'none';
            if (!visible) continue;
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            if (!text) continue;
            const path = buildSelector(el);
            // 标记 selector 稳定性, 让 UI 提示用户
            let quality = 'fragile';
            if (path.startsWith('[data-testid=')) quality = 'stable';
            else if (path.startsWith('#')) quality = 'stable';
            else if (path.includes('[name=')) quality = 'medium';
            candidates.push({
                text: text.slice(0, 100),
                tag: el.tagName.toLowerCase(),
                selector: path,
                quality: quality,
                href: el.tagName === 'A' ? (el.href || '') : '',
            });
        }
    }
    return candidates;
}
`
