const test = require('node:test');
const assert = require('node:assert/strict');
const U = require('../../static/job-utils.js');

test('les erreurs 502/503/504 sont temporaires et le backoff est borné', () => {
  assert.equal(U.isTransientStatus(502), true);
  assert.equal(U.isTransientStatus(503), true);
  assert.equal(U.isTransientStatus(504), true);
  assert.equal(U.isTransientStatus(500), false);
  assert.equal(U.backoffDelay(0), 750);
  assert.equal(U.backoffDelay(20), 12000);
});

test('les paramètres TikTok sont nettoyés et les doublons supprimés', () => {
  const parsed = U.parseLinks([
    'https://www.tiktok.com/@demo/video/123?q=abc&t=4',
    'https://www.tiktok.com/@demo/video/123?is_copy_url=1'
  ].join('\n'), 20);
  assert.deepEqual(parsed.links, ['https://www.tiktok.com/@demo/video/123']);
  assert.equal(parsed.duplicates.length, 1);
  assert.equal(parsed.errors.length, 0);
});

test('un job sauvegardé est repris après actualisation', () => {
  const values = new Map();
  const storage = {
    setItem: (key, value) => values.set(key, value),
    getItem: key => values.get(key) || null,
    removeItem: key => values.delete(key)
  };
  U.saveActiveJob(storage, { id: 'abc123', type: 'montage' });
  assert.deepEqual(U.loadActiveJob(storage), { id: 'abc123', type: 'montage' });
  U.clearActiveJob(storage);
  assert.equal(U.loadActiveJob(storage), null);
});

test('six projets préparés survivent à une actualisation', () => {
  const values = new Map();
  const storage = {
    setItem: (key, value) => values.set(key, value),
    getItem: key => values.get(key) || null,
    removeItem: key => values.delete(key)
  };
  const drafts = Array.from({ length: 6 }, (_, i) => ({ titre: `Projet ${i + 1}` }));
  U.saveBatchDrafts(storage, drafts);
  assert.deepEqual(U.loadBatchDrafts(storage), drafts);
  U.saveBatchDrafts(storage, [...drafts, { titre: 'Projet 7' }]);
  assert.equal(U.loadBatchDrafts(storage).length, 6);
  U.clearBatchDrafts(storage);
  assert.deepEqual(U.loadBatchDrafts(storage), []);
});

test('le verrou de soumission empêche un double clic', async () => {
  const guard = U.submissionGuard();
  let releases;
  const pending = new Promise(resolve => { releases = resolve; });
  let calls = 0;
  const first = guard.run(async () => { calls += 1; await pending; return 'ok'; });
  const second = await guard.run(async () => { calls += 1; return 'duplicate'; });
  assert.deepEqual(second, { ignored: true });
  releases();
  assert.equal(await first, 'ok');
  assert.equal(calls, 1);
});

test('RsT multiple : 6 liens maximum, doublons et erreurs signalés', () => {
  const texte = [
    'https://www.tiktok.com/@a/video/1',
    'https://www.tiktok.com/@a/video/1?is_from_webapp=1',
    'https://example.com/pas-tiktok',
    'https://www.tiktok.com/@b/video/2',
    'https://www.tiktok.com/@c/video/3',
    'https://www.tiktok.com/@d/video/4',
    'https://www.tiktok.com/@e/video/5',
    'https://www.tiktok.com/@f/video/6',
    'https://www.tiktok.com/@g/video/7'
  ].join('\n');
  const parsed = U.parseRstLinks(texte);
  assert.equal(U.RST_LIENS_MAX, 6);
  assert.equal(parsed.links.length, 6);
  assert.deepEqual(parsed.duplicates, ['https://www.tiktok.com/@a/video/1']);
  assert.ok(parsed.errors.some(e => e.error === 'Domaine TikTok requis'));
});

test('RsT multiple : les travaux survivent à une actualisation', () => {
  const values = new Map();
  const storage = {
    setItem: (key, value) => values.set(key, value),
    getItem: key => values.get(key) || null,
    removeItem: key => values.delete(key)
  };
  assert.equal(U.RST_JOBS_KEY, 'vesper.rstJobs.v1');
  assert.deepEqual(U.loadRstJobs(storage), []);

  U.saveRstJobs(storage, [
    { id: 'job-1', lien: 'https://www.tiktok.com/@a/video/1', titre: 'RsT · @a' },
    { id: '', lien: 'ignoré' },
    { id: 'job-2', lien: 'https://www.tiktok.com/@b/video/2', titre: 'RsT · @b' }
  ]);
  const repris = U.loadRstJobs(storage);
  assert.equal(repris.length, 2);
  assert.equal(repris[0].id, 'job-1');
  assert.equal(repris[1].titre, 'RsT · @b');

  U.clearRstJobs(storage);
  assert.deepEqual(U.loadRstJobs(storage), []);
});
