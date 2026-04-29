"""签到按钮启发式查找。

通过 DOM 文本匹配常见签到关键字, 不调用 LLM。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# 关键字按权重降序: 越靠前权重越高
# 中文优先, 英文次之
KEYWORDS_HIGH = [
    "立即签到", "每日签到", "每日打卡", "签到领取",
    "签到", "打卡", "领取奖励",
    "立即领取", "每日领取", "今日签到", "去签到",
]
KEYWORDS_MEDIUM = [
    "领取", "签", "打", "check in", "check-in", "checkin",
    "sign in", "sign-in", "signin", "daily", "attend", "claim",
    "punch", "clock in",
]
# 反关键字: 出现这些词的按钮优先级降低（避免点到登录、退出等）
KEYWORDS_NEGATIVE = [
    "登录", "登出", "退出", "注销", "注册", "log in", "log out",
    "login", "logout", "register", "sign up", "signup",
]

# 找候选按钮的 JS 脚本: 在浏览器内执行
JS_FIND_CANDIDATES = """
() => {
    const candidates = [];
    const seen = new Set();
    const selectors = ['button', 'a', '[role="button"]', 'input[type="submit"]', 'input[type="button"]'];
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
            // 给元素一个稳定 selector 标识, 优先用 id 否则按结构定位
            let path = '';
            if (el.id) {
                path = '#' + CSS.escape(el.id);
            } else {
                // 走 nth-of-type 层级路径, 控制深度避免无限长
                let cur = el;
                const parts = [];
                for (let depth = 0; depth < 6 && cur && cur.nodeType === 1 && cur.tagName !== 'HTML'; depth++) {
                    const tag = cur.tagName.toLowerCase();
                    let nth = 1;
                    let sib = cur.previousElementSibling;
                    while (sib) {
                        if (sib.tagName === cur.tagName) nth++;
                        sib = sib.previousElementSibling;
                    }
                    parts.unshift(`${tag}:nth-of-type(${nth})`);
                    cur = cur.parentElement;
                }
                path = parts.join(' > ');
            }
            candidates.push({
                text: text.slice(0, 100),
                tag: el.tagName.toLowerCase(),
                selector: path,
                href: el.tagName === 'A' ? (el.href || '') : '',
            });
        }
    }
    return candidates;
}
"""


def score_candidate(text: str, hint: str = "") -> int:
    """给候选按钮文本打分, 高分更可能是签到按钮。"""
    if not text:
        return 0
    t = text.lower()
    score = 0

    # 用户提示词命中加 +50
    if hint:
        for word in re.split(r"[\s,，、]+", hint.lower()):
            if word and word in t:
                score += 50

    # 高权重关键字 +20
    for kw in KEYWORDS_HIGH:
        if kw in text or kw.lower() in t:
            score += 20

    # 中权重 +5
    for kw in KEYWORDS_MEDIUM:
        if kw in text or kw.lower() in t:
            score += 5

    # 反关键字 -30
    for kw in KEYWORDS_NEGATIVE:
        if kw in text or kw.lower() in t:
            score -= 30

    return score


def pick_button(candidates: List[dict], hint: str = "") -> Tuple[Optional[dict], List[dict]]:
    """从候选中挑分数最高且 > 0 的; 返回 (选中, 排序后的全部 top 10)。"""
    scored = [
        (score_candidate(c["text"], hint), c)
        for c in candidates
    ]
    scored.sort(key=lambda x: -x[0])
    top = [
        {**c, "_score": s}
        for s, c in scored[:10]
    ]
    if scored and scored[0][0] > 0:
        return scored[0][1], top
    return None, top
