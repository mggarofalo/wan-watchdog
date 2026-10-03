# Local configuration reference

[Home](../README.md) · [Install](setup.md) · [Notifications](notifications.md)

All settings are environment variables. Defaults below are Python defaults;
the Compose template overrides the gateway address with a required placeholder
and starts with dry-run enabled. Empty strings disable optional probe URLs,
notifications and the status token; numeric settings use their defaults when empty.

## Gateway and probes

| Variable | Default | Purpose |
| --- | --- | --- |
| `BGW_GATEWAY_HOST` | `192.168.1.254` | Gateway management address |
| `BGW_ACCESS_CODE` | Required | Device access code for reboots; optional in `--test` |
| `WATCHDOG_EXTERNAL_URL` | Required | Public HTTPS health endpoint |
| `WATCHDOG_LOCAL_URL` | `http://127.0.0.1:8080/healthz` | Container check; default port follows `WATCHDOG_PORT` |
| `WATCHDOG_PROXY_URL` | Unset | Direct LAN proxy health URL; strongly recommended |
| `WATCHDOG_PROXY_HOST` | Unset | Host header for the local proxy's virtual host |
| `WATCHDOG_OUTBOUND_URL` | `https://1.1.1.1/cdn-cgi/trace` | Initial outbound HTTP check |
| `WATCHDOG_VERIFY_INSTANCE` | `true` | Verify the public response belongs to this process |
| `WATCHDOG_PORT` | `8080` | Health server port inside the container |
| `WATCHDOG_BIND` | `0.0.0.0` | Health server bind address |

The template maps host port `3000` to container port `8080`. If you change the
container port, update that mapping too. For an explanation of the checks,
see [probe logic](architecture.md).

## Timing and actions

| Variable | Default | Purpose |
| --- | --- | --- |
| `WATCHDOG_CHECK_INTERVAL` | `60` | Seconds to wait after a completed cycle |
| `WATCHDOG_PROBE_TIMEOUT` | `20` | HTTP/socket timeout in seconds |
| `WATCHDOG_FAILURES_BEFORE_REBOOT` | `5` | Consecutive same-verdict failures before acting |
| `WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS` | `21600` | Six-hour reboot cooldown |
| `WATCHDOG_POST_REBOOT_GRACE` | `420` | Pause after sending a restart request |
| `WATCHDOG_SCHEDULED_REBOOT_DAYS` | `0` | Unconditional periodic reboot; `0` disables it |
| `WATCHDOG_STARTUP_DELAY` | `15` | Initial delay before normal probes |
| `WATCHDOG_DRY_RUN` | `false` | Suppress restart requests; template sets `true` |

Probes run sequentially, so a cycle can take longer than `WATCHDOG_CHECK_INTERVAL`.
DNS resolution can also outlast the socket timeout. Five failures do not imply
exactly five minutes. Scheduled and fault-driven reboots share the same cooldown.

## State and notifications

| Variable | Default | Purpose |
| --- | --- | --- |
| `WATCHDOG_STATE_FILE` | `/data/state.json` | Reboot history, deduplication and pending notifications |
| `WATCHDOG_NOTIFY_URL` | Unset | Full ntfy topic URL or compatible plain-text POST endpoint |
| `WATCHDOG_NOTIFY_TOKEN` | Unset | Optional notification bearer token; use HTTPS |
| `WATCHDOG_STATUS_TOKEN` | Unset | Enable authenticated `/status`; unset returns 404 |

The [notification guide](notifications.md) covers retries and recovery. Notification
recovery requires three healthy checks; this count is independent of the reboot
failure threshold and is not currently configurable.

## HTTP endpoints

`GET /healthz` returns uncached JSON:

```json
{"status":"ok","instance":"a1b2c3d4e5f60718","ts":1791000000,"version":"1.1.0"}
```

The instance ID changes when the process restarts. This endpoint proves that
the health server answers, not that the WAN is healthy.

`GET /status` adds the last verdict/check, failure count, reboot history, current
notification incident ID and pending-message count. Authenticate with the
`X-Status-Token` header matching `WATCHDOG_STATUS_TOKEN`. A `token` query parameter
also works, but a header avoids placing the secret in URL logs. Keep this endpoint
disabled unless you need it; the public proxy may expose it.
