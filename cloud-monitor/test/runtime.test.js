import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Miniflare, Response, convertV4MiniflareOptions } from 'miniflare';
import worker from '../src/index.js';

test('real Workers runtime persists one incident and has no public check endpoint', async () => {
  let notifications = 0;
  let healthy = false;
  const runtime = new Miniflare(convertV4MiniflareOptions({
    name: 'wan-watchdog-monitor',
    modules: true,
    scriptPath: '.build/index.js',
    compatibilityDate: '2026-10-01',
    durableObjects: { MONITOR: { className: 'WanMonitor', useSQLite: true } },
    bindings: { HEALTH_URL: 'https://home.test/healthz', NTFY_URL: 'https://ntfy.test/topic' },
    outboundService: request => {
      const target = new URL(request.url);
      if (target.host === 'home.test') {
        return healthy
          ? Response.json({ status: 'ok', instance: 'restarted', version: '1.0', ts: Date.now() / 1000 })
          : new Response('', { status: 522 });
      }
      assert.equal(target.host, 'ntfy.test');
      assert.equal(target.pathname, '/topic');
      assert.equal(request.method, 'POST');
      notifications += 1;
      return new Response('{}');
    },
  }));
  try {
    const publicResponse = await runtime.dispatchFetch('https://worker.test/check', { method: 'POST' });
    assert.equal(publicResponse.status, 404);
    const namespace = await runtime.getDurableObjectNamespace('MONITOR');
    const stub = namespace.get(namespace.idFromName('home'));
    const start = Math.floor(Date.now() / 300000) * 300000;
    const check = async slot => {
      const response = await stub.fetch('https://monitor.internal/check', {
        method: 'POST', body: JSON.stringify({ scheduledTime: start + slot * 300000 }),
      });
      assert.equal(response.status, 200);
      return response.json();
    };
    assert.equal((await check(0)).incidentId, null);
    const opened = await check(1);
    assert.ok(opened.incidentId);
    await runtime.unsafeEvictDurableObject('wan-watchdog-monitor', 'WanMonitor', { name: 'home' });
    const results = await Promise.all([check(2), check(2), check(2)]);
    for (const result of results) assert.equal(result.incidentId, opened.incidentId);
    assert.equal(notifications, 1);
    assert.equal(opened.pendingNotifications, 0);
    healthy = true;
    await worker.scheduled({ scheduledTime: start + 3 * 300000 }, { MONITOR: namespace });
    await check(4);
    assert.equal(notifications, 1);
    assert.equal((await check(5)).incidentId, null);
    assert.equal(notifications, 2);
  } finally {
    await runtime.dispose();
  }
});
