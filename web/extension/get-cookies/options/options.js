'use strict';

const STORAGE_KEY = 'qdHosts';
const $ = (id) => document.getElementById(id);

function applyI18n() {
  for (const el of document.querySelectorAll('[data-i18n]')) {
    const msg = chrome.i18n.getMessage(el.dataset.i18n);
    if (msg) el.textContent = msg;
  }
}

function setStatus(text, isError = false) {
  const el = $('status');
  el.textContent = text;
  el.classList.toggle('error', isError);
  setTimeout(() => { el.textContent = ''; }, 1800);
}

function normalize(raw) {
  const accepted = [];
  const rejected = [];
  for (const line of raw.split(/\s+/)) {
    if (!line) continue;
    const candidate = /^https?:\/\//i.test(line) ? line : `http://${line}`;
    try {
      // throws if invalid
      new URL(candidate);
      accepted.push(line);
    } catch {
      rejected.push(line);
    }
  }
  return { accepted, rejected };
}

async function load() {
  const { [STORAGE_KEY]: hosts = '' } = await chrome.storage.sync.get(STORAGE_KEY);
  $('hosts').value = hosts;
}

async function save() {
  const raw = $('hosts').value;
  const { accepted, rejected } = normalize(raw);
  await chrome.storage.sync.set({ [STORAGE_KEY]: accepted.join('\n') });
  $('hosts').value = accepted.join('\n');
  if (rejected.length) {
    setStatus(
      chrome.i18n.getMessage('optionsInvalid', rejected.join(', ')) ||
      `Invalid: ${rejected.join(', ')}`,
      true
    );
  } else {
    setStatus(chrome.i18n.getMessage('optionsSaved') || 'Saved');
  }
}

async function reset() {
  await chrome.storage.sync.remove(STORAGE_KEY);
  $('hosts').value = '';
  setStatus(chrome.i18n.getMessage('optionsReseted') || 'Reset');
}

document.addEventListener('DOMContentLoaded', () => {
  applyI18n();
  load();
  $('save').addEventListener('click', save);
  $('reset').addEventListener('click', reset);
});
