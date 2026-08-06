# wan-watchdog

Watches an AT&T BGW320 residential gateway for the failure where inbound
traffic stops arriving while everything else looks fine, and restarts the
gateway when — and only when — the evidence actually implicates it.

## The problem

Every week or two, Cloudflare starts returning 5xx for services hosted behind
the gateway. Rebooting the gateway fixes it. From inside the house nothing
looks wrong, which is what makes it awkward to catch, and impossible to fix
from a hotel room.

That combination is diagnostic. A 5xx from Cloudflare means Cloudflare could
not get a usable response from the origin. Meanwhile browsing from inside the
house keeps working, and any dynamic-DNS sync keeps reporting the correct
address, because both of those only exercise **outbound** traffic. The failure
is asymmetric: outbound is fine, inbound is dead.

When the gateway runs in **IP Passthrough** mode it hands the public address to
a router behind it. The binding that makes that work — the gateway's
association between the public address and the downstream router — is exactly
the state inbound delivery depends on. When it goes stale, packets arriving
from the ISP are dropped at the gateway while everything originating inside the
house continues normally. A reboot rebuilds the binding, which is why a reboot
always fixes it.

## Why not just reboot on a timer

A timer reboots whether or not anything is wrong, and does nothing at all when
the fault appears an hour after a scheduled reboot.

This reboots on evidence instead. The watchdog **serves its own health
endpoint and then calls it back over the public internet**, so the probe target
is itself rather than some unrelated service that might be down for its own
reasons. Four probes run each cycle:

| Probe | What it proves |
| --- | --- |
| `external` | Fetches the public health URL. The request leaves the network, reaches Cloudflare's edge and comes back in through the gateway and the reverse proxy — the whole inbound path, end to end. |
| `local` | Fetches the health endpoint directly, proving this container is serving. |
| `proxy` | Fetches it through the local reverse proxy, proving nginx is up. |
| `outbound` | Fetches a known-good internet endpoint, proving the WAN is up. |

The verdict follows from the combination:

| external | local | proxy | outbound | verdict | action |
| --- | --- | --- | --- | --- | --- |
| OK | – | – | – | `healthy` | none |
| down | down | – | – | `app_down` | alert — a reboot cannot fix a stopped container |
| down | OK | down | – | `proxy_down` | alert — fix nginx |
| down | OK | OK | down | `wan_down` | **reboot** |
| down | OK | OK | OK | `inbound_broken` | **reboot** — this is the fault above |

Four further cases never blame the gateway, because a reboot could not fix them
and would take the network offline for three minutes to prove it:

- a TLS certificate the host cannot validate,
- a hostname that will not resolve while outbound is otherwise fine,
- a 200 that came back carrying the **wrong instance id**, which means a cache
  or a different backend answered rather than this container,
- a Cloudflare **525 or 526**, which are not "origin unreachable" at all.
  Cloudflare returns those *after* completing a TCP connection to the origin
  and then failing on TLS, so they are positive proof that inbound delivery is
  working. The fault is the certificate the reverse proxy presents, usually
  expired. The `proxy` probe speaks plain HTTP and so cannot see this by
  itself; without the special case, an expired certificate would be diagnosed
  as `inbound_broken` and reboot the gateway.

### Why the proxy probe takes two settings

`WATCHDOG_PROXY_URL` is *where to send the packets* — the reverse proxy's LAN
address, so the request goes straight there without touching DNS, Cloudflare or
the gateway. `WATCHDOG_PROXY_HOST` is *which site it should serve*, sent as the
`Host` header.

Both are needed because nginx does name-based virtual hosting: one address and
port serving many sites, choosing the `server { }` block by matching `Host`
against `server_name`. Connecting by IP without the header means nginx matches
nothing and falls through to the default server — some other site entirely — so
the probe would report on the wrong virtual host and tell you nothing useful.

That last one is why the health endpoint returns an id generated at start-up
and the external probe checks it. Without that, a cached Cloudflare response
would look like a healthy round trip forever.

The blind timer is still available if you want it as a backstop: set
`WATCHDOG_SCHEDULED_REBOOT_DAYS`. It is off by default.

## The health endpoint

`GET /healthz` returns, with `Cache-Control: no-store`:

```json
{"status": "ok", "instance": "a1b2c3d4e5f60718", "ts": 1770000000, "version": "1.0"}
```

It deliberately exposes nothing about the network it protects. Richer detail —
current verdict, reboot history, failure counts — lives on `GET /status`, which
returns 404 unless `WATCHDOG_STATUS_TOKEN` is set, because it is reachable from
the public internet through the same reverse proxy.

## Quick start

```bash
git clone https://github.com/mggarofalo/wan-watchdog.git
cd wan-watchdog

$EDITOR docker-compose.yml    # fill in every value marked CHANGE ME

docker compose up -d
docker compose logs -f
```

Everything is a literal in `docker-compose.yml` — no `.env`, no shell
interpolation. That includes `BGW_ACCESS_CODE`, the **device access code**
printed on the label on the side of the gateway, which is the only secret this
needs. The copy in git holds a placeholder; keep your filled-in copy out of any
public repository. `docker-compose.override.yml`, `.deploy.yml` and `.local.yml`
are gitignored if you would rather keep your values in one of those.

Add a reverse-proxy entry so the endpoint is reachable at
`https://health.<your-domain>/healthz` — see
[`nginx/health.conf.example`](nginx/health.conf.example). Point a **proxied**
DNS record at it. If the record is DNS-only, the probe hairpins back through
your own router instead of leaving the network, and would report success even
while the real inbound path was dead. The watchdog warns about this on startup.

