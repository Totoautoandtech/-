(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.JobUtils = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const ACTIVE_JOB_KEY = 'vesper.activeJob.v2';
  const BATCH_DRAFTS_KEY = 'vesper.batchDrafts.v1';
  // Mode RsT multiple : un job indépendant par lien, suivis même après actualisation.
  const RST_JOBS_KEY = 'vesper.rstJobs.v1';
  const RST_LIENS_MAX = 6;

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
  function saveBatchDrafts(storage, drafts) {
    storage.setItem(BATCH_DRAFTS_KEY, JSON.stringify(Array.isArray(drafts) ? drafts.slice(0, 6) : []));
  }
  function loadBatchDrafts(storage) {
    try {
      const drafts = JSON.parse(storage.getItem(BATCH_DRAFTS_KEY) || '[]');
      return Array.isArray(drafts) ? drafts.slice(0, 6) : [];
    } catch (_) { return []; }
  }
  function clearBatchDrafts(storage) { storage.removeItem(BATCH_DRAFTS_KEY); }

  function parseRstLinks(text, max) {
    return parseLinks(text, Number.isFinite(max) && max > 0 ? max : RST_LIENS_MAX);
  }

  function saveRstJobs(storage, jobs) {
    const propres = (Array.isArray(jobs) ? jobs : [])
      .filter(job => job && typeof job.id === 'string' && job.id)
      .slice(0, RST_LIENS_MAX)
      .map(job => ({ id: job.id, lien: String(job.lien || ''), titre: String(job.titre || '') }));
    storage.setItem(RST_JOBS_KEY, JSON.stringify(propres));
  }

  function loadRstJobs(storage) {
    try {
      const jobs = JSON.parse(storage.getItem(RST_JOBS_KEY) || '[]');
      if (!Array.isArray(jobs)) return [];
      return jobs
        .filter(job => job && typeof job.id === 'string' && job.id)
        .slice(0, RST_LIENS_MAX);
    } catch (_) { return []; }
  }

  function clearRstJobs(storage) { storage.removeItem(RST_JOBS_KEY); }

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
    ACTIVE_JOB_KEY, BATCH_DRAFTS_KEY, isTransientStatus, backoffDelay, sleep, normalizeTikTokLink,
    RST_JOBS_KEY, RST_LIENS_MAX,
    parseLinks, parseRstLinks, saveActiveJob, loadActiveJob, clearActiveJob,
    saveBatchDrafts, loadBatchDrafts, clearBatchDrafts,
    saveRstJobs, loadRstJobs, clearRstJobs, submissionGuard
  };
});
