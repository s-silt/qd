(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const form = byId('author-form');
  const fileInput = byId('author-file');
  const submit = form.querySelector('button[type="submit"]');
  const status = byId('author-status');
  const error = byId('author-error');
  const preview = byId('author-draft');
  let draft;

  function showDraft(result) {
    draft = result;
    byId('author-summary').textContent = result.summary;
    const warnings = byId('author-warnings');
    warnings.replaceChildren(...result.warnings.map((message) => {
      const item = document.createElement('li');
      item.textContent = message;
      return item;
    }));
    warnings.hidden = warnings.childElementCount === 0;

    const bindings = Array.isArray(result.bindings) ? result.bindings : [];
    byId('author-steps').replaceChildren(...result.steps.map((step, offset) => {
      const item = document.createElement('li');
      const request = document.createElement('code');
      request.textContent = step.request.method + ' ' + step.request.url;
      const rules = document.createElement('small');
      const count = step.rule.success_asserts.filter((condition) => condition.re !== '(?!)').length;
      rules.textContent = count ? ('响应判定 ' + count + ' 项') : '缺少可确认的响应判定';
      item.append(request, rules);
      for (const binding of bindings.filter((entry) => entry.target_step === offset + 1)) {
        const flow = document.createElement('small');
        flow.textContent = '自动传值 · 从第 ' + binding.source_step + ' 步响应读取 ' + binding.field;
        item.append(flow);
      }
      return item;
    }));
    byId('author-json').textContent = JSON.stringify(result.steps, null, 2);
    preview.hidden = false;
    byId('author-draft-title').focus();
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const file = fileInput.files[0];
    const limit = Number(form.dataset.limit);
    error.textContent = '';
    status.textContent = '';
    draft = undefined;
    preview.hidden = true;
    if (!file || file.size > limit) {
      error.textContent = '请选择不超过 5 MB 的 HAR。';
      fileInput.focus();
      return;
    }
    submit.disabled = true;
    form.setAttribute('aria-busy', 'true');
    status.textContent = '正在分析签到记录…';
    try {
      let har;
      try {
        har = JSON.parse(await file.text());
      } catch (_) {
        throw new Error('读取失败。请选择 JSON 格式的 HAR。');
      }
      if (!Array.isArray(har?.log?.entries)) throw new Error('文件缺少 HAR 请求列表。');
      const body = JSON.stringify({ har, site: byId('author-site').value.trim() });
      if (new Blob([body]).size > limit) throw new Error('文件过大，请只保留这次签到的请求。');
      const response = await fetch('/ai-workbench/author', {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-QD-Workbench': '1' },
        body,
      });
      if (response.redirected || response.status === 403) throw new Error('请重新登录 QD 后再分析。');
      if (response.status === 413) throw new Error('文件过大，请只保留这次签到的请求。');
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !Array.isArray(result.steps) || !result.steps.length) {
        throw new Error(result.error || '这次没有生成草稿，请检查 HAR 后重试。');
      }
      showDraft(result);
      status.textContent = '草稿已生成，可以进入编辑器审核和试跑。';
    } catch (failure) {
      status.textContent = '';
      error.textContent = failure.message;
    } finally {
      form.removeAttribute('aria-busy');
      submit.disabled = false;
    }
  });

  byId('author-edit').addEventListener('click', () => {
    if (!draft) return;
    try {
      sessionStorage.setItem('qd-authoring-draft', JSON.stringify({
        owner: String(form.dataset.owner), site: draft.site, steps: draft.steps,
        bindings: draft.bindings,
      }));
      window.location.assign('/har/edit?workbench=1');
    } catch (_) {
      error.textContent = '浏览器未允许暂存草稿。请下载草稿，再到模板编辑器上传。';
    }
  });

  byId('author-download').addEventListener('click', () => {
    if (!draft) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft.steps, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'qd-signin-draft.har';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
