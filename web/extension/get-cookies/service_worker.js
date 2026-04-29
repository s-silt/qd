'use strict';

/*
 * QD Cookies Helper - service worker (Manifest V3)
 *
 * Listens for "get_cookie" messages from the QD page (relayed via the content
 * script) and replies with the full cookie set for the requested target site.
 *
 * Differences vs. upstream qd-today/get-cookies:
 *   - host allowlist is matched by parsed URL.hostname (exact + suffix), not
 *     substring includes(): "evil-qiandao.today" no longer matches "qiandao.today".
 *   - one-shot chrome.runtime.onMessage instead of long-lived ports.
 *   - chrome.scripting.executeScript guarded by frameIds:[0] (top frame only).
 *   - injection runs only after the tab finishes loading the matched origin,
 *     not on every URL change.
 */

const STORAGE_KEY = 'qdHosts';

let allowedHostnames = new Set();
let allowedOrigins = new Set();

function parseHostList(raw) {
  const hosts = new Set();
  const origins = new Set();
  if (!raw) return { hosts, origins };
  for (const line of String(raw).split(/\s+/)) {
    if (!line) continue;
    const candidate = /^https?:\/\//i.test(line) ? line : `http://${line}`;
    try {
      const u = new URL(candidate);
      hosts.add(u.hostname.toLowerCase());
      origins.add(u.origin.toLowerCase());
    } catch {
      // ignore malformed entries
    }
  }
  return { hosts, origins };
}

function urlMatchesAllowlist(url) {
  if (!url) return false;
  let hostname;
  try {
    hostname = new URL(url).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (allowedHostnames.has(hostname)) return true;
  for (const allowed of allowedHostnames) {
    if (hostname.endsWith('.' + allowed)) return true;
  }
  return false;
}

async function refreshAllowlist() {
  const { [STORAGE_KEY]: raw } = await chrome.storage.sync.get(STORAGE_KEY);
  const { hosts, origins } = parseHostList(raw);
  allowedHostnames = hosts;
  allowedOrigins = origins;
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes[STORAGE_KEY]) refreshAllowlist();
});

chrome.runtime.onInstalled.addListener(refreshAllowlist);
chrome.runtime.onStartup.addListener(refreshAllowlist);
refreshAllowlist();

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== 'complete') return;
  if (!tab.url || !urlMatchesAllowlist(tab.url)) return;
  chrome.scripting.executeScript({
    target: { tabId, frameIds: [0] },
    files: ['content.js'],
  }).catch(() => {
    // expected on chrome:// / file:// / cross-origin frames
  });
});

async function getCookiesForSite(site) {
  // Pull both by URL (covers path-restricted cookies) and by domain
  // (gets cookies set on parent domains).
  const out = {};
  try {
    const byUrl = await chrome.cookies.getAll({ url: site });
    for (const c of byUrl) out[c.name] = c.value;
  } catch {
    // ignore
  }
  try {
    const host = new URL(site).hostname;
    const byDomain = await chrome.cookies.getAll({ domain: host });
    for (const c of byDomain) {
      if (!(c.name in out)) out[c.name] = c.value;
    }
  } catch {
    // invalid URL
  }
  return out;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.do !== 'get_cookie') return false;
  if (!sender.tab || !urlMatchesAllowlist(sender.tab.url)) {
    sendResponse({ error: 'origin_not_allowed' });
    return false;
  }
  const site = msg.site;
  if (!site) {
    sendResponse({ error: 'missing_site' });
    return false;
  }
  getCookiesForSite(site).then((cookies) => sendResponse({ cookies }));
  return true; // keep channel open for async sendResponse
});
