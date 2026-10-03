# How the watchdog decides

[Home](../README.md) · [Configuration](configuration.md) · [Operations](operations.md)

The original symptom was asymmetric reachability: services became unreachable
through Cloudflare while outbound internet still worked. Gateway/IP Passthrough
state is a possible cause, but these probes do not prove a particular firmware
bug. Software reboots can help some failures and can also fail to restore service.

## Request paths

```text
Local watchdog → Cloudflare → gateway → downstream router → proxy → /healthz
       ├──────→ local health server
       ├──────→ proxy directly over LAN
       └──────→ internet HTTP endpoint / literal-IP TCP fallbacks

Cloudflare Cron → Durable Object → public /healthz
                       └────────→ ntfy → phone
```

The local and cloud monitors share a health endpoint, not incident state or a
reboot control channel. Only the local watchdog can request gateway restarts.

## Local probes

| Probe | Evidence |
| --- | --- |
| `external` | Public response; successful JSON is checked against this process's instance ID |
| `local` | The health server answers inside the container |
| `proxy` | The configured virtual host answers through the LAN proxy |
| `outbound` | HTTP internet access; on failure, TCP to `1.1.1.1:443` and `8.8.8.8:53` |

The public response uses cache-busting requests; the server sends no-store
headers. Local/proxy checks establish reachability, not application correctness:
ordinary HTTP errors such as 401 and 404 generally count as a responding origin.

## Decision order

An OK external probe yields `healthy`. Otherwise, special failures take priority:

- Cloudflare 525/526 → `proxy_down`: origin TLS needs attention.
- Wrong instance or client-side TLS failure → `local_fault`.
- DNS failure while outbound works → `local_fault`.

For other external failures:

| Additional evidence | Verdict | Reboot eligible? |
| --- | --- | --- |
| Local health check fails | `app_down` | No |
| Local proxy has a transport/origin failure | `proxy_down` | No |
| Outbound checks fail | `wan_down` | Yes |
| Outbound works and earlier checks did not identify another fault | `inbound_broken` | Yes |

Eligibility still requires the configured failure threshold and reboot cooldown.
Changing the verdict resets the failure streak. Omitting the local proxy probe
removes an important way to distinguish proxy failures from inbound path failures.

## Persistence and limits

`State` stores reboot timestamps/history and notification state in an atomically
replaced JSON file. The independent health-server thread continues responding
while probes or the post-reboot grace wait are in progress.

The cloud monitor validates fresh health JSON but does not pin an instance ID
across container restarts. It detects endpoint unavailability, not its root cause.
Neither monitor provides power control, and the external check still depends on
Cloudflare being operational. See [cloud implementation and setup](../cloud-monitor/README.md).