### Before trusting it

```bash
docker compose run --rm wan-watchdog --test
```

`--test` runs every probe, reports the verdict, verifies the access code is
accepted and checks that the hostname resolves to Cloudflare rather than to
you. It changes nothing.

`WATCHDOG_DRY_RUN` defaults to `"true"` in the template, which logs the reboot
decision without sending it. Leave it there through at least one real episode,
confirm it fires exactly when the fault appears and never otherwise, then set
it to `"false"`.

## Configuration

Everything is environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BGW_ACCESS_CODE` | — | Device access code from the gateway label. Required. |
| `BGW_GATEWAY_HOST` | `192.168.1.254` | Gateway management address. |
| `WATCHDOG_EXTERNAL_URL` | — | Your public health URL. Required. |
| `WATCHDOG_LOCAL_URL` | `http://127.0.0.1:$PORT/healthz` | Direct check of this container. |
| `WATCHDOG_PROXY_URL` | unset | Reverse proxy's LAN address — where to send the probe. Strongly recommended. |
| `WATCHDOG_PROXY_HOST` | unset | `Host` header, so nginx serves the right virtual host. |
| `WATCHDOG_OUTBOUND_URL` | `https://1.1.1.1/cdn-cgi/trace` | Known-good internet endpoint. |
| `WATCHDOG_PORT` | `8080` | Port the health endpoint listens on. |
| `WATCHDOG_CHECK_INTERVAL` | `60` | Seconds between cycles. |
| `WATCHDOG_PROBE_TIMEOUT` | `20` | Per-probe timeout in seconds. |
| `WATCHDOG_FAILURES_BEFORE_REBOOT` | `5` | Consecutive same-verdict failures required. |
| `WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS` | `21600` | Rate limit. |
| `WATCHDOG_POST_REBOOT_GRACE` | `420` | Pause after a restart. |
| `WATCHDOG_SCHEDULED_REBOOT_DAYS` | `0` | Unconditional reboot interval. `0` = off. |
| `WATCHDOG_STATE_FILE` | `/data/state.json` | Reboot history and rate-limit clock. |
| `WATCHDOG_NOTIFY_URL` | unset | Endpoint accepting a plain-text POST. |
| `WATCHDOG_STATUS_TOKEN` | unset | Enables `/status`. |
| `WATCHDOG_VERIFY_INSTANCE` | `true` | Check the instance id on the way back in. |
| `WATCHDOG_STARTUP_DELAY` | `15` | Grace before the first cycle. |
| `WATCHDOG_DRY_RUN` | `false` | Log reboot decisions without sending them. |

## Safety rails

- **Debounce** — the fault must persist for `WATCHDOG_FAILURES_BEFORE_REBOOT`
  cycles *of the same verdict*. Counting per verdict matters: the reverse proxy
  holds the TLS certificate, so an nginx restart reliably produces a run of
  failures, and letting those accumulate would mean a genuine gateway fault
  arriving afterwards skipped its debounce entirely.
- **Rate limit** — never more than one reboot per
  `WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS`. If the fault returns sooner the
  watchdog logs loudly and notifies rather than looping, because that means
  something else is wrong.
- **State survives restarts** — written atomically to a mounted volume, so
  restarting the container cannot reset the rate limit.
- **Never dies quietly** — unhandled errors in a cycle are logged and the loop
  continues; the container restarts if the process exits.

## Manual control

```bash
docker compose exec wan-watchdog python3 /app/bgw320.py status
```

```
HUMAX BGW320-500  fw 6.34.7
  uptime        : 3d 22h 55m
  PON Link Status         : OPERATION (O5)
  IP Passthrough          : On
```

`status` needs no authentication and is the quickest way to answer "how long
since the gateway last came up". `login` verifies the access code; `reboot`
restarts the gateway and prompts first.

## Development

```bash
python3 selftest.py            # everything
python3 selftest.py --offline  # deterministic subset, as CI runs it
```

No dependencies — the standard library only, so it runs on any Python 3.11+.
The gateway client is tested against captured pages in `testdata/`, which
preserve the real quirks of the device's HTML: labels repeated in a help
section with a trailing colon, and a nav menu whose links reuse the field
labels verbatim, so on that page only the *last* match is the actual value.

Images are built for `linux/amd64` and `linux/arm64` and published to
`ghcr.io/mggarofalo/wan-watchdog`.

## Reducing how often the fault happens

The watchdog handles the symptom. On the gateway itself, worth examining:

- **Firewall → IP Passthrough** — allocation mode should be `DHCPS-fixed`
  pinned to the downstream router's WAN MAC, not "first detected device".
  Pinning removes the ambiguity about which device the binding belongs to.
- **Firewall → Firewall Advanced** — in IP Passthrough the downstream router is
  doing the firewalling, so the gateway's own filtering is redundant. Its DoS
  protection is a plausible contributor: Cloudflare reaches an origin from many
  edge addresses at a high connection rate, which is exactly the traffic shape
  flood protection is built to suppress. If it is progressively blocking
  Cloudflare's edge, that would produce this symptom on this timescale, and a
  reboot would clear the block list. This is a hypothesis worth testing, not a
  confirmed cause.
- **Firewall → Packet Filter** — same reasoning.

If the interval between failures does not lengthen after those changes, the
remaining options are replacing the gateway or moving to a Cloudflare Tunnel,
which makes the inbound path irrelevant by having the origin hold an outbound
connection to Cloudflare's edge instead.

## License

MIT
