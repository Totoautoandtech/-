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
