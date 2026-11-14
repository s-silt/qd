(() => {
  'use strict';

  const el = (id) => document.getElementById(id);
  const workspace = el('workbench');
  const task = Number(workspace.dataset.task) || null;
  const template = Number(workspace.dataset.template) || null;
  const base = '/ai-workbench';
  const storageKey = `qd-workbench-${template ? `template-${template}` : task || 'new'}`;
  const chat = el('chat-scroll');
  const welcome = Array.from(chat.children, (node) => node.cloneNode(true));
  const draftPanel = el('draft-panel');
  const workflowSteps = Array.from(workspace.querySelectorAll('[data-phase]'));
  const image = el('browser-frame');
  const screen = el('browser-screen');
  const browser = el('browser-panel');
  const history = [];
  let session = null;
  let frameURL;
  let tabState = '';
  let moving;
  let inputQueue = Promise.resolve();
  let pointer = null;
  let busy = false;
  let recording = false;
  let recordingStatus = '';
  let needsVerification = false;
  let chatBusy = false;
  let frameTimer;
  let pendingFrame;
  let readyPhase = 'open';

  async function api(path, data, keepalive = false) {
    const response = await fetch(`${base}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-QD-Workbench': '1' },
      body: JSON.stringify(data),
      keepalive,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const failure = new Error(result.error || (response.status === 404 ? '浏览器会话已关闭' : '操作失败，请重试'));
      failure.status = response.status;
      failure.verification = result.verification === true;
      throw failure;
    }
    return result;
  }

  function sessionRequest(id, action, data, keepalive = false) {
    return api(`/browser/${id}/${action}`, data, keepalive);
  }

  function addMessage(text, type = 'assistant') {
    const row = document.createElement('div');
    row.className = `message ${type}`;
    const avatar = document.createElement('span');
    avatar.className = 'avatar';
    avatar.setAttribute('aria-hidden', 'true');
    const glyph = document.createElement('i');
    glyph.className = 'fa fa-bolt';
    avatar.append(glyph);
    const bubble = document.createElement('div');
    bubble.className = 'message-content';
    const message = document.createElement('p');
    message.textContent = text;
    bubble.append(message);
    row.append(avatar, bubble);
    chat.append(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }

  function error(text = '') {
    el('browser-error').textContent = text;
  }

  function status(text, announce = false) {
    const statusText = el('browser-status');
    if (statusText.textContent !== text) statusText.textContent = text;
    const liveRegion = el('browser-announcement');
    if (announce && liveRegion.textContent !== text) liveRegion.textContent = text;
  }

  function setPhase(phase) {
    if (readyPhase === phase) return;
    readyPhase = phase;
    const selected = workflowSteps.findIndex((step) => step.dataset.phase === phase);
    for (const [index, step] of workflowSteps.entries()) {
      step.classList.toggle('complete', index < selected);
      if (index === selected) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    }
  }

  function clearDraft() {
    draftPanel.replaceChildren();
    draftPanel.hidden = true;
  }

  function stored(value) {
    try {
      if (value !== undefined) {
        if (value) sessionStorage.setItem(storageKey, value);
        else sessionStorage.removeItem(storageKey);
      }
      return sessionStorage.getItem(storageKey);
    } catch (_) {
      return null;
    }
  }

  function setControls() {
    const active = Boolean(session);
    el('browser-live').hidden = !active;
    el('browser-empty').hidden = active;
    el('browser-actions').hidden = !active;
    el('browser-close').hidden = !active;
    el('browser-close').disabled = busy;
    el('browser-expand').hidden = !active || !browser.requestFullscreen;
    if (task || template) {
      el('recovery-open').disabled = active || busy || !workspace.dataset.browser;
      if (task) {
        el('resume-task').disabled = busy;
      } else {
        el('template-test').disabled = busy;
        el('template-task').hidden = !active;
        for (const button of el('template-task').querySelectorAll('button')) button.disabled = busy;
      }
    } else {
      el('site-open').disabled = busy || recording || !workspace.dataset.browser;
      el('browser-back').disabled = !active || busy || recording;
      el('browser-refresh').disabled = !active || busy || recording;
      el('record-auto').hidden = recording;
      el('record-manual').hidden = recording;
      el('record-finish').hidden = !recording;
      el('record-cancel').hidden = !recording;
      for (const control of ['record-auto', 'record-manual', 'record-finish', 'record-retry', 'record-cancel']) {
        el(control).disabled = busy;
      }
      for (const button of draftPanel.querySelectorAll('button')) button.disabled = busy || recording;
    }
  }

  function renderTabs(raw, active) {
    const signature = `${active}:${raw}`;
    if (tabState === signature) return;
    tabState = signature;
    const container = el('browser-tabs');
    const tabs = JSON.parse(raw || '[]');
    container.replaceChildren();
    if (tabs.length < 2) return;
    tabs.forEach((tab, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tab';
      button.textContent = tab;
      button.title = tab;
      button.setAttribute('aria-selected', String(index === Number(active)));
      button.addEventListener('click', () => sendInput({ kind: 'tab', index }));
      container.append(button);
    });
  }

  async function frames(id) {
    if (session !== id) return;
    const controller = new AbortController();
    pendingFrame = controller;
    try {
      const response = await fetch(`${base}/browser/${id}/frame`, { cache: 'no-store', signal: controller.signal });
      if (session !== id) return;
      if (response.status === 404 || response.redirected) {
        resetBrowser();
        error('浏览器会话已过期，请重新打开网站。');
        return;
      }
      if (response.ok && response.status !== 204) {
        const frame = await response.blob();
        if (session !== id) return;
        if (frameURL) URL.revokeObjectURL(frameURL);
        frameURL = URL.createObjectURL(frame);
        image.src = frameURL;
        renderTabs(response.headers.get('X-QD-Tabs'), response.headers.get('X-QD-Active-Tab'));
        if (!busy) {
          const remoteRecording = response.headers.get('X-QD-Recording') === '1';
          if (remoteRecording !== recording) {
            recording = remoteRecording;
            if (!recording) recordingStatus = '';
            setControls();
          }
          const pendingVerification = response.headers.get('X-QD-Verification') === '1';
          const verificationChanged = needsVerification !== pendingVerification;
          needsVerification = pendingVerification;
          if (needsVerification) {
            status('站点要求登录或验证，请在当前页面完成', verificationChanged);
          } else if (recording) {
            status(recordingStatus || '正在录制签到');
          } else if (verificationChanged || readyPhase === 'record') {
            status(response.headers.get('X-QD-Site') || '浏览器已打开', verificationChanged);
          }
        }
      }
    } catch (_) {
      if (session === id) status('正在重新连接浏览器…');
    }
    if (pendingFrame === controller) pendingFrame = undefined;
    if (session === id) frameTimer = window.setTimeout(() => frames(id), document.hidden ? 1800 : 300);
  }

  function resetBrowser() {
    session = null;
    clearTimeout(frameTimer);
    pendingFrame?.abort();
    pendingFrame = undefined;
    clearDraft();
    setPhase('open');
    needsVerification = false;
    stored(null);
    recording = false;
    recordingStatus = '';
    tabState = '';
    pointer = null;
    el('browser-tabs').replaceChildren();
    el('type-input').value = '';
    el('type-form').hidden = true;
    el('keyboard-toggle').setAttribute('aria-expanded', 'false');
    el('record-retry')?.setAttribute('hidden', '');
    document.querySelector('.draft-card')?.remove();
    image.removeAttribute('src');
    if (frameURL) URL.revokeObjectURL(frameURL);
    frameURL = undefined;
    status('等待打开网站');
    el('browser-announcement').textContent = '';
    setControls();
  }

  async function browserOperation(operation) {
    if (busy) return;
    busy = true;
    error();
    screen.setAttribute('aria-busy', 'true');
    setControls();
    try {
      await inputQueue;
      await operation();
    } catch (failure) {
      error(failure.message);
    } finally {
      busy = false;
      screen.removeAttribute('aria-busy');
      setControls();
    }
  }

  function openSite(url) {
    if (!workspace.dataset.browser) {
      error('在线浏览器尚未启用。请查看浏览器配置说明。');
      return Promise.resolve();
    }
    return browserOperation(async () => {
      status('正在打开网站…');
      if (session) {
        await sessionRequest(session, 'navigate', { url });
      } else {
        const result = await api('/browser', task ? { task } : template ? { template } : { url });
        session = result.session;
        stored(session);
        frames(session);
        if (task) addMessage('请在浏览器完成验证，然后点击「验证完成，恢复任务」。');
        else if (template) addMessage('登录站点后，可试跑脚本、定时或立即签到。');
        else addMessage('网站已打开。登录并进入签到页面后，点击「已登录，录制签到」。');
      }
      clearDraft();
      setPhase('record');
      status('网站已打开。请在浏览器完成登录。', true);
      screen.focus({ preventScroll: true });
      browser.scrollIntoView({ block: 'nearest' });
    }).finally(() => {
      if (!session) status('等待打开网站');
    });
  }

  function sendInput(data) {
    if (!session || busy) return;
    const id = session;
    const pending = inputQueue;
    inputQueue = (async () => {
      await pending;
      if (id !== session) return;
      try {
        const result = await sessionRequest(id, 'input', data);
        if (recording && result.recording === false) {
          recording = false;
          setControls();
          addMessage('已停止录制。完成验证后可以重新录制签到。');
        }
      } catch (failure) {
        error(failure.message);
      }
    })();
  }

  function coordinates(event, kind) {
    const box = image.getBoundingClientRect();
    return {
      kind,
      x: Math.max(0, Math.min(image.naturalWidth, (event.clientX - box.left) * image.naturalWidth / box.width)),
      y: Math.max(0, Math.min(image.naturalHeight, (event.clientY - box.top) * image.naturalHeight / box.height)),
    };
  }

  image.addEventListener('pointerdown', (event) => {
    if (busy || pointer !== null || !image.naturalWidth || event.button !== 0) return;
    event.preventDefault();
    pointer = event.pointerId;
    image.setPointerCapture(pointer);
    screen.focus({ preventScroll: true });
    sendInput(coordinates(event, 'down'));
  });
  image.addEventListener('pointermove', (event) => {
    if (event.pointerId !== pointer || !image.naturalWidth) return;
    const now = performance.now();
    if (moving && now - moving < 48) return;
    moving = now;
    sendInput(coordinates(event, 'move'));
  });
  for (const eventName of ['pointerup', 'pointercancel']) {
    image.addEventListener(eventName, (event) => {
      if (event.pointerId !== pointer) return;
      pointer = null;
      sendInput(coordinates(event, 'up'));
    });
  }
  screen.addEventListener('wheel', (event) => {
    event.preventDefault();
    sendInput({ kind: 'wheel', delta: Math.max(-2000, Math.min(2000, event.deltaY)) });
  }, { passive: false });
  screen.addEventListener('keydown', (event) => {
    if (event.isComposing || event.defaultPrevented) return;
    if (event.ctrlKey && event.key.toLowerCase() === 'a') {
      event.preventDefault();
      sendInput({ kind: 'key', key: 'Control+a' });
    } else if (!event.metaKey && !event.altKey && !event.ctrlKey && event.key.length === 1) {
      event.preventDefault();
      sendInput({ kind: 'text', text: event.key });
    } else if (['Tab', 'Enter', 'Escape', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) {
      event.preventDefault();
      sendInput({ kind: 'key', key: event.shiftKey && event.key === 'Tab' ? 'Shift+Tab' : event.key });
    }
  });
  screen.addEventListener('paste', (event) => {
    event.preventDefault();
    sendInput({ kind: 'text', text: event.clipboardData.getData('text').slice(0, 1024) });
  });
  el('keyboard-toggle').addEventListener('click', () => {
    const form = el('type-form');
    form.hidden = !form.hidden;
    el('keyboard-toggle').setAttribute('aria-expanded', String(!form.hidden));
    if (!form.hidden) el('type-input').focus();
  });
  el('type-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = el('type-input');
    sendInput({ kind: 'text', text: input.value });
    input.value = '';
    el('type-form').hidden = true;
    el('keyboard-toggle').setAttribute('aria-expanded', 'false');
    screen.focus();
  });
  el('browser-expand').addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await browser.requestFullscreen();
    } catch (_) {
      error('浏览器未能进入全屏。');
    }
  });
  el('browser-close').addEventListener('click', () => browserOperation(async () => {
    const id = session;
    try {
      if (id) await sessionRequest(id, 'close', {});
    } finally {
      resetBrowser();
    }
  }));
  el('browser-back')?.addEventListener('click', () => sendInput({ kind: 'back' }));
  el('browser-refresh')?.addEventListener('click', () => sendInput({ kind: 'reload' }));
  el('address-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = el('site-input').value.trim();
    if (value) openSite(/^https?:\/\//i.test(value) ? value : `https://${value}`);
  });
  el('recovery-open')?.addEventListener('click', () => openSite());
  el('resume-task')?.addEventListener('click', () => browserOperation(async () => {
    const result = await sessionRequest(session, 'resume', {});
    await finishSession(result.url);
  }));

  el('template-test')?.addEventListener('click', () => browserOperation(async () => {
    const outcome = await sessionRequest(session, 'test', {});
    addMessage(outcome.result);
  }));

  el('template-task')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const mode = event.submitter?.value || 'schedule';
    const clock = event.currentTarget.elements.clock.value;
    browserOperation(async () => {
      const result = await sessionRequest(session, 'save', { mode, clock });
      await finishSession(result.url);
    });
  });

  async function finishSession(url) {
    const id = session;
    resetBrowser();
    if (id) await sessionRequest(id, 'close', {}, true).catch(() => {});
    window.location.assign(url);
  }

  function showDraft(draft, saved, moveFocus = true) {
    const card = el('draft-template').content.firstElementChild.cloneNode(true);
    card.querySelector('.draft-site').textContent = draft.site;
    card.querySelector('.draft-summary').textContent = draft.summary;
    card.querySelector('.draft-stats').textContent = `${draft.steps} 步点击 · ${draft.requests} 条请求 · 拦截 ${draft.blocked} 条`;
    card.querySelector('.draft-result').textContent = draft.verified ? '录制时网页显示了签到结果。' : '录制时未识别到签到成功提示，可先试跑验证。';
    const form = card.querySelector('form');
    form.elements.name.value = new URL(draft.site).hostname;
    const full = card.querySelector('[data-full]');
    full.hidden = draft.full_steps || Boolean(saved);
    full.addEventListener('click', () => record('full'));
    for (const [index, step] of draft.script.steps.entries()) {
      const item = document.createElement('li');
      item.textContent = [draft.script.labels?.[index], step.text].filter(Boolean).join(' · ');
      card.querySelector('.draft-steps').append(item);
    }
    card.querySelector('pre').textContent = JSON.stringify(draft.script, null, 2);
    const savedLink = card.querySelector('.draft-template-link');
    const showSaved = (result) => {
      savedLink.href = result.url;
      savedLink.hidden = false;
      full.hidden = true;
      if (result.name) form.elements.name.value = result.name;
      form.elements.name.readOnly = true;
      setPhase('saved');
      status('模板已保存', true);
    };
    setPhase('review');
    if (saved) showSaved(saved);
    card.querySelector('[data-test]').addEventListener('click', () => browserOperation(async () => {
      const testResult = card.querySelector('.draft-test-result');
      status('正在试跑签到…');
      testResult.textContent = '';
      try {
        const outcome = await sessionRequest(session, 'test', {});
        testResult.textContent = outcome.result;
        status('试跑结束');
      } catch (failure) {
        testResult.textContent = failure.message;
        status('试跑未完成');
      }
    }));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form));
      data.mode = event.submitter?.value || 'template';
      if (!form.elements.name.reportValidity()) return;
      browserOperation(async () => {
        const result = await sessionRequest(session, 'save', data);
        if (result.task) await finishSession(result.url);
        else showSaved(result);
      });
    });
    draftPanel.replaceChildren(card);
    draftPanel.hidden = false;
    setControls();
    if (!saved) status('脚本已生成，可以检查或试跑', true);
    if (moveFocus) card.querySelector('h2').focus();
  }

  function record(mode) {
    browserOperation(async () => {
      if (mode === 'auto') status('正在识别并录制签到步骤…');
      else if (mode === 'finish' || mode === 'retry') status('正在分析录制结果…');
      const retry = el('record-retry');
      retry.hidden = true;
      try {
        const result = await sessionRequest(session, 'record', { mode });
        recording = Boolean(result.manual);
        recordingStatus = '';
        if (mode === 'cancel') {
          status('录制已取消', true);
        } else if (recording) {
          clearDraft();
          setPhase('record');
          if (result.reason && result.recorded_steps) {
            recordingStatus = `${result.reason}。已保留 ${result.recorded_steps} 步，请从当前页面继续，只录制剩余操作。完成后结束录制。`;
          } else if (result.reason) {
            recordingStatus = `${result.reason}。请在当前页面手动操作，完成后结束录制。`;
          } else {
            recordingStatus = '正在录制签到。操作完成后点击结束录制。';
          }
          status(recordingStatus, true);
          screen.focus({ preventScroll: true });
        } else {
          showDraft(result.draft);
        }
      } catch (failure) {
        // Retry analysis after a model outage without repeating the site action.
        retry.hidden = failure.status !== 502;
        if (failure.verification) {
          recording = false;
          recordingStatus = '';
          status('录制已暂停，等待站点验证', true);
        }
        throw failure;
      }
    });
  }

  el('record-auto')?.addEventListener('click', () => record('auto'));
  el('record-manual')?.addEventListener('click', () => record('manual'));
  el('record-finish')?.addEventListener('click', () => record('finish'));
  el('record-retry')?.addEventListener('click', () => record('retry'));
  el('record-cancel')?.addEventListener('click', () => record('cancel'));

  async function submitChat(value) {
    if (chatBusy || !value) return;
    const site = !task && !template && value.match(/^https?:\/\/[^\s<>]+/i);
    if (site) {
      try {
        const url = new URL(site[0]);
        el('site-input').value = url.href;
        addMessage(url.origin, 'user');
        await openSite(url.href);
      } catch (_) {
        addMessage('网址格式无效，请输入完整的网站地址。', 'error');
      }
      return;
    }
    if (workspace.dataset.model !== 'ready') {
      addMessage('请先配置自建模型。也可以直接输入完整网址打开在线浏览器。', 'error');
      return;
    }
    chatBusy = true;
    el('chat-send').disabled = true;
    const pending = addMessage('正在思考…');
    const turn = { role: 'user', content: value };
    try {
      const result = await api('/chat', { messages: [...history.slice(-10), turn] });
      pending.remove();
      addMessage(value, 'user');
      addMessage(result.text);
      history.push(turn, { role: 'assistant', content: result.text });
    } catch (failure) {
      pending.remove();
      addMessage(failure.message, 'error');
    } finally {
      chatBusy = false;
      el('chat-send').disabled = false;
    }
  }

  el('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const composer = el('chat-input');
    if (chatBusy) return;
    const text = composer.value.trim();
    composer.value = '';
    submitChat(text);
  });
  el('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      el('chat-form').requestSubmit();
    }
  });
  chat.addEventListener('click', (event) => {
    const prompt = event.target.closest('[data-prompt]');
    if (prompt) submitChat(prompt.dataset.prompt);
  });
  el('chat-reset').addEventListener('click', () => {
    if (chatBusy) return;
    history.length = 0;
    chat.replaceChildren(...welcome.map((node) => node.cloneNode(true)));
    el('chat-input').focus();
  });

  const previous = stored();
  if (workspace.dataset.browser && previous && /^[A-Za-z0-9_-]{32}$/.test(previous)) {
    session = previous;
    frames(session);
    setPhase('record');
    if (!task && !template) void restoreDraft(previous);
  }

  async function restoreDraft(id) {
    try {
      const response = await fetch(base + '/browser/' + id + '/draft', { cache: 'no-store' });
      if (!response.ok) return;
      const result = await response.json();
      if (session === id && result?.draft) showDraft(result.draft, result.template, false);
    } catch (_) {
      // The frame refresh handles connection and session expiry feedback.
    }
  }

  setControls();
})();
