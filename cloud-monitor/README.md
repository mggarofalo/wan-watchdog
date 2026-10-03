# External WAN monitor

A Cloudflare Cron Trigger runs every five minutes (`*/5 * * * *`). One named
SQLite-backed Durable Object probes the public watchdog health endpoint, stores
incident state, and publishes transition notifications to ntfy. It keeps working
when the home WAN is down. Your phone still needs cellular or another working
internet connection to receive notifications.

## Incident policy

- Two consecutive failed checks open an incident and queue one outage alert.
- Further failures remain part of that incident, regardless of how long it lasts.
- Three consecutive successful checks close it and queue one recovery message.
- A failure during recovery resets the success streak, without opening another incident.
- State and pending notifications survive Worker deployments and object restarts.
- Duplicate or older scheduled slots cannot increment the failure/success streak.

With five-minute checks, detection normally takes 5–10 minutes and recovery
confirmation 10–15 minutes, plus probe duration and scheduling delay. These are
consecutive observations, not proof that connectivity was constant between checks.
The local Python watchdog retains its own check interval and reboot cooldown.

The probe requires HTTP 200 and watchdog JSON with `status`, `instance`, `version`
and a timestamp within 120 seconds of the monitor's clock. Keep the home host's
clock synchronized. Requests use unique query strings and bypass cache; also
configure the health path to bypass Cloudflare caching. Instance IDs may change
on container restart and are not pinned. This validates the response format and
freshness, not cryptographic origin identity.

## Deploy

Use Node.js 22 or newer and a Cloudflare account with Workers access. No new DNS
record, public Worker route, KV namespace or manually created database is needed.
Wrangler creates the SQLite-backed Durable Object namespace from the migration
in `wrangler.jsonc` and installs the five-minute Cron Trigger.

From this directory:

```sh
npm ci
npm test
npm run test:runtime
npx wrangler login
npx wrangler whoami
```

Complete the login in your browser using the intended Cloudflare account. If
you have multiple accounts, select the intended account when prompted; for
noninteractive deployment, set `CLOUDFLARE_ACCOUNT_ID` to that account's ID.

`HEALTH_URL` in `wrangler.jsonc` is configured for `https://health.wallingford.me/healthz`.
Change it when deploying for another home. Example hostnames are rejected at runtime. The Worker needs no
public route; `workers_dev` and preview URLs are disabled and its HTTP handler
always returns 404. Do not route the health hostname to this Worker.

On the current checkout, the supplied ntfy topic is already saved in the ignored
`.dev.vars` file. This file is local only: it is not committed and a plain
`npm run deploy` does not upload it. Upload it explicitly with the first deployment:

```sh
npm run deploy -- --secrets-file .dev.vars
```

On a new checkout, first create `.dev.vars` with your own values:

```dotenv
NTFY_URL="https://ntfy.sh/your-random-topic"
```

If the topic requires authentication, add `NTFY_TOKEN="your-access-token"` to
that same ignored file before deploying. Never commit this file. Subsequent
code-only updates can use `npm run deploy`; existing deployed secrets are retained.

`NTFY_URL` is the full topic URL, such as `https://ntfy.sh/your-random-topic`.
`NTFY_TOKEN` is optional: omit it for an unprotected topic. Prefer a
protected topic; otherwise choose an unguessable name, since public topics are
readable by anyone who knows the name. Subscribe in the ntfy phone app.

`npm run deploy` creates the Durable Object migration and enables the schedule.
Cron Trigger changes can take up to 15 minutes to propagate. Run `npm run tail`
to inspect structured check and delivery logs. Config errors cause the invocation to fail
without probing or sending misleading outage notifications. Inspect the first
successful scheduled check after deployment.

In the Cloudflare dashboard, open **Workers & Pages → wan-watchdog-monitor**:

- Under **Settings → Triggers → Cron Triggers**, verify `*/5 * * * *`.
- Under **Settings → Variables and Secrets**, verify `NTFY_URL` exists as a secret.
- Verify the `MONITOR` Durable Object binding points to `WanMonitor`.
- In logs, expect `event: "check"`, `ok: true`, and `incidentId: null` while healthy.

A quiet phone while healthy is expected; successful checks do not send alerts.
An accepted notification appears as `event: "notification", accepted: true`.
HTTP 401/403 usually means the topic token or permissions need attention; 429
backs off according to `Retry-After`. Network failures retry automatically.

To pause monitoring, set `triggers.crons` to `[]` in `wrangler.jsonc` and deploy.
This preserves incident state. Do not delete the Durable Object to stop alerts.
The GitHub workflow validates builds and tests; merging the PR does not deploy
the Worker. Deploy separately with Wrangler after review.

Before trusting the monitor, exercise it against a staging health endpoint:
keep it failing through two checks, verify one alert, keep it failing longer and
verify silence, then restore it through three checks and verify one recovery.
Runtime tests mock all outbound traffic and send no real notifications.

## Delivery behavior

Incident transitions and their pending messages are persisted together before
delivery. Successful ntfy responses remove the message from the durable outbox.
Failed requests retry on later cron runs with exponential backoff from five
minutes to one hour, respecting a longer `Retry-After`. Delivery stays in order;
up to four queued messages are attempted per run. A prolonged ntfy outage can
therefore produce delayed outage/recovery messages when it returns. Each message
includes its incident ID and original observation time.

This prevents an alert on every health check, but does not promise exactly-once
delivery: if ntfy accepts a POST and its response is lost, or the monitor stops
before recording acceptance, retrying can duplicate that notification. An incident
ID in message text is not receiver-side deduplication. Pending messages are retained
until accepted; repeated delivery failures are visible in logs.

## Responsibilities and limits

The cloud monitor reports **home health endpoint unreachable**, not **modem
broken**. DNS, TLS, the gateway, downstream router, proxy, and container can all
break the path. Local watchdog probes remain responsible for fault diagnosis and
bounded software reboots. This monitor cannot hard power-cycle the gateway.

The Compose template now disables scheduled gateway reboots. Existing deployments
must apply `WATCHDOG_SCHEDULED_REBOOT_DAYS: 0` and recreate their container to adopt
that change. The cloud monitor does not alter a deployed gateway or Docker host.

Expiring maintenance windows and automatic power control are not implemented.
A planned reboot is currently subject to the same two-failure threshold as any
other outage. A failure of Cloudflare itself may also prevent this monitor from
running; this is not an independent check of Cloudflare availability.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `HEALTH_URL` | `https://health.wallingford.me/healthz` | Public HTTPS watchdog health endpoint |
| `NTFY_URL` | Required secret | Full HTTPS notification topic URL |
| `NTFY_TOKEN` | Optional secret | Bearer token for protected topics |
| `FAILURES_TO_OPEN` | `2` | Failed observations before opening |
| `SUCCESSES_TO_CLOSE` | `3` | Successful observations before closing |

Do not rename the Worker, binding, class or the `home` object identity casually:
creating a fresh monitor loses the incident continuity and can alert again for an
already-open outage. Notification URLs and tokens are excluded from application logs.

References: [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[Durable Object storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/),
[ntfy publishing](https://docs.ntfy.sh/publish/).
