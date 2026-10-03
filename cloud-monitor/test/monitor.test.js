import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Monitor, INTERVAL_MS, configFromEnv, probeHealth } from '../src/monitor.js';

const env = { HEALTH_URL: 'https://home.test/healthz', NTFY_URL: 'https://ntfy.test/topic' };
const baseTime = Date.UTC(2026, 9, 3);

function harness() {
  let saved;
  let now = baseTime;
  let healthy = false;
  let notifyStatus = 200;
  let probes = 0;
  const posts = [];
  const storage = {
    async get() { return structuredClone(saved); },
    async put(key, value) { saved = structuredClone(value); },
  };
  const fetcher = async (url, options) => {
    if (new URL(url).host === 'ntfy.test') {
      posts.push(options.body);
      return new Response('', { status: notifyStatus });
    }
    probes += 1;
    return healthy
      ? Response.json({ status: 'ok', instance: 'current', version: '1.0', ts: now / 1000 })
      : new Response('', { status: 522 });
  };
  const create = () => new Monitor(storage, env, { fetcher, now: () => now, log() {} });
  let monitor = create();
  return {
    posts,
    storage,
    state: () => structuredClone(saved),
    probes: () => probes,
    restart() { monitor = create(); },
    healthy(value) { healthy = value; },
    notifyStatus(value) { notifyStatus = value; },
    async tick(slot) {
      now = baseTime + slot * INTERVAL_MS;
      return monitor.check(now);
    },
  };
}

test('4.5-hour outage sends one alert, surviving object re-creation and repeated triggers', async () => {
  const fixture = harness();
  await fixture.tick(0);
  assert.equal(fixture.posts.length, 0);
  await fixture.tick(1);
  assert.equal(fixture.posts.length, 1);
  const incident = fixture.state().incident.id;
  fixture.restart();
  await fixture.tick(1);
  assert.equal(fixture.probes(), 2);
  for (let slot = 2; slot <= 54; slot += 1) await fixture.tick(slot);
  assert.equal(fixture.posts.length, 1);
  assert.equal(fixture.state().incident.id, incident);
});

test('brief recovery retains incident; three successes close and rearm it', async () => {
  const fixture = harness();
  await fixture.tick(0);
  await fixture.tick(1);
  const incident = fixture.state().incident.id;
  fixture.healthy(true);
  await fixture.tick(2);
  await fixture.tick(3);
  fixture.healthy(false);
  await fixture.tick(4);
  assert.equal(fixture.state().incident.id, incident);
  assert.equal(fixture.posts.length, 1);
  fixture.healthy(true);
  await fixture.tick(5);
  await fixture.tick(6);
  await fixture.tick(7);
  assert.equal(fixture.posts.length, 2);
  assert.match(fixture.posts[1], /Recovery confirmed after 35 minutes/);
  assert.equal(fixture.state().incident, null);
  await fixture.tick(8);
  assert.equal(fixture.posts.length, 2);
  fixture.healthy(false);
  await fixture.tick(9);
  await fixture.tick(10);
  assert.equal(fixture.posts.length, 3);
  assert.notEqual(fixture.state().incident.id, incident);
});

test('isolated failure resets without opening an incident', async () => {
  const fixture = harness();
  await fixture.tick(0);
  fixture.healthy(true);
  await fixture.tick(1);
  fixture.healthy(false);
  await fixture.tick(2);
  assert.equal(fixture.state().firstFailureAt, baseTime + 2 * INTERVAL_MS);
  assert.equal(fixture.posts.length, 0);
});

test('ntfy failure persists an ordered outbox and backs off across restarts', async () => {
  const fixture = harness();
  fixture.notifyStatus(503);
  await fixture.tick(0);
  await fixture.tick(1);
  await fixture.tick(2);
  fixture.restart();
  fixture.healthy(true);
  await fixture.tick(3);
  assert.equal(fixture.posts.length, 2);
  await fixture.tick(4);
  await fixture.tick(5);
  assert.deepEqual(fixture.state().outbox.map(event => event.type), ['outage', 'recovery']);
  fixture.notifyStatus(200);
  await fixture.tick(8);
  assert.equal(fixture.state().outbox.length, 0);
  assert.match(fixture.posts.at(-2), /First observed failure/);
  assert.match(fixture.posts.at(-1), /Recovery confirmed/);
  const delivered = fixture.posts.length;
  await fixture.tick(9);
  assert.equal(fixture.posts.length, delivered);
});

