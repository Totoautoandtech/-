(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.JobUtils = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const ACTIVE_JOB_KEY = 'vesper.activeJob.v2';

  function isTransientStatus(status) { return status === 502 || status === 503 || status === 504; }
  function backoffDelay(attempt) { return Math.min(12000, 750 * Math.pow(2, Math.max(0, attempt))); }
  function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

  function normalizeTikTokLink(value) {
    let url;
    try { url = new URL(String(value || '').trim()); } catch (_) { return { ok: false, error: 'URL illisible' }; }
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!['http:', 'https:'].includes(url.protocol)) return { ok: false, error: 'Protocole invalide' };
    if (!(host === 'tiktok.com' || host.endsWith('.tiktok.com'))) return { ok: false, error: 'Domaine TikTok requis' };
    if (!url.pathname || url.pathname === '/') return { ok: false, error: 'Vidéo manquante' };
    url.protocol = 'https:'; url.search = ''; url.hash = '';
    const clean = `${url.protocol}//${host}${url.pathname.replace(/\/{2,}/g, '/').replace(/\/$/, '')}`;
    return { ok: true, url: clean };
  }

  function parseLinks(text, max) {
    const links = [], errors = [], duplicates = [], seen = new Set();
    String(text || '').split(/\r?\n/).forEach((raw, index) => {
      if (!raw.trim()) return;
      const result = normalizeTikTokLink(raw);
      if (!result.ok) { errors.push({ index, url: raw.trim(), error: result.error }); return; }
      if (seen.has(result.url)) { duplicates.push(result.url); return; }
      seen.add(result.url); links.push(result.url);
    });
    if (links.length > max) errors.push({ index: max, url: '', error: `${max} liens maximum` });
    return { links: links.slice(0, max), errors, duplicates };
  }

  function saveActiveJob(storage, value) { storage.setItem(ACTIVE_JOB_KEY, JSON.stringify(value)); }
  function loadActiveJob(storage) {
    try {
      const value = JSON.parse(storage.getItem(ACTIVE_JOB_KEY) || 'null');
      return value && typeof value.id === 'string' ? value : null;
    } catch (_) { return null; }
  }
  function clearActiveJob(storage) { storage.removeItem(ACTIVE_JOB_KEY); }

  function submissionGuard() {
    let busy = false;
    return {
      get busy() { return busy; },
      async run(work) {
        if (busy) return { ignored: true };
        busy = true;
        try { return await work(); } finally { busy = false; }
      }
    };
  }

  return {
    ACTIVE_JOB_KEY, isTransientStatus, backoffDelay, sleep, normalizeTikTokLink,
    parseLinks, saveActiveJob, loadActiveJob, clearActiveJob, submissionGuard
  };
});
