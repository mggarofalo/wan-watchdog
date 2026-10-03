# Install the local watchdog

[Home](../README.md) · [Configuration](configuration.md) · [Operations](operations.md)

## Prerequisites

- Docker Compose on a host that can reach the gateway and reverse proxy.
- A BGW320 management address and its device access code (not the Wi-Fi password).
- A public health hostname proxied through Cloudflare to your home reverse proxy.
- A working origin TLS certificate and an uncached `/healthz` route.

## 1. Configure Compose

Copy your deployment settings into `docker-compose.override.yml`, which is
gitignored and loaded automatically by Compose. Replace the example values:

```yaml
services:
  wan-watchdog:
    environment:
      BGW_GATEWAY_HOST: "192.168.1.254"
      BGW_ACCESS_CODE: "YOUR_DEVICE_ACCESS_CODE"
      WATCHDOG_EXTERNAL_URL: "https://health.example.com/healthz"
      WATCHDOG_PROXY_URL: "http://192.168.1.10/healthz"
      WATCHDOG_PROXY_HOST: "health.example.com"
```

The base template contains placeholders for these five values. Keep
`WATCHDOG_DRY_RUN: "true"` during setup and `WATCHDOG_SCHEDULED_REBOOT_DAYS: 0`.
Settings are literal values; no `.env` file is required. Other ignored filenames
such as `docker-compose.deploy.yml` require explicit `-f` options to load.

The image runs as UID 10001. On a Linux Docker host, prepare the bind-mounted
state directory before starting it:

```sh
mkdir -p data
sudo chown 10001:10001 data
docker compose pull wan-watchdog
```

Use equivalent writable-directory permissions for other Docker hosts. Preserve
`./data`: it contains the reboot cooldown and notification state.

## 2. Connect the reverse proxy

Use [the nginx example](../nginx/health.conf.example) as a starting point for
the public HTTPS route. Send it to Docker-host port **3000**, or to
`wan-watchdog:8080` when nginx shares the container's Docker network.

Set the health DNS record to **proxied**, and bypass caching for `/healthz`.
A DNS-only record may test a local hairpin path instead of public inbound access.

The local proxy probe needs a separate direct LAN path. `WATCHDOG_PROXY_URL`
selects the proxy's address; `WATCHDOG_PROXY_HOST` selects its virtual host.
Serve `/healthz` directly on the proxy's LAN HTTP listener. Do not redirect this
probe to the public hostname: that would make the local check depend on the WAN.
The bundled example's port-80 server redirects everything, so replace that
behavior for `/healthz`, for example inside the matching HTTP server:

```nginx
location = /healthz {
    proxy_pass http://192.168.1.10:3000;
    proxy_set_header Host $host;
    proxy_no_cache 1;
    proxy_cache_bypass 1;
}
```

Use your actual upstream address. Keep redirects for other paths in a separate
`location /` block rather than a server-wide `return`.

## 3. Verify before starting the service

With the regular watchdog container stopped:

```sh
docker compose run --rm --service-ports wan-watchdog --test
```

`--service-ports` publishes port 3000 so the public round trip can reach this
one-off container. Do not run it alongside the normal container on the same port.
Test mode checks gateway access and runs the probes without rebooting or sending
notifications. It can update the persisted probe state.

Look for a healthy verdict and an accepted access code, then start dry-run mode:

```sh
docker compose up -d wan-watchdog
docker compose logs -f wan-watchdog
```

Once the readings are correct, set `WATCHDOG_DRY_RUN: "false"` in the override
and run `docker compose up -d wan-watchdog` again to apply it. A plain restart
does not apply changed environment variables.

## 4. Add notifications

Configure [local diagnostics](notifications.md) and, for total WAN outages,
deploy the [external Cloudflare monitor](../cloud-monitor/README.md).
