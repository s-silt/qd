'use strict';

/*
 * QD Cookies Helper - content script
 *
 * Injected into pages that match the QD allowlist. Marks the page as
 * "extension is here" and forwards click events on `[data-toggle=get-cookie]`
 * elements to the service worker, then posts the resulting cookie bundle back
 * to the page via window.postMessage.
 *
 * Wire format expected by QD's frontend (web/tpl/utils.html, web/static/har/editor.js):
 *   { info: 'get-cookieModReady' }            // sent on load
 *   { info: 'cookieRaw', data: { name: value } } // sent after a successful click
 *
 * Differences vs. upstream:
 *   - postMessage uses window.location.origin instead of '*' (matches QD's
 *     event.origin === window.location.origin check).
 *   - guards against extension-context invalidation (e.g. after reload).
 *   - exits early if the script ever runs twice in the same page.
 */

(function () {
  if (window.__qdGetCookieInjected) return;
  window.__qdGetCookieInjected = true;

  const SELF_ORIGIN = window.location.origin;
  const READY_MESSAGE = { info: 'get-cookieModReady' };

  function announceReady() {
    document.body && document.body.setAttribute('get-cookie', 'true');
    window.postMessage(READY_MESSAGE, SELF_ORIGIN);
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
      fn();
    }
  }

  function readBtnSite(btn) {
    return (
      btn.getAttribute('data-site') ||
      btn.getAttribute('data-url') ||
      ''
    );
  }

  async function handleExportClick(btn) {
    const site = readBtnSite(btn);
    if (!site) {
      console.warn('[QD-cookies] data-site missing on button');
      return;
    }
    const human = site.replace(/^https?:\/\//, '');
    if (!window.confirm(
      `确定从「${human}」获取 cookie 并发送给当前页面（${SELF_ORIGIN}）？`
    )) return;

    let response;
    try {
      response = await chrome.runtime.sendMessage({ do: 'get_cookie', site });
    } catch (err) {
      console.warn('[QD-cookies] extension context invalidated, please reload.', err);
      return;
    }
    if (!response) return;
    if (response.error) {
      console.warn('[QD-cookies] error:', response.error);
      return;
    }
    window.postMessage(
      { info: 'cookieRaw', data: response.cookies || {} },
      SELF_ORIGIN
    );
  }

  ready(() => {
    announceReady();
    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest && ev.target.closest('[data-toggle=get-cookie]');
      if (!btn) return;
      handleExportClick(btn);
    }, false);
  });
})();
