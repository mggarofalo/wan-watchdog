# External Cloudflare monitor

[Home](../README.md) · [Notifications](../docs/notifications.md) · [Development](../docs/development.md)

Checks the public health endpoint every five minutes and sends ntfy alerts even
when the home's WAN is offline. One SQLite-backed Durable Object owns incident
state, serializes overlapping checks and retains pending notifications.

## Alert policy

| Observation | Action |
| --- | --- |
| Two consecutive failures | Open an incident; queue one outage alert |
| Further failures | Keep the same incident; no new outage alert |
| Three consecutive successes | Close the incident; queue recovery and rearm |

A failure during recovery resets the success streak. Detection usually takes
5–10 minutes and recovery confirmation 10–15 minutes, plus check/scheduling delays.
These are observations at five-minute intervals, not continuous measurements.

The probe requires HTTP 200 and watchdog JSON with `status`, `instance`, `version`
and a timestamp within 120 seconds of the cloud clock. Keep the origin clock
synchronized and bypass caching for `/healthz`. Instance IDs may change on restart;
the cloud monitor does not authenticate origin identity cryptographically.

## Deploy

Use Node.js 22+ and a Cloudflare account with Workers access. From this directory:

```sh
npm ci
npm test
npm run test:runtime
npx wrangler login
npx wrangler whoami
```

Select the intended account. For noninteractive account selection, set
`CLOUDFLARE_ACCOUNT_ID`. Review `HEALTH_URL` in `wrangler.jsonc`; this repository's
configuration points at `https://health.wallingford.me/healthz`.

Create the gitignored `.dev.vars` file with your topic:

```dotenv
NTFY_URL="https://ntfy.sh/YOUR_TOPIC"
```

Add `NTFY_TOKEN="YOUR_ACCESS_TOKEN"` only if the topic requires authentication.
Subscribe to the topic on your phone. Then deploy code and secrets together:

```sh
npm run deploy -- --secrets-file .dev.vars
npm run tail
```

Wrangler creates the Worker, Durable Object namespace and cron schedule. No new
DNS record, public Worker route, KV namespace or manual database creation is needed.
Do not point the health hostname at this Worker: it must continue reaching home.
The Worker's public HTTP handler returns 404 by design.

`.dev.vars` is not automatically uploaded by a plain deployment. Subsequent
code-only updates can use `npm run deploy`, which preserves deployed secrets.
GitHub CI tests the Worker but does not deploy it on merge.

## Verify and troubleshoot

In **Workers & Pages → wan-watchdog-monitor**, verify the `MONITOR` binding,
the `NTFY_URL` secret, and cron `*/5 * * * *`. Use `npm run tail` for live checks:

- `event: "check", ok: true, incidentId: null` — healthy and intentionally silent.
- `event: "notification", accepted: true` — ntfy accepted a queued event.
- HTTP 401/403 on delivery — check the topic token/permissions.
- Configuration error — fix the missing/invalid URL or threshold; no probe runs.

New cron schedules can take **15 minutes** to propagate. **Past Cron Events**
can take **30 minutes** to display events for a new Worker. A blank dashboard
after five minutes does not establish that execution is broken.

For end-to-end verification, use a staging health endpoint: fail two checks,
confirm one alert and continued silence, then restore three checks and confirm
recovery. Automated tests send no real ntfy messages.

To pause, set `triggers.crons` to `[]` and deploy. This preserves incident state.
Do not delete or casually rename the Worker, class, binding or `home` object
identity: a new state store can announce an existing outage again.

## Configuration and boundaries

| Setting | Purpose |
| --- | --- |
| `HEALTH_URL` | Public HTTPS watchdog endpoint |
| `NTFY_URL` | Required secret: full HTTPS topic URL |
| `NTFY_TOKEN` | Optional secret: bearer token |
| `FAILURES_TO_OPEN` | Failed checks before opening; default `2` |
| `SUCCESSES_TO_CLOSE` | Successful checks before closing; default `3` |

Pending messages are persisted before delivery and retried in order, with
backoff from five minutes to one hour and support for a longer `Retry-After`.
At most four queued messages are attempted per run. Long notification-service
outages can produce delayed messages carrying original incident times.
Ambiguous responses or crashes can duplicate a retry; exactly-once delivery
is not guaranteed.

The alert means **home health endpoint unreachable**, not **modem broken**.
The monitor neither reboots nor power-cycles anything. Maintenance windows and
automatic power control are not implemented. Cloudflare outages can also prevent
this monitor from running.

References: [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[Durable Object storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/),
[ntfy publishing](https://docs.ntfy.sh/publish/).
