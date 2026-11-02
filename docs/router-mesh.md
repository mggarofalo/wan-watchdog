# Router mesh monitor

[Home](../README.md) · [Configuration](configuration.md) · [Notifications](notifications.md)

An optional part of the local watchdog for an ASUS AiMesh router (ZenWiFi and
similar). It keeps the router's log and tells you when the router needs a
reboot. It never reboots the router itself.

## The fault it watches for

Sometimes the main router's wireless gets stuck. It restarts one radio every
few seconds, fails to bring it up, and tries again. The router log fills with:

```
kernel: Set wpa_cap for preauth error -1
kernel: Beacon set security failed
kernel: ADD/SET beacon failed
```

While this goes on, the satellites lose their link to the main router. The
satellite light changes (blinking blue on a ZenWiFi), and every device on that
satellite, or plugged into it, loses internet. Devices on the main router keep
working, so the network looks fine from some rooms and dead from others. A
router reboot clears it.

The loop writes about 180 lines a minute, and the router keeps only a few
thousand. After an hour, the start of the incident has gone from the router.

## What it does

| Part | Enabled by | Does |
| --- | --- | --- |
| Syslog receiver | `MESH_SYSLOG_PORT` | Writes every router log line to `/data/router-logs/router-YYYY-MM-DD.log` and counts the beacon-failure lines |
| Router API polling | `MESH_ROUTER_URL` + password | Each cycle, reads the AiMesh node list and router uptime: the data on the router's AiMesh page |

Either works alone. Both are checked every `MESH_CHECK_INTERVAL` (60 s by
default), whatever the gateway check interval is. The one exception: checks
pause while the watchdog waits out the grace period after a gateway reboot.

### Alerts

Alerts go to `WATCHDOG_NOTIFY_URL` with the ntfy title **Router mesh**. They
form their own incident, separate from the gateway's, so a healthy WAN check
never closes a router incident.

| Event | When | Priority |
| --- | --- | --- |
| `node_offline:<MAC>` | A satellite has been offline for `MESH_OFFLINE_CHECKS` consecutive checks | high |
| `syslog_pattern` | At least `MESH_SYSLOG_THRESHOLD` beacon failures in the last 5 minutes | high |
| `router_unreachable` | The router API has failed for `MESH_OFFLINE_CHECKS` consecutive checks | high |
| `login_failed` | The router rejected the credentials. Polling pauses for 15 minutes, because ASUS firmware locks the login after repeated failures | high |
| `router_rebooted` | Uptime went backwards. This is a one-off notice, not an incident. You also get it after rebooting the router yourself | default |
| `recovered` | Three healthy checks in a row after an incident | default |

Each event is sent once per incident. For the first `MESH_BOOT_GRACE` seconds
after the router boots (10 minutes by default), an offline satellite does not
count, because satellites take a few minutes to rejoin. A manual reboot
therefore does not page you.

## Set up

1. **Compose.** In `docker-compose.yml`, set `MESH_ROUTER_URL`,
   `MESH_ROUTER_USER` and `MESH_ROUTER_PASSWORD`, and keep the
   `"514:5514/udp"` port mapping. Run `docker compose up -d wan-watchdog`.
2. **Router.** In the router's web page, go to **System Log → General Log**. The
   **Remote Log Server** fields are on that page (older firmware has them under
   **Administration → System**). Enter the Docker host's LAN address, keep port
   514, and apply.
3. **Check.**
   - `docker compose exec wan-watchdog python3 /app/watchdog.py --mesh-test`
     should print `mesh: router ok` and list every node as `online`. This
     checks only the router and starts no servers, so it is safe to run
     inside the running container. A full `--test` is not: it needs the
     health port, so follow the procedure in [operations](operations.md).
   - Within a minute, `data/router-logs/` on the host should contain today's file.

Polling uses the router's mobile-app login, so it does not sign you out of the
web page. The password sits in the compose file, like `BGW_ACCESS_CODE`, so
keep filled-in copies out of git.

## Reading the logs

```sh
tail -f data/router-logs/router-$(date +%F).log
grep -c "Beacon set security failed" data/router-logs/router-*.log
```

Each line starts with the time the container received it, then the router's
address, then the router's own line. The router's clock has no year or time
zone and is wrong for a minute after boot, so trust the first timestamp.

A day's file stops growing at `MESH_LOG_MAX_MB_PER_DAY` and records a single
marker line when that happens. Counting for alerts carries on past the cap.
Files older than `MESH_LOG_RETENTION_DAYS` are deleted.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `MESH_ROUTER_URL` | Unset | Router web address, e.g. `http://192.168.1.1`. Unset turns polling off |
| `MESH_ROUTER_USER` | `admin` | Router admin user |
| `MESH_ROUTER_PASSWORD` | Unset | Required when `MESH_ROUTER_URL` is set |
| `MESH_CHECK_INTERVAL` | `60` | Seconds between mesh checks (minimum 10), independent of `WATCHDOG_CHECK_INTERVAL` |
| `MESH_OFFLINE_CHECKS` | `5` | Consecutive failing mesh checks before a satellite or API alert |
| `MESH_BOOT_GRACE` | `600` | Seconds after a router boot during which offline satellites are ignored |
| `MESH_SYSLOG_PORT` | `0` (template: `5514`) | UDP port to receive syslog on inside the container. `0` turns it off |
| `MESH_SYSLOG_PATTERN` | beacon-failure regex | Python regex for log lines that count toward the alert. Empty turns pattern alerts off |
| `MESH_SYSLOG_THRESHOLD` | `20` | Matches within 5 minutes that trigger `syslog_pattern` |
| `MESH_SYSLOG_ALLOWED_SOURCES` | Unset (any sender) | Comma-separated sender addresses to accept, e.g. `192.168.1.1`. Docker can rewrite the sender address, so check the second column of the log files before you set this. The first message from any other sender is logged, then ignored |
| `MESH_LOG_DIR` | `/data/router-logs` | Where daily log files go |
| `MESH_LOG_RETENTION_DAYS` | `30` | Days of files kept. `0` keeps them forever |
| `MESH_LOG_MAX_MB_PER_DAY` | `100` | Size cap for each day's file |

`/status` includes a `mesh` object with the last check's node list, router
uptime, syslog counters and current problems.
