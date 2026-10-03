# Notifications

[Home](../README.md) · [Operations](operations.md) · [Cloud monitor](../cloud-monitor/README.md)

## Choose what to receive

| Source | What it knows | Delivery during a WAN outage |
| --- | --- | --- |
| Local watchdog | Probe diagnosis and reboot decisions | Queued until it can reach the notification service |
| Cloud monitor | Whether the public health endpoint is reachable | Independent of the home connection |

Both can publish to the same ntfy topic. Their incidents are tracked separately,
so one physical outage can produce messages from both. The phone needs cellular
or another functioning connection to receive an external alert during a home outage.

## Configure local diagnostics

Add these settings to your deployment override:

```yaml
services:
  wan-watchdog:
    environment:
      WATCHDOG_NOTIFY_URL: "https://ntfy.sh/YOUR_TOPIC"
      WATCHDOG_NOTIFY_TOKEN: ""
```

Subscribe to that topic in ntfy. Set the token only for a topic requiring bearer
authentication. Prefer a protected topic; an unprotected topic is readable by
anyone who knows its name. Keep the URL/token out of git and use HTTPS.
Apply changes with `docker compose up -d wan-watchdog`.

## Local event policy

After the existing probe failure threshold, an incident can produce:

- One message per diagnostic verdict: app, proxy, or local configuration fault.
- One message when the reboot cooldown blocks recovery.
- One message when a restart request fails, or when a missing access code prevents it.
- A notice **before** requesting a reboot, without claiming it succeeded. Repeated
  failed requests share a notice; a later request after a recorded reboot can notify again.
- One recovery message after **three consecutive healthy checks**.

A failed check resets the recovery streak. A brief recovery does not rearm the
event notifications. Once recovery is confirmed, a new incident can notify again.
Normal healthy checks are silent. `--test` sends nothing; dry-run mode suppresses
reboot-request notices but can still report diagnostic faults.

## Persistence and delivery

The local incident, event keys, pending messages and retry deadlines live in
`/data/state.json`. Preserve the mounted data directory across upgrades. Older
state files load with defaults for the new notification fields.

Local delivery retries in order, starting at one minute and backing off to one
hour; a longer `Retry-After` is respected. Requests time out after five seconds,
redirects are not followed, and up to four queued messages are attempted per
flush. Retries happen when the watchdog next processes its queue, not on a
separate timer. Delayed messages carry their original observation time and incident ID.

Emptying `WATCHDOG_NOTIFY_URL` disables sending and new entries. Existing queued
messages remain and can be delivered if notifications are re-enabled.

Deduplication prevents a new event message on every probe. It does **not** guarantee
exactly-once delivery: a lost response or crash after the server accepts a POST
can result in a duplicate retry. The cloud notifier has the same limitation,
with a separate queue and a five-minute initial retry interval.
