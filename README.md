# wan-watchdog

Monitor services behind an AT&T BGW320, restart the gateway when local probes
indicate a network fault, and get notified when service fails or recovers.

The local watchdog runs in Docker. It checks its own public health endpoint,
the container, the reverse proxy, and outbound connectivity before deciding
whether to request a reboot. An optional Cloudflare Worker checks from outside
the home, so a total WAN outage can still produce an alert.

## Start here

```sh
git clone https://github.com/mggarofalo/wan-watchdog.git
cd wan-watchdog
```

Follow the [installation guide](docs/setup.md) to configure the gateway,
reverse proxy, public health hostname, and persistent state directory. The
Compose template uses the `stable` image, starts in dry-run mode, and leaves
scheduled reboots disabled.

Once configured:

```sh
docker compose up -d wan-watchdog
docker compose logs -f wan-watchdog
```

Enable real reboots only after checking the probe results. Keep scheduled
reboots off unless you deliberately want them: a reboot can interrupt a
working connection and is not guaranteed to restore it.

## Two complementary monitors

| Component | Responsibility | Alerts |
| --- | --- | --- |
| Local watchdog | Diagnose failures and request rate-limited software reboots | Local diagnosis, reboot decisions, confirmed recovery |
| [Cloud monitor](cloud-monitor/README.md) | Check public reachability every five minutes | One outage alert after two failures; recovery after three successes |

Local notifications are deduplicated by event within an incident and survive
container restarts. They cannot be delivered while outbound internet is down.
Neither monitor can hard power-cycle the gateway or prove from a failed public
request alone that the gateway caused the outage.

## Guides

- [Install and verify](docs/setup.md)
- [Configuration reference](docs/configuration.md)
- [Notifications and recovery](docs/notifications.md)
- [Operate, upgrade, and troubleshoot](docs/operations.md)
- [Probe logic and architecture](docs/architecture.md)
- [Development and image releases](docs/development.md)
- [Deploy the Cloudflare monitor](cloud-monitor/README.md)

Container: `ghcr.io/mggarofalo/wan-watchdog` · `linux/amd64` and `linux/arm64`

[MIT license](LICENSE)