test('overlapping checks share work and stale trigger timestamps do not increment streaks', async () => {
  const fixture = harness();
  await Promise.all([fixture.tick(0), fixture.tick(0), fixture.tick(0)]);
  assert.equal(fixture.probes(), 1);
  await fixture.tick(1);
  await fixture.tick(0);
  assert.equal(fixture.probes(), 2);
  assert.equal(fixture.posts.length, 1);
});

test('failed state persistence cannot send an unrecorded notification', async () => {
  const fixture = harness();
  await fixture.tick(0);
  fixture.storage.put = async () => { throw new Error('disk unavailable'); };
  await assert.rejects(fixture.tick(1), /disk unavailable/);
  assert.equal(fixture.posts.length, 0);
});

test('health validation rejects redirects, wrong JSON, oversized and stale responses', async () => {
  const config = configFromEnv(env);
  const cases = [
    () => new Response('', { status: 302 }),
    () => new Response('<html>Login</html>'),
    () => Response.json({ status: 'ok' }),
    () => Response.json(null),
    () => new Response('x'.repeat(4097)),
    () => Response.json({ status: 'ok', instance: 'abc', version: '1', ts: 0 }),
    () => { throw new TypeError('network unreachable'); },
  ];
  for (const response of cases) {
    assert.equal((await probeHealth(config, response, () => baseTime)).ok, false);
  }
});

test('fresh health passes across instance changes and requests bypass cache', async () => {
  const targets = [];
  for (const instance of ['before-restart', 'after-restart']) {
    const result = await probeHealth(configFromEnv(env), async (url, options) => {
      targets.push(url);
      assert.equal(options.redirect, 'manual');
      assert.equal(options.cf.cacheTtl, 0);
      assert.equal(options.signal.aborted, false);
      assert.match(options.headers['Cache-Control'], /no-cache/);
      return Response.json({ status: 'ok', instance, version: '1', ts: baseTime / 1000 });
    }, () => baseTime);
    assert.equal(result.ok, true);
  }
  assert.notEqual(targets[0], targets[1]);
});

test('credentials and configuration errors fail before probing', () => {
  for (const overrides of [
    { HEALTH_URL: 'https://health.example.com/healthz' },
    { NTFY_URL: undefined },
    { NTFY_URL: 'http://ntfy.test/topic' },
    { HEALTH_URL: 'https://user:password@home.test/' },
    { FAILURES_TO_OPEN: '0' },
    { SUCCESSES_TO_CLOSE: 'NaN' },
  ]) assert.throws(() => configFromEnv({ ...env, ...overrides }));
});

test('notification uses bearer secret, honors Retry-After, and does not leak it to logs', async () => {
  let saved;
  let now = baseTime;
  let posts = 0;
  const logs = [];
  const storage = { async get() { return structuredClone(saved); }, async put(key, value) { saved = structuredClone(value); } };
  const monitor = new Monitor(storage, { ...env, NTFY_TOKEN: 'secret-token', FAILURES_TO_OPEN: '1' }, {
    now: () => now,
    log: message => logs.push(message),
    fetcher: async (url, options) => {
      if (options.method !== 'POST') return new Response('', { status: 522 });
      posts += 1;
      assert.equal(options.headers.Authorization, 'Bearer secret-token');
      assert.equal(options.redirect, 'manual');
      return new Response('', { status: 429, headers: { 'Retry-After': '1800' } });
    },
  });
  await monitor.check(now);
  now += INTERVAL_MS;
  await monitor.check(now);
  assert.equal(posts, 1);
  assert.equal(saved.outbox[0].nextAttemptAt, baseTime + 1800000);
  assert.equal(logs.join('').includes('secret-token'), false);
});
