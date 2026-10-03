export const INTERVAL_MS = 5 * 60 * 1000;

function httpsUrl(value, name) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash) {
    throw new Error(`${name} must be HTTPS without credentials or a fragment`);
  }
  if (url.hostname === 'example.com' || url.hostname.endsWith('.example.com')) {
    throw new Error(`Configure ${name} before enabling the monitor`);
  }
  return url.toString();
}

function threshold(value, fallback) {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 100) {
    throw new Error('Monitor thresholds must be integers between 1 and 100');
  }
  return parsed;
}

export function configFromEnv(env) {
  return {
    healthUrl: httpsUrl(env.HEALTH_URL, 'HEALTH_URL'),
    ntfyUrl: httpsUrl(env.NTFY_URL, 'NTFY_URL'),
    ntfyToken: env.NTFY_TOKEN || '',
    failuresToOpen: threshold(env.FAILURES_TO_OPEN, 2),
    successesToClose: threshold(env.SUCCESSES_TO_CLOSE, 3),
  };
}

async function readHealth(response) {
  if (!response.body) throw new Error('Empty health response');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let size = 0;
  let text = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > 4096) throw new Error('Health response too large');
      text += decoder.decode(chunk.value, { stream: true });
    }
    return JSON.parse(text + decoder.decode());
  } finally {
    await reader.cancel();
  }
}

export async function probeHealth(config, fetcher = fetch, now = Date.now) {
  const target = new URL(config.healthUrl);
  target.searchParams.set('_monitor', crypto.randomUUID());
  try {
    const response = await fetcher(target.toString(), {
      redirect: 'manual',
      signal: AbortSignal.timeout(15000),
      headers: { 'Cache-Control': 'no-cache, no-store', 'Accept': 'application/json' },
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    if (response.status !== 200) {
      await response.body?.cancel();
      return { ok: false, detail: `HTTP ${response.status}` };
    }
    const body = await readHealth(response);
    if (body?.status !== 'ok' || typeof body.instance !== 'string' || !body.instance
        || typeof body.version !== 'string' || !body.version
        || !Number.isFinite(body.ts)) {
      return { ok: false, detail: 'Invalid watchdog health response' };
    }
    if (Math.abs(now() - body.ts * 1000) > 120000) {
      return { ok: false, detail: 'Stale health response or origin clock skew over 120s' };
    }
    return { ok: true, detail: 'Fresh watchdog health response' };
  } catch (error) {
    return { ok: false, detail: `Probe failed (${error.name || 'Error'})` };
  }
}

export function initialState() {
  return {
    lastSlot: -1,
    failures: 0,
    successes: 0,
    firstFailureAt: null,
    incident: null,
    lastCheck: null,
    lastClosed: null,
    outbox: [],
  };
}

function notification(incident, type, now, detail) {
  const start = new Date(incident.startedAt).toISOString();
  const duration = Math.round((now - incident.startedAt) / 60000);
  return {
    id: `${incident.id}:${type}`,
    type,
    title: type === 'outage' ? 'Home health endpoint unreachable' : 'Home health endpoint recovered',
    message: type === 'outage'
      ? `Incident ${incident.id}. First observed failure: ${start}. ${detail}. The external monitor cannot identify which home component failed.`
      : `Incident ${incident.id}. Recovery confirmed after ${duration} minutes since the first observed failure (${start}).`,
    attempts: 0,
    nextAttemptAt: now,
  };
}

export function recordProbe(state, probe, now, config) {
  state.lastCheck = { at: now, ...probe };
  if (probe.ok) {
    state.failures = 0;
    state.successes = Math.min(state.successes + 1, config.successesToClose);
    if (state.incident && state.successes >= config.successesToClose) {
      state.outbox.push(notification(state.incident, 'recovery', now));
      state.lastClosed = { ...state.incident, closedAt: now };
      state.incident = null;
    }
    if (!state.incident) state.firstFailureAt = null;
  } else {
    state.successes = 0;
    state.failures = Math.min(state.failures + 1, config.failuresToOpen);
    state.firstFailureAt ??= now;
    if (!state.incident && state.failures >= config.failuresToOpen) {
      state.incident = { id: crypto.randomUUID(), startedAt: state.firstFailureAt, openedAt: now };
      state.outbox.push(notification(state.incident, 'outage', now, probe.detail));
    }
  }
}

export class Monitor {
  constructor(storage, env, { fetcher = (...args) => fetch(...args), now = Date.now, log = console.log } = {}) {
    this.storage = storage;
    this.env = env;
    this.fetcher = fetcher;
    this.now = now;
    this.log = log;
    this.inFlight = null;
  }

  check(scheduledTime) {
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.run(scheduledTime).finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  async run(scheduledTime) {
    const config = configFromEnv(this.env);
    if (!Number.isSafeInteger(scheduledTime) || scheduledTime < 0) {
      throw new Error('Invalid scheduled time');
    }
    const state = await this.storage.get('state') || initialState();
    const slot = Math.floor(scheduledTime / INTERVAL_MS);
    if (slot > state.lastSlot) {
      const probe = await probeHealth(config, this.fetcher, this.now);
      recordProbe(state, probe, this.now(), config);
      state.lastSlot = slot;
      await this.storage.put('state', state);
      this.log(JSON.stringify({ event: 'check', ...state.lastCheck, incidentId: state.incident?.id ?? null }));
    }
    await this.deliver(state, config);
    return { incidentId: state.incident?.id ?? null, pendingNotifications: state.outbox.length };
  }

  async deliver(state, config) {
    for (let delivered = 0; delivered < 4 && state.outbox.length; delivered += 1) {
      const event = state.outbox[0];
      const now = this.now();
      if (event.nextAttemptAt > now) break;
      let accepted = false;
      let status = 'network_error';
      let retryAfter = 0;
      try {
        const headers = {
          'Content-Type': 'text/plain; charset=utf-8',
          'Title': event.title,
          'Priority': event.type === 'outage' ? 'high' : 'default',
          'Tags': event.type === 'outage' ? 'warning' : 'white_check_mark',
        };
        if (config.ntfyToken) headers.Authorization = `Bearer ${config.ntfyToken}`;
        const response = await this.fetcher(config.ntfyUrl, {
          method: 'POST', headers, body: event.message,
          redirect: 'manual', signal: AbortSignal.timeout(10000),
        });
        accepted = response.ok;
        status = response.status;
        const rawRetry = response.headers.get('Retry-After');
        if (rawRetry) {
          retryAfter = /^\d+$/.test(rawRetry) ? Number(rawRetry) * 1000 : Date.parse(rawRetry) - now;
          if (!Number.isFinite(retryAfter)) retryAfter = 0;
        }
        await response.body?.cancel();
      } catch {
        status = 'network_error';
      }
      if (accepted) {
        state.outbox.shift();
      } else {
        event.attempts += 1;
        const backoff = Math.min(3600000, INTERVAL_MS * 2 ** Math.min(event.attempts - 1, 4));
        event.nextAttemptAt = now + Math.max(backoff, retryAfter);
      }
      await this.storage.put('state', state);
      this.log(JSON.stringify({ event: 'notification', id: event.id, accepted, status }));
      if (!accepted) break;
    }
  }
}
