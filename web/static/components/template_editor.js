(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const app = byId('template-editor');
  const form = byId('te-form');
  const settings = byId('te-settings');
  const stepFields = byId('te-step-fields');
  const trialResult = byId('te-trial-result');
  const trialTrace = byId('te-trial-trace');
  const failedButton = byId('te-failed-step');
  const status = byId('te-status');
  const error = byId('te-error');
  const successText = byId('te-success-text');
  const templateId = app.dataset.template;
  const globals = new Set([
    '_cookies', '__log__', 'true', 'false', 'none',
    'loop_index0', 'loop_index', 'loop_first', 'loop_last', 'loop_length',
    'loop_revindex0', 'loop_revindex', 'loop_depth', 'loop_depth0',
    ...app.dataset.globals.split(','),
  ]);

  const groups = {
    success_asserts: { id: 'te-success', label: '成功条件' },
    failed_asserts: { id: 'te-failed', label: '失败条件' },
    extract_variables: { id: 'te-extract', label: '提取规则', extraction: true },
    headers: { id: 'te-headers', label: '请求头', pair: true },
    cookies: { id: 'te-cookies', label: 'Cookie', pair: true },
  };
  const sources = {
    content: '响应正文',
    status: 'HTTP 状态码',
    'header-location': '跳转地址（Location）',
    header: '全部响应头',
  };
  const receiptLabels = new Map([
    ['signed_in', '站点确认签到成功'],
    ['already_signed', '站点确认已签到'],
    ['failed', '签到失败'],
    ['unconfirmed', '签到结果待确认'],
  ]);
  const evidenceLabels = new Map([
    ['confirmed', '响应包含签到回执'],
    ['rule', '命中模板判定'],
    ['none', '无签到确认信息'],
  ]);
  const vars = new Map();
  const knownVars = new Set();
  let selected = 0;
  let readonly = false;
  let busy = false;
  let dirty = false;
  let failedIndex;
  let har = emptyHar();
  let extraSettings = {};
  let draftBindings = [];

  function el(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function emptyHar() {
    return { log: { version: '1.2', creator: { name: 'QD', version: '1.0' }, entries: [], pages: [] } };
  }

  function emptyStep() {
    return {
      comment: '',
      checked: true,
      startedDateTime: new Date().toISOString(),
      time: 0,
      request: { method: 'GET', url: '', httpVersion: 'HTTP/1.1', headers: [], cookies: [], queryString: [], headersSize: -1, bodySize: -1 },
      response: { content: {}, headers: [], cookies: [] },
      cache: {},
      timings: {},
      success_asserts: [],
      failed_asserts: [],
      extract_variables: [],
    };
  }

  function entries() {
    return har.log.entries;
  }

  function enabled(entry) {
    return !!entry.checked;
  }

  function currentStep() {
    return entries()[selected];
  }

  function escapeLiteral(value) {
    // A hex escape also keeps opening braces away from the Jinja parser.
    return value.replace(/[.*+?^${}()|[\]\\]/g, (char) => char === '{' ? '\\x7b' : '\\' + char);
  }

  function getRows(step, key) {
    return (groups[key].pair ? step.request : step)[key] || [];
  }

  function setRows(step, key, rows) {
    (groups[key].pair ? step.request : step)[key] = rows;
  }

  function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function pairsToTpl(pairs) {
    return (pairs || []).filter((row) => row.checked).map(({ name, value }) => ({ name, value }));
  }

  function toSteps() {
    return entries().filter(enabled).map((entry) => ({
      comment: entry.comment,
      request: {
        method: entry.request.method,
        url: entry.request.url,
        headers: pairsToTpl(entry.request.headers),
        cookies: pairsToTpl(entry.request.cookies),
        data: entry.request.postData?.text,
        mimeType: entry.request.postData?.mimeType,
      },
      rule: {
        success_asserts: getRows(entry, 'success_asserts'),
        failed_asserts: getRows(entry, 'failed_asserts'),
        extract_variables: getRows(entry, 'extract_variables'),
        ...(entry.signin_receipt === true ? { signin_receipt: true } : {}),
      },
    }));
  }

  function stepsToHar(steps) {
    return {
      ...emptyHar(),
      log: {
        ...emptyHar().log,
        entries: steps.map((step) => ({
          ...emptyStep(),
          comment: step.comment || '',
          request: {
            ...emptyStep().request,
            method: step.request.method,
            url: step.request.url,
            headers: (step.request.headers || []).map((row) => ({ ...row, checked: true })),
            cookies: (step.request.cookies || []).map((row) => ({ ...row, checked: true })),
            ...(step.request.mimeType !== undefined || step.request.data !== undefined
              ? { postData: { mimeType: step.request.mimeType || '', text: step.request.data || '' } }
              : {}),
          },
          ...Object.fromEntries(
            ['success_asserts', 'failed_asserts', 'extract_variables']
              .map((key) => [key, step.rule?.[key] || []]),
          ),
          ...(step.rule?.signin_receipt === true ? { signin_receipt: true } : {}),
        })),
      },
    };
  }

  function normalizeHar(input) {
    if (Array.isArray(input)) {
      if (!input.every((step) => isObject(step) && isObject(step.request))) {
        throw new Error('该文件不是可用的 QD 模板。');
      }
      input = stepsToHar(input);
    }
    if (!Array.isArray(input?.log?.entries) || !input.log.entries.every((entry) => (
      isObject(entry) && isObject(entry.request) && typeof entry.request.method === 'string' &&
      typeof entry.request.url === 'string' && Object.keys(groups).every((key) => {
        const rows = (groups[key].pair ? entry.request : entry)[key];
        return rows == null || (Array.isArray(rows) && rows.every(isObject));
      })
    ))) {
      throw new Error('该文件缺少可用的 HAR 请求列表。');
    }
    return input;
  }

  function showActionError(cause) {
    error.textContent = cause.message || '操作失败，请重试。';
    status.textContent = '';
  }

  function markChanged() {
    dirty = true;
    invalidateTrial('内容已改动，请重新试跑。');
  }

  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    syncControls();
  }

  function syncControls() {
    settings.disabled = readonly || busy;
    stepFields.disabled = readonly || busy;
    byId('te-variables').disabled = busy;
    byId('te-import').disabled = readonly || busy;
    byId('te-download').disabled = busy || !entries().length;
    byId('te-add-step').disabled = readonly || busy;
    byId('te-up').disabled = readonly || busy || selected <= 0;
    byId('te-down').disabled = readonly || busy || selected >= entries().length - 1;
    byId('te-remove-step').disabled = readonly || busy || !currentStep();
    byId('te-trial').disabled = busy || !entries().some(enabled);
    byId('te-save').disabled = readonly || busy || !entries().some(enabled);
    byId('te-save').hidden = readonly;
    for (const element of form.querySelectorAll('[data-te-mutable]')) {
      element.disabled = readonly || busy;
    }
    for (const element of byId('te-steps').querySelectorAll('button')) element.disabled = busy;
  }

  function parseUrl(value) {
    try {
      return new URL(value);
    } catch (_) {
      return null;
    }
  }

  function briefUrl(url) {
    if (!url) return '待填写请求地址';
    const parsed = parseUrl(url);
    return parsed && (parsed.protocol === 'https:' || parsed.protocol === 'http:')
      ? parsed.hostname + parsed.pathname
      : url;
  }

  function renderSteps() {
    const list = byId('te-steps');
    list.replaceChildren(...entries().map((step, index) => {
      const item = el('li');
      item.className = 'te-step-item';
      const checkbox = el('input');
      checkbox.type = 'checkbox';
      checkbox.checked = enabled(step);
      checkbox.setAttribute('aria-label', '执行步骤 ' + (index + 1));
      checkbox.dataset.teMutable = '';
      checkbox.addEventListener('change', () => {
        step.checked = checkbox.checked;
        item.classList.toggle('te-skipped', !checkbox.checked);
        markChanged();
        updateCount();
        renderVariables();
        syncControls();
      });
      const button = el('button');
      button.type = 'button';
      button.className = 'te-step-button';
      button.title = step.request.url;
      if (index === selected) button.setAttribute('aria-current', 'true');
      const head = el('strong', (index + 1) + '. ' + (step.comment || step.request.method || '请求'));
      const source = el('span', briefUrl(step.request.url));
      button.append(head, source);
      button.addEventListener('click', () => selectStep(index, 'list'));
      item.classList.toggle('te-skipped', !checkbox.checked);
      item.append(checkbox, button);
      return item;
    }));
    updateCount();
    byId('te-empty').hidden = entries().length !== 0;
  }

  function updateCount() {
    byId('te-step-count').textContent = '执行 ' + entries().filter(enabled).length + ' / ' + entries().length + ' 步';
  }

  function updateActiveStepLabel() {
    const step = currentStep();
    const button = byId('te-steps').children[selected]?.querySelector('button');
    if (!step || !button) return;
    button.title = step.request.url;
    button.firstChild.textContent = (selected + 1) + '. ' + (step.comment || step.request.method || '请求');
    button.lastChild.textContent = briefUrl(step.request.url);
  }

  function rowField(wrapper, labelText, value, update, choices) {
    const label = el('label', labelText);
    const input = choices ? el('select') : el('input');
    input.dataset.teMutable = '';
    if (choices) {
      for (const [key, title] of Object.entries(choices)) {
        const option = el('option', title);
        option.value = key;
        input.append(option);
      }
      if (value && !Object.hasOwn(choices, value)) {
        const legacy = el('option', value);
        legacy.value = value;
        input.append(legacy);
      }
    } else {
      input.spellcheck = false;
      input.autocomplete = 'off';
    }
    input.value = value || '';
    input.addEventListener('input', () => {
      update(input.value);
      markChanged();
      updateRuleCount();
      renderVariables();
    });
    label.append(input);
    wrapper.append(label);
  }

  function renderRows(key) {
    const step = currentStep();
    const group = groups[key];
    const host = byId(group.id);
    host.replaceChildren(...getRows(step, key).map((row, index, rows) => {
      const wrapper = el('div');
      wrapper.className = 'te-rule-row';
      wrapper.setAttribute('role', 'group');
      wrapper.setAttribute('aria-label', group.label + ' ' + (index + 1));
      if (group.pair) {
        const checkbox = el('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !!row.checked;
        checkbox.dataset.teMutable = '';
        const label = el('label', '使用');
        label.className = 'te-check';
        label.append(checkbox);
        wrapper.append(label);
        checkbox.addEventListener('change', () => {
          row.checked = checkbox.checked;
          markChanged();
          renderVariables();
        });
        rowField(wrapper, '名称', row.name, (value) => { row.name = value; });
        rowField(wrapper, '值', row.value, (value) => { row.value = value; });
      } else {
        const literal = key === 'success_asserts' && (!row.from || row.from === 'content') && typeof row.re === 'string'
          ? row.re.replace(/\\(x7b|.)/g, (_, char) => char === 'x7b' ? '{' : char)
          : '';
        if (literal && escapeLiteral(literal) === row.re) {
          rowField(wrapper, '响应正文包含', literal, (value) => { row.re = escapeLiteral(value); });
        } else {
          if (group.extraction) rowField(wrapper, '变量名', row.name, (value) => { row.name = value; });
          rowField(wrapper, '匹配范围', row.from || 'content', (value) => { row.from = value; }, sources);
          rowField(wrapper, '匹配表达式', row.re, (value) => { row.re = value; });
        }
      }
      const remove = el('button', '删除');
      remove.type = 'button';
      remove.className = 'button';
      remove.dataset.teMutable = '';
      remove.setAttribute('aria-label', '删除' + group.label + ' ' + (index + 1));
      remove.addEventListener('click', () => {
        rows.splice(index, 1);
        renderRows(key);
        updateRuleCount();
        renderVariables();
        markChanged();
        syncControls();
        const next = host.children[index]?.querySelector('input, select') ||
          form.querySelector('[data-group="' + key + '"]');
        next.focus();
      });
      wrapper.append(remove);
      return wrapper;
    }));
  }

  function updateRuleCount() {
    const step = currentStep();
    const matches = step ? getRows(step, 'success_asserts').filter((rule) => rule.re && rule.re !== '(?!)').length : 0;
    byId('te-rule-count').textContent = matches ? '（' + matches + ' 条成功条件）' : '（未设置成功条件）';
  }

  function renderStep() {
    const step = currentStep();
    byId('te-step-panel').hidden = !step;
    if (!step) return;
    successText.value = '';
    byId('te-step-title').textContent = '编辑步骤 ' + (selected + 1);
    stepFields.querySelector('[name="comment"]').value = step.comment || '';
    stepFields.querySelector('[name="method"]').value = step.request.method;
    stepFields.querySelector('[name="url"]').value = step.request.url;
    stepFields.querySelector('[name="mime"]').value = step.request.postData?.mimeType || '';
    stepFields.querySelector('[name="body"]').value = step.request.postData?.text || '';
    stepFields.querySelector('[name="signin_receipt"]').checked = step.signin_receipt === true;
    for (const key of Object.keys(groups)) renderRows(key);
    updateRuleCount();
  }

  function selectStep(index, focus) {
    selected = index;
    renderSteps();
    renderStep();
    syncControls();
    if (focus === 'list') byId('te-steps').children[selected]?.querySelector('button')?.focus();
    if (focus === 'address') stepFields.querySelector('[name="url"]').focus();
    if (focus === 'failed') {
      byId('te-step-title').focus();
      byId('te-step-panel').scrollIntoView({ block: 'nearest' });
    }
  }

  function referencedFields(request, rules) {
    return [
      request.method, request.url, request.data,
      ...request.headers.flatMap((pair) => [pair.name, pair.value]),
      ...request.cookies.flatMap((pair) => [pair.name, pair.value]),
      ...Object.values(rules).flat().map((rule) => rule.re),
    ].filter((value) => typeof value === 'string');
  }

  function bindDraft(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.flatMap((row) => {
      if (!row || !Number.isInteger(row.source_step) || !Number.isInteger(row.target_step) ||
          typeof row.field !== 'string' || typeof row.variable !== 'string' || !/^\w+$/.test(row.variable)) return [];
      const source = entries()[row.source_step - 1];
      const target = entries()[row.target_step - 1];
      if (!source || !target) return [];
      const extraction = getRows(source, 'extract_variables').find((rule) => rule.name === row.variable);
      return extraction ? [{ source, target, field: row.field, variable: row.variable, re: extraction.re }] : [];
    });
  }

  function renderBindings(steps) {
    const summary = byId('te-bindings');
    const items = draftBindings.flatMap(({ source, target, field, variable, re }) => {
      const sourceIndex = entries().indexOf(source);
      const targetIndex = entries().indexOf(target);
      if (sourceIndex < 0 || targetIndex <= sourceIndex || !enabled(source) || !enabled(target)) return [];
      if (!getRows(source, 'extract_variables').some((rule) => rule.name === variable && rule.re === re)) return [];
      const execTarget = entries().filter(enabled).indexOf(target);
      const step = steps[execTarget];
      const refers = new RegExp('\\{\\{\\s*' + variable + '\\b');
      if (!referencedFields(step.request, step.rule).some((value) => refers.test(value))) return [];
      const overridden = entries().slice(sourceIndex + 1, targetIndex).some((entry) =>
        enabled(entry) && getRows(entry, 'extract_variables').some((rule) => rule.name === variable));
      if (overridden) return [];
      return [el('li', '第 ' + (targetIndex + 1) + ' 步使用从第 ' + (sourceIndex + 1) + ' 步响应提取的 ' + field)];
    });
    summary.querySelector('ul').replaceChildren(...items);
    summary.hidden = items.length === 0;
  }

  function neededVariables(steps = toSteps()) {
    const needed = new Set();
    const available = new Set(globals);
    for (const step of steps) {
      for (const raw of referencedFields(step.request, step.rule)) {
        const expressions = raw.matchAll(/\{\{([\s\S]*?)\}\}|\{%([\s\S]*?)%\}/g);
        for (const match of expressions) {
          const expr = match[1] ?? match[2];
          const uncommented = expr.replace(/"[^"]*"|'[^']*'/g, '');
          const first = expr.match(/^\s*([A-Za-z_]\w*)/);
          const optional = first && /^\s*\|\s*default\s*\(/.test(expr.slice(first[0].length)) ? first[1] : null;
          if (match[1] !== undefined && first && first[1] !== optional && !available.has(first[1])) {
            needed.add(first[1]);
          }
          // Exact saved names also cover references inside more complex Jinja expressions.
          for (const [, name] of uncommented.matchAll(/(?<![.\w])([A-Za-z_]\w*)/g)) {
            if (name !== optional && knownVars.has(name) && !available.has(name)) needed.add(name);
          }
        }
      }
      for (const rule of step.rule.extract_variables) {
        if (typeof rule.name === 'string' && rule.name) available.add(rule.name);
      }
    }
    return [...needed];
  }

  function renderVariables() {
    const list = byId('te-variables');
    const steps = toSteps();
    const names = neededVariables(steps);
    const active = list.contains(document.activeElement) && document.activeElement.dataset.variable;
    list.replaceChildren(...names.map((name) => {
      const label = el('label', name);
      const input = el('input');
      input.dataset.variable = name;
      input.value = vars.get(name) ?? '';
      input.type = /password|passwd|token|cookie|authorization|secret|auth|session|key/i.test(name) ? 'password' : 'text';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.setAttribute('aria-describedby', 'te-trial-help');
      input.addEventListener('input', () => {
        vars.set(name, input.value);
        invalidateTrial();
      });
      label.append(input);
      return label;
    }));
    if (active) [...list.querySelectorAll('input')].find((input) => input.dataset.variable === active)?.focus();
    byId('te-no-variables').hidden = names.length !== 0;
    renderBindings(steps);
  }

  function invalidateTrial(message = '试跑参数已改动，请重新试跑。') {
    if (!trialResult.hidden && trialResult.dataset.outcome !== 'stale') {
      trialResult.textContent = message;
      trialResult.dataset.outcome = 'stale';
      failedButton.hidden = true;
      byId('te-trace-title').textContent = '上次试跑日志';
    }
  }

  function renderTrialDetails(list, details) {
    const rows = Array.isArray(details) ? details.filter((row) => (
      isObject(row) && typeof row.label === 'string' && row.label.length &&
      ['string', 'number', 'boolean'].includes(typeof row.value)
    )) : [];
    list.replaceChildren(...rows.map(({ label, value }) => {
      const detail = el('div');
      detail.append(el('dt', label), el('dd', String(value)));
      return detail;
    }));
    list.hidden = rows.length === 0;
  }

  function trialRequestLabel(step, report) {
    const method = [report.method, step.request.method].find((value) =>
      typeof value === 'string' && /^[A-Z][A-Z-]{0,15}$/i.test(value));
    const url = parseUrl(step.request.url);
    const path = url && ['http:', 'https:'].includes(url.protocol) ? url.pathname : '';
    return [method?.toUpperCase(), path].filter(Boolean).join(' ');
  }

  function renderTrial(data, activeEntries) {
    const failed = activeEntries[data.failed_step - 1];
    failedIndex = failed ? entries().indexOf(failed) : -1;
    const runLabel = data.success === true
      ? '试跑通过'
      : (failedIndex >= 0 ? '第 ' + (failedIndex + 1) + ' 步未通过' : '试跑未通过');
    const receipt = isObject(data.receipt) ? data.receipt : {};
    const receiptLabel = receiptLabels.get(receipt.outcome) || receiptLabels.get('unconfirmed');
    const summary = [runLabel, receiptLabel];
    const message = data.success === true ? receipt.message || data.message : data.message || receipt.message;
    trialResult.dataset.outcome = 'unknown';
    if (data.success !== true || receipt.outcome === 'failed') trialResult.dataset.outcome = 'failure';
    else if (receipt.outcome === 'signed_in' || receipt.outcome === 'already_signed') {
      trialResult.dataset.outcome = 'success';
    }
    trialResult.textContent = summary.join(' · ') + (message ? '：' + message : '。');
    failedButton.hidden = data.success === true || failedIndex < 0;

    const list = byId('te-trial-steps');
    const reports = Array.isArray(data.steps) ? data.steps : [];
    list.replaceChildren(...reports.flatMap((report) => {
      if (!isObject(report) || !Number.isInteger(report.step)) return [];
      const step = activeEntries[report.step - 1];
      if (!step) return [];
      const item = el('li');
      item.className = 'te-trace-step';
      item.dataset.outcome = report.outcome === 'passed' ? 'passed' : 'failed';
      const heading = el('h4', '第 ' + (entries().indexOf(step) + 1) + ' 步');
      const request = trialRequestLabel(step, report);
      if (request) heading.append(el('code', request));
      if (Number.isInteger(report.ordinal) && report.ordinal > 0) {
        const ordinal = el('span', '执行序号 ' + report.ordinal);
        ordinal.className = 'te-trace-ordinal';
        heading.append(ordinal);
      }
      const state = [report.outcome === 'passed' ? '步骤通过' : '步骤未通过'];
      if (report.internal === true) state.push('QD 内部处理');
      if (Number.isInteger(report.status) && report.status >= 100 && report.status <= 599) {
        state.push('HTTP ' + report.status);
      }
      state.push(evidenceLabels.get(report.evidence) || evidenceLabels.get('none'));
      const line = el('p', state.join(' · '));
      line.className = 'te-trace-state';
      item.append(heading, line);
      if (typeof report.message === 'string' && report.message) {
        const detail = el('p', report.message);
        detail.className = 'te-trace-message';
        item.append(detail);
      }
      const details = el('dl');
      details.className = 'te-trace-details';
      renderTrialDetails(details, report.details);
      if (!details.hidden) item.append(details);
      return [item];
    }));
    list.hidden = list.childElementCount === 0;
    const receiptDetails = byId('te-receipt-details');
    renderTrialDetails(receiptDetails, receipt.details);
    const templateLog = byId('te-template-log');
    const templateDetails = templateLog.querySelector('dl');
    renderTrialDetails(templateDetails, data.template_log);
    templateLog.hidden = templateDetails.hidden;
    const omitted = byId('te-trace-omitted');
    omitted.hidden = !Number.isInteger(data.omitted_steps) || data.omitted_steps <= 0;
    if (!omitted.hidden) omitted.textContent = '已省略中间 ' + data.omitted_steps + ' 条步骤记录。';
    const elapsed = byId('te-trace-duration');
    const ms = data.duration_ms;
    elapsed.hidden = !Number.isFinite(ms) || ms < 0;
    if (!elapsed.hidden) elapsed.textContent = '耗时 ' + (ms < 1000 ? Math.round(ms) + ' 毫秒' : (ms / 1000).toFixed(1) + ' 秒');
    const timestamp = byId('te-trace-time');
    const now = new Date();
    timestamp.dateTime = now.toISOString();
    timestamp.textContent = new Intl.DateTimeFormat(document.documentElement.lang, {
      dateStyle: 'short', timeStyle: 'medium',
    }).format(now);
    byId('te-trace-title').textContent = '试跑日志';
    trialTrace.hidden = list.hidden && receiptDetails.hidden && templateLog.hidden && elapsed.hidden;
  }

  async function send(path, payload, headers = {}) {
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-XSRFToken': app.dataset.xsrf,
        ...headers,
      },
      body: JSON.stringify(payload),
    });
    if (response.redirected || response.status === 401) throw new Error('请先登录 QD 或确认模板访问权限。');
    if (response.status === 403) throw new Error('当前账号没有执行该操作的权限。');
    if (response.status === 404) throw new Error('模板已不存在。');
    if (response.status === 413) throw new Error('模板超过服务允许的大小。');
    const data = await response.json().catch(() => undefined);
    if (!response.ok || !isObject(data)) {
      throw new Error(data?.message || 'QD 未能完成请求，请重试。');
    }
    return data;
  }

  function checkedSteps() {
    const all = toSteps();
    if (!all.length) throw new Error('请至少勾选一个请求步骤。');
    const empty = all.findIndex((step) => !step.request.method.trim() || !step.request.url.trim());
    if (empty >= 0) {
      const originalIndex = entries().indexOf(entries().filter(enabled)[empty]);
      selectStep(originalIndex, 'address');
      throw new Error('请填写该步骤的请求方法和地址。');
    }
    return all;
  }

  function saveSettings() {
    const fields = new FormData(form);
    return {
      ...extraSettings,
      sitename: fields.get('sitename').trim(),
      siteurl: fields.get('siteurl').trim(),
      interval: fields.get('interval'),
      note: fields.get('note'),
    };
  }

  function updateReady() {
    renderSteps();
    renderStep();
    renderVariables();
    syncControls();
  }

  function loadHar(candidate, fromDisk = false) {
    har = normalizeHar(candidate);
    if (fromDisk) {
      selected = 0;
      draftBindings = [];
      vars.clear();
      knownVars.clear();
      markChanged();
      const first = entries().find((entry) => /^https?:\/\//i.test(entry.request.url));
      const url = first && parseUrl(first.request.url);
      if (!templateId && url) {
        if (!form.elements.sitename.value) form.elements.sitename.value = url.hostname;
        if (!form.elements.siteurl.value) form.elements.siteurl.value = url.origin;
      }
    }
    updateReady();
  }

  byId('te-legacy').href = (() => {
    const legacy = new URL(window.location.href);
    legacy.searchParams.set('legacy', '1');
    return legacy.pathname + legacy.search;
  })();

  stepFields.addEventListener('input', (event) => {
    const step = currentStep();
    if (!step || readonly) return;
    const field = event.target.name;
    switch (field) {
      case 'comment': step.comment = event.target.value; break;
      case 'method': step.request.method = event.target.value; break;
      case 'url': step.request.url = event.target.value; break;
      case 'signin_receipt':
        if (event.target.checked) step.signin_receipt = true;
        else delete step.signin_receipt;
        break;
      case 'body':
      case 'mime':
        if (!isObject(step.request.postData)) step.request.postData = {};
        step.request.postData[field === 'body' ? 'text' : 'mimeType'] = event.target.value;
        break;
      default: return;
    }
    updateActiveStepLabel();
    markChanged();
    renderVariables();
  });
  settings.addEventListener('input', markChanged);

  byId('te-add-step').addEventListener('click', () => {
    entries().push(emptyStep());
    markChanged();
    selectStep(entries().length - 1, 'address');
    renderVariables();
  });

  function moveStep(offset) {
    const next = selected + offset;
    const steps = entries();
    if (next < 0 || next >= steps.length) return;
    [steps[selected], steps[next]] = [steps[next], steps[selected]];
    markChanged();
    selectStep(next, 'list');
    renderVariables();
  }
  byId('te-up').addEventListener('click', () => moveStep(-1));
  byId('te-down').addEventListener('click', () => moveStep(1));
  byId('te-remove-step').addEventListener('click', () => {
    if (!currentStep()) return;
    entries().splice(selected, 1);
    markChanged();
    selectStep(Math.max(0, Math.min(selected, entries().length - 1)), 'list');
    renderVariables();
    if (!entries().length) byId('te-add-step').focus();
  });

  for (const button of form.querySelectorAll('.te-add-row')) {
    button.dataset.teMutable = '';
    button.addEventListener('click', () => {
      const key = button.dataset.group;
      const step = currentStep();
      const group = groups[key];
      const newRow = group.pair ? { name: '', value: '', checked: true } : { re: '', from: 'content' };
      if (group.extraction) newRow.name = '';
      setRows(step, key, [...getRows(step, key), newRow]);
      renderRows(key);
      updateRuleCount();
      markChanged();
      syncControls();
      byId(group.id).lastElementChild.querySelector('input, select')?.focus();
    });
  }

  function addSuccessText() {
    const literal = successText.value;
    const step = currentStep();
    if (!step || readonly || busy || !literal.trim()) {
      successText.focus();
      return;
    }
    const rules = getRows(step, 'success_asserts').filter((rule) => rule.re !== '(?!)');
    setRows(step, 'success_asserts', [...rules, { from: 'content', re: escapeLiteral(literal) }]);
    successText.value = '';
    renderRows('success_asserts');
    updateRuleCount();
    markChanged();
    successText.focus();
  }
  byId('te-add-success-text').addEventListener('click', addSuccessText);
  successText.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      addSuccessText();
    }
  });

  const file = byId('te-file');
  byId('te-import').addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const selectedFile = file.files[0];
    file.value = '';
    if (!selectedFile || busy || readonly) return;
    if (entries().length && !window.confirm('导入将替换编辑器中当前的请求步骤。继续导入吗？')) return;
    error.textContent = '';
    setBusy(true);
    try {
      if (selectedFile.size > 10 * 1024 * 1024) throw new Error('请选择 10 MB 以内的 HAR 或 QD 模板。');
      const source = await selectedFile.text();
      let data;
      try {
        data = JSON.parse(source.replace(/^\ufeff/, ''));
      } catch (_) {
        throw new Error('文件无法读取，请选择格式正确的 HAR 或 QD 模板。');
      }
      loadHar(data, true);
      status.textContent = '已导入 ' + entries().length + ' 个请求步骤，请确认要执行的步骤已经勾选。';
    } catch (cause) {
      showActionError(cause);
    } finally {
      setBusy(false);
    }
  });

  byId('te-download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(har, null, 2)], { type: 'application/json' }));
    const link = el('a');
    link.href = url;
    link.download = 'qd-template.har';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  byId('te-trial').addEventListener('click', async () => {
    if (busy) return;
    error.textContent = '';
    status.textContent = '';
    failedButton.hidden = true;
    trialTrace.hidden = true;
    try {
      const steps = checkedSteps();
      const activeEntries = entries().filter(enabled);
      const variables = Object.fromEntries(neededVariables().map((name) => [name, vars.get(name) ?? '']));
      trialResult.textContent = '正在试跑…';
      trialResult.dataset.outcome = 'pending';
      trialResult.hidden = false;
      setBusy(true);
      const data = await send(
        '/tpl/run?format=json',
        { tpl: steps, env: { variables, session: [] } },
        { 'X-QD-Editor': '1' },
      );
      renderTrial(data, activeEntries);
    } catch (cause) {
      showActionError(cause);
      trialResult.hidden = true;
    } finally {
      setBusy(false);
      if (document.activeElement === document.body) byId('te-trial').focus();
    }
  });

  failedButton.addEventListener('click', () => selectStep(failedIndex, 'failed'));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (readonly || busy) return;
    error.textContent = '';
    try {
      const steps = checkedSteps();
      const setting = saveSettings();
      if (!setting.sitename) throw new Error('请填写模板名称。');
      const url = new URL(templateId ? '/tpl/' + templateId + '/save' : '/har/save', window.location.origin);
      if (app.dataset.sourceRepo) url.searchParams.set('reponame', app.dataset.sourceRepo);
      if (app.dataset.sourceName) url.searchParams.set('name', app.dataset.sourceName);
      status.textContent = '正在保存…';
      setBusy(true);
      const result = await send(url.pathname + url.search, { har, tpl: steps, setting });
      if (!/^[1-9][0-9]*$/.test(String(result.id))) throw new Error('QD 没有返回保存后的模板编号。');
      dirty = false;
      status.textContent = '模板已保存。';
      if (String(result.id) !== templateId) window.location.assign('/tpl/' + result.id + '/edit');
    } catch (cause) {
      showActionError(cause);
    } finally {
      setBusy(false);
    }
  });

  window.addEventListener('beforeunload', (event) => {
    if (dirty && !readonly) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  async function initialize() {
    try {
      if (templateId) {
        const data = await send(window.location.pathname + window.location.search, {});
        readonly = !!data.readonly;
        extraSettings = data.setting || {};
        har = normalizeHar(data.har);
        for (const [name, value] of Object.entries(data.env || {})) {
          knownVars.add(name);
          vars.set(name, value ?? '');
        }
      } else if (app.dataset.workbench === '1') {
        const serialized = sessionStorage.getItem('qd-authoring-draft');
        sessionStorage.removeItem('qd-authoring-draft');
        const draft = JSON.parse(serialized);
        if (!draft || String(draft.owner) !== app.dataset.owner || !Array.isArray(draft.steps)) {
          throw new Error('草稿已失效，请回到 AI 签到工作台，或重新导入文件。');
        }
        const site = new URL(draft.site);
        extraSettings = { sitename: site.hostname, siteurl: site.origin };
        har = normalizeHar(draft.steps);
        draftBindings = bindDraft(draft.bindings);
        dirty = true;
      }
      status.textContent = readonly
        ? '该模板为只读，可查看、下载和试跑。'
        : (app.dataset.workbench === '1' ? '已带入 AI 草稿，请检查请求和成功条件后试跑。' : '');
    } catch (cause) {
      showActionError(cause);
      if (templateId) return;
    }
    for (const name of ['sitename', 'siteurl', 'interval', 'note']) {
      form.elements[name].value = extraSettings[name] ?? '';
    }
    form.hidden = false;
    updateReady();
  }

  initialize();
})();
