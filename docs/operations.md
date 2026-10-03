# Operations and troubleshooting

[Home](../README.md) · [Configuration](configuration.md) · [Probe logic](architecture.md)

## Everyday commands

```sh
docker compose ps
docker compose logs --tail=100 wan-watchdog
docker compose logs -f wan-watchdog
```

Docker's health check tests the local HTTP server only. A healthy container can
still report a WAN outage. Use the probe logs or authenticated `/status` to
inspect the network verdict.

To read gateway uptime and broadband state without authentication, run this on
the Docker host; the shell reads the configured gateway address inside the container:

```sh
docker compose exec wan-watchdog sh -c 'python3 /app/bgw320.py status --host "$BGW_GATEWAY_HOST"'
```

The standalone `bgw320.py` CLI does not read the watchdog's environment settings
automatically. Outside this command, supply `--host` yourself. Its `login` and
`reboot` subcommands also need `--code`; manual reboot prompts unless `--yes` is used.

## Upgrade

The recommended channel is `ghcr.io/mggarofalo/wan-watchdog:stable`.
Keep your existing override file and data directory:

```sh
docker compose pull wan-watchdog
docker compose up -d wan-watchdog
docker compose logs --tail=30 wan-watchdog
```

Use a full version such as `:1.1.0` or a digest to pin an exact release. `latest`
also includes successful development-branch builds. `stable` changes only on
final major/minor releases; a patch release does not move it. See
[release policy](development.md#image-tags).

For an existing deployment, explicitly set `WATCHDOG_SCHEDULED_REBOOT_DAYS: 0`
if you want timer-based restarts disabled. Updating the repository alone does
not change a running container. Compose `up -d` applies changed settings;
`docker compose restart` does not.

## Re-run setup checks

Test mode starts its own health server and needs the published port. Stop the
normal container first, then restart it when testing finishes:

```sh
docker compose stop wan-watchdog
docker compose run --rm --service-ports wan-watchdog --test
docker compose up -d wan-watchdog
```

It does not reboot or notify, but it can update persisted probe state. Do not
execute a second `watchdog.py --test` inside the already-running container:
both processes would try to bind the same health port.

## Read the failure, then inspect the right layer

| Symptom | Next check |
| --- | --- |
| `app_down` | Container health server and configured local URL |
| `proxy_down` | Proxy process, virtual-host mapping, origin TLS certificate |
| `local_fault` | DNS, client certificate trust, caching or wrong backend |
| `inbound_broken` | Public route through Cloudflare, gateway and downstream router |
| `wan_down` | Host/container routing, downstream WAN lease, gateway broadband status |
| `FAULT PERSISTS` | Recent reboot and cooldown; inspect why recovery failed |
| Notification failures | Topic permissions/token, outbound connection, HTTP endpoint |
| State cannot be persisted | Data directory ownership, mount and free disk space |

`wan_down` describes reachability from the watchdog, not proof that the ISP line
is physically down. The outbound probe tries literal-IP TCP connections after
HTTP failure, so this verdict is stronger evidence than a DNS failure alone.

If the gateway reports working broadband but the host still has no internet,
inspect the downstream router's WAN address, DHCP lease and default route.
If the gateway is unresponsive after a restart, a hard power cycle may be needed.
The software cannot provide that power cycle itself.

## Reboot and timing behavior

The defaults require five consecutive failures of the same verdict, then allow
at most one recorded restart per six hours. A restart request is followed by a
seven-minute grace period before normal monitoring resumes. The grace period
is a wait, not a positive confirmation of reboot or recovery.

Probes are sequential and the interval is a wait after the cycle. For example,
45 seconds of failed checks plus a 60-second interval gives roughly 105 seconds
between log batches. A log such as `6/5` means the failure count has exceeded
the action threshold; it is not a sixth reboot attempt that succeeded.

Scheduled reboots share the fault-recovery cooldown. A scheduled reboot can
therefore take a healthy network down and block another attempt until the
cooldown expires. Leave the timer off by default. Do not delete state to bypass
the cooldown: that also loses notification continuity and reboot history.

For missing cloud invocations or external alerts, use the
[Cloudflare troubleshooting guide](../cloud-monitor/README.md#verify-and-troubleshoot).
