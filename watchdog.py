#!/usr/bin/env python3
"""Detect the inbound-path failure on an AT&T BGW320 and restart the gateway.

The failure this targets is asymmetric: the gateway keeps routing *outbound*
traffic perfectly while its IP Passthrough binding to the downstream router
goes stale, so traffic arriving from the internet is dropped at the gateway.
From inside the house nothing looks wrong -- browsing works, the dynamic-DNS
sync keeps reporting the correct address -- but Cloudflare can no longer reach
the origin and starts serving 5xx.

The watchdog serves its own health endpoint and then calls it back over the
public internet, so the probe target is itself rather than some unrelated
service that might be down for its own reasons. Four probes run each cycle:

    external : fetch our own public health URL. The request leaves the house,
               reaches Cloudflare's edge and comes back in through the gateway
               and the reverse proxy, exercising the whole inbound path. The
               instance id in the reply is checked, so a cached response or a
               different backend cannot pass for success.
    local    : fetch the health endpoint directly, proving this process serves.
    proxy    : fetch it through the local reverse proxy, proving nginx is up.
               Without this, an nginx outage looks exactly like a gateway fault.
    outbound : fetch a known-good internet endpoint, proving the WAN is up.

A reboot is only issued when the evidence actually implicates the gateway.

Configuration is entirely by environment variable, for Docker Compose.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import health  # noqa: E402
from bgw320 import Gateway, GatewayError  # noqa: E402

LOG = logging.getLogger("wan-watchdog")

# Set by SIGTERM/SIGINT. Every wait in this process goes through the event
# rather than time.sleep(), so a stop takes effect immediately instead of at the
# end of the current interval -- which matters most for the seven-minute
# post-reboot grace.
_STOP = threading.Event()


def _install_signal_handlers() -> None:
    """Make `docker stop` exit promptly.

    The container runs python as PID 1, and the kernel applies no default action
    for SIGTERM to PID 1: a process there dies from it only if it installs a
    handler. Python installs one for SIGINT but not SIGTERM, so without this the
    watchdog ignores the stop signal outright and Docker waits out the full
    grace period before resorting to SIGKILL.
    """
    def _stop(signum: int, _frame) -> None:
        LOG.info("received %s — shutting down", signal.Signals(signum).name)
        _STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)


# Cloudflare returns these when it could not get a usable response from the
# origin at all. Ordinary 4xx from the app itself means the origin is reachable.
ORIGIN_UNREACHABLE = {502, 504, 520, 521, 522, 523, 524}

# Cloudflare DID reach the origin on these: the TCP connection succeeded and
# TLS then failed -- 525 is a failed handshake, 526 an invalid or expired
# certificate. Both are positive proof that inbound packets are flowing, so
# they must never be read as a gateway fault. The certificate belongs to the
# reverse proxy, and rebooting the gateway cannot renew it.
ORIGIN_TLS_ERROR = {525, 526}

# How a probe failed. Only TRANSPORT and ORIGIN failures are evidence against
# the gateway. A certificate that will not validate, a name that will not
# resolve, or a reply from the wrong backend are all problems a gateway reboot
# cannot fix -- and it would take the house offline for three minutes to prove
# it.
KIND_OK = "ok"
KIND_ORIGIN = "origin_unreachable"
KIND_ORIGIN_TLS = "origin_tls"  # reached the origin, its certificate failed
KIND_TRANSPORT = "transport"
KIND_TLS = "tls"
KIND_DNS = "dns"
KIND_MISMATCH = "instance_mismatch"

BLAMES_NETWORK = {KIND_ORIGIN, KIND_TRANSPORT}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    gateway_host: str
    access_code: str
    external_url: str
    local_url: str
    proxy_url: str
    proxy_host_header: str
    outbound_url: str
    port: int
    bind: str
    check_interval: int
    probe_timeout: int
    failures_before_reboot: int
    min_seconds_between_reboots: int
    post_reboot_grace: int
    scheduled_reboot_days: int
    state_file: str
    notify_url: str
    status_token: str
    startup_delay: int
    dry_run: bool
    verify_instance: bool


def load_config(require_code: bool = True) -> Config:
    port = _env_int("WATCHDOG_PORT", 8080)
    cfg = Config(
        gateway_host=_env("BGW_GATEWAY_HOST", "192.168.1.254"),
        access_code=_env("BGW_ACCESS_CODE"),
        external_url=_env("WATCHDOG_EXTERNAL_URL"),
        local_url=_env("WATCHDOG_LOCAL_URL", f"http://127.0.0.1:{port}/healthz"),
        proxy_url=_env("WATCHDOG_PROXY_URL"),
        proxy_host_header=_env("WATCHDOG_PROXY_HOST"),
        outbound_url=_env("WATCHDOG_OUTBOUND_URL", "https://1.1.1.1/cdn-cgi/trace"),
        port=port,
        bind=_env("WATCHDOG_BIND", "0.0.0.0"),
        check_interval=_env_int("WATCHDOG_CHECK_INTERVAL", 60),
        probe_timeout=_env_int("WATCHDOG_PROBE_TIMEOUT", 20),
        failures_before_reboot=_env_int("WATCHDOG_FAILURES_BEFORE_REBOOT", 5),
        min_seconds_between_reboots=_env_int("WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS", 21600),
        post_reboot_grace=_env_int("WATCHDOG_POST_REBOOT_GRACE", 420),
        scheduled_reboot_days=_env_int("WATCHDOG_SCHEDULED_REBOOT_DAYS", 0),
        state_file=_env("WATCHDOG_STATE_FILE", "/data/state.json"),
        notify_url=_env("WATCHDOG_NOTIFY_URL"),
        status_token=_env("WATCHDOG_STATUS_TOKEN"),
        startup_delay=_env_int("WATCHDOG_STARTUP_DELAY", 15),
        dry_run=_env_bool("WATCHDOG_DRY_RUN"),
        verify_instance=_env_bool("WATCHDOG_VERIFY_INSTANCE", True),
    )
    if not cfg.external_url:
        raise SystemExit("WATCHDOG_EXTERNAL_URL is required "
                         "(e.g. https://health.example.com/healthz)")
    if require_code and not cfg.access_code:
        raise SystemExit("BGW_ACCESS_CODE is required to issue reboots")
    return cfg


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    status: int | None = None
    kind: str = KIND_OK


@dataclass
class State:
    consecutive_failures: int = 0
    last_reboot_ts: float = 0.0
    reboot_count: int = 0
    last_reboot_reason: str = ""
    last_verdict: str = ""
    last_check_ts: float = 0.0
    history: list = dataclass_field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.__dict__, indent=2))
            tmp.replace(path)  # atomic: a crash cannot truncate the state
        except OSError as exc:
            LOG.warning("could not persist state to %s: %s", path, exc)


def _classify(reason: object) -> str:
    if isinstance(reason, ssl.SSLError):
        return KIND_TLS
    if isinstance(reason, socket.gaierror):
        return KIND_DNS
    return KIND_TRANSPORT


def http_probe(
    name: str,
    url: str,
    timeout: int,
    cache_bust: bool = False,
    host_header: str = "",
    expect_instance: str = "",
) -> Probe:
    """Fetch `url` and report whether the far end produced a real response.

    "ok" means the origin answered, not that it answered 200: a 404 or a 401
    still proves the request arrived. Only transport failures and Cloudflare's
    origin-unreachable codes count as down.

    When `expect_instance` is given the JSON body must carry that instance id.
    A 200 from the wrong instance means something other than this container
    answered, which is a routing problem rather than a gateway problem.
    """
    target = url
    if cache_bust:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        target = f"{url}{sep}_wd={uuid.uuid4().hex[:12]}"

    headers = {
        "User-Agent": "wan-watchdog/1.0",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if host_header:
        headers["Host"] = host_header

    req = urllib.request.Request(target, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            if expect_instance:
                return _verify_instance(name, resp.status, body, expect_instance)
            return Probe(name, True, f"HTTP {resp.status}", resp.status, KIND_OK)
    except urllib.error.HTTPError as exc:
        if exc.code in ORIGIN_UNREACHABLE:
            return Probe(name, False, f"HTTP {exc.code} (origin unreachable)",
                         exc.code, KIND_ORIGIN)
        if exc.code in ORIGIN_TLS_ERROR:
            return Probe(name, False,
                         f"HTTP {exc.code} (origin reached, its TLS failed — "
                         "the inbound path is working)", exc.code, KIND_ORIGIN_TLS)
        return Probe(name, True, f"HTTP {exc.code} (origin responded)", exc.code, KIND_OK)
    except urllib.error.URLError as exc:
        return Probe(name, False, f"unreachable: {exc.reason}", None, _classify(exc.reason))
    except (TimeoutError, socket.timeout):
        return Probe(name, False, f"timed out after {timeout}s", None, KIND_TRANSPORT)
    except ssl.SSLError as exc:
        return Probe(name, False, f"TLS error: {exc}", None, KIND_TLS)
    except OSError as exc:
        return Probe(name, False, f"socket error: {exc}", None, KIND_TRANSPORT)


def _verify_instance(name: str, status: int, body: bytes, expected: str) -> Probe:
    try:
        got = json.loads(body.decode("utf-8", errors="replace")).get("instance", "")
    except ValueError:
        return Probe(name, False, f"HTTP {status} but body was not JSON — "
                     "something other than the watchdog answered",
                     status, KIND_MISMATCH)
    if got != expected:
        return Probe(name, False,
                     f"HTTP {status} from instance {got or '(none)'}, expected "
                     f"{expected} — a cache or another backend answered",
                     status, KIND_MISMATCH)
    return Probe(name, True, f"HTTP {status} (instance verified)", status, KIND_OK)


def tcp_probe(name: str, host: str, port: int, timeout: int) -> Probe:
    """Prove reachability with a bare TCP connect.

    Immune to certificate and DNS problems when given a literal address, which
    makes it the reliable last word on whether outbound connectivity exists.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return Probe(name, True, f"TCP {host}:{port} connected", None, KIND_OK)
    except OSError as exc:
        return Probe(name, False, f"TCP {host}:{port} failed: {exc}", None, KIND_TRANSPORT)


# Literal addresses, so this keeps working when DNS is the thing that broke.
OUTBOUND_TCP_FALLBACKS = [("1.1.1.1", 443), ("8.8.8.8", 53)]


def outbound_probe(url: str, timeout: int) -> Probe:
    """Decide whether the house has working internet access.

    The HTTP check is tried first because it is the strongest signal. If it
    fails for any reason -- including a certificate this host cannot validate
    -- fall back to raw TCP before concluding the WAN is down. Declaring "no
    internet" wrongly is expensive here: it reboots the gateway.
    """
    probe = http_probe("outbound", url, timeout)
    if probe.ok:
        return probe
    for host, port in OUTBOUND_TCP_FALLBACKS:
        fallback = tcp_probe("outbound", host, port, min(timeout, 10))
        if fallback.ok:
            return Probe("outbound", True,
                         f"{fallback.detail} (HTTP check failed: {probe.detail})",
                         None, KIND_OK)
    return Probe("outbound", False, f"{probe.detail}; TCP fallbacks also failed",
                 None, KIND_TRANSPORT)


def check_dns_is_proxied(external_url: str, public_ip: str | None) -> str:
    """Warn if the health hostname resolves straight to the house.

    A DNS-only record would make the probe hairpin back through the router
    instead of leaving the network, so it would report success even while the
    real inbound path was dead.
    """
    host = urllib.parse.urlparse(external_url).hostname
    if not host:
        return "could not parse a hostname out of WATCHDOG_EXTERNAL_URL"
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except OSError as exc:
        return f"DNS lookup for {host} failed: {exc}"
    if public_ip and public_ip in addrs:
        return (f"WARNING: {host} resolves to {public_ip}, your own public IP. "
                "The record is DNS-only, so the probe will hairpin instead of "
                "testing the real inbound path.")
    return f"{host} -> {', '.join(addrs)} (proxied; the probe will leave the network)"


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

HEALTHY = "healthy"
APP_DOWN = "app_down"
PROXY_DOWN = "proxy_down"
WAN_DOWN = "wan_down"
INBOUND_BROKEN = "inbound_broken"
LOCAL_FAULT = "local_fault"

REBOOT_WORTHY = {WAN_DOWN, INBOUND_BROKEN}


def evaluate(
    external: Probe,
    local: Probe | None,
    proxy: Probe | None,
    outbound: Probe,
) -> tuple[str, str]:
    """Classify the fault. Only returns a reboot-worthy verdict when the
    evidence actually points at the network path."""
    if external.ok:
        return HEALTHY, "external round trip verified"

    if external.kind == KIND_ORIGIN_TLS:
        # Cloudflare completed a TCP connection to the origin and only then
        # failed on TLS, which proves inbound delivery is working. The fault is
        # the certificate the reverse proxy presents -- very likely expired.
        # The plain-HTTP proxy probe cannot see this, so without this branch
        # the verdict would be inbound_broken and the gateway would be rebooted
        # to fix a certificate.
        return PROXY_DOWN, (
            f"external down ({external.detail}) — inbound reachability is fine, "
            "so this is the reverse proxy's TLS certificate. Check whether it "
            "has expired; a gateway reboot cannot fix it."
        )

    if external.kind == KIND_MISMATCH:
        return LOCAL_FAULT, (
            f"external probe answered but not by us ({external.detail}). "
            "Check the reverse proxy mapping and Cloudflare caching, not the gateway."
        )
    if external.kind == KIND_TLS:
        return LOCAL_FAULT, (
            f"external probe failed on TLS ({external.detail}). That is a "
            "certificate-trust problem here, not a gateway fault."
        )
    if external.kind == KIND_DNS and outbound.ok:
        return LOCAL_FAULT, (
            f"external probe failed on DNS ({external.detail}) while outbound "
            "is up. That is a resolver problem, not a gateway fault."
        )

    if local is not None and not local.ok:
        return APP_DOWN, (
            f"external down ({external.detail}) and the health endpoint is not "
            f"serving locally ({local.detail}) — this container is the problem"
        )
    if proxy is not None and not proxy.ok and proxy.kind in BLAMES_NETWORK:
        # nginx being down looks identical to a gateway fault from outside.
        # Without this branch the watchdog would reboot the gateway to fix a
        # web server on the same machine it is running on.
        return PROXY_DOWN, (
            f"external down ({external.detail}) but the container is healthy and "
            f"the local reverse proxy is failing ({proxy.detail}) — fix nginx"
        )
    if not outbound.ok:
        return WAN_DOWN, (
            f"external down ({external.detail}) and outbound is down "
            f"({outbound.detail}) — the whole WAN is offline"
        )
    return INBOUND_BROKEN, (
        f"external down ({external.detail}) while outbound is up, the container "
        "is serving and the proxy is fine — the inbound path through the "
        "gateway is broken"
    )


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def notify(cfg: Config, message: str) -> None:
    if not cfg.notify_url:
        return
    try:
        req = urllib.request.Request(
            cfg.notify_url,
            data=message.encode("utf-8"),
            headers={"User-Agent": "wan-watchdog/1.0", "Content-Type": "text/plain"},
        )
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # notification must never break the watchdog
        LOG.warning("notification failed: %s", exc)


def reboot_gateway(cfg: Config, state: State, reason: str, state_path: Path) -> bool:
    """Restart the gateway, honouring the rate limit. True if the request was sent."""
    now = time.time()
    since = now - state.last_reboot_ts
    if state.last_reboot_ts and since < cfg.min_seconds_between_reboots:
        LOG.error("FAULT PERSISTS (%s) but the last reboot was %.0f min ago and the "
                  "rate limit is %.0f min. Not rebooting — this needs a human.",
                  reason, since / 60, cfg.min_seconds_between_reboots / 60)
        notify(cfg, f"[wan-watchdog] fault persists after a recent reboot: {reason}")
        return False

    LOG.warning("REBOOTING GATEWAY: %s", reason)
    if cfg.dry_run:
        LOG.warning("WATCHDOG_DRY_RUN is set — not actually sending the restart")
        return False
    if not cfg.access_code:
        LOG.error("no BGW_ACCESS_CODE configured — cannot reboot")
        return False

    try:
        Gateway(cfg.gateway_host, cfg.access_code, cfg.probe_timeout).reboot()
    except GatewayError as exc:
        LOG.error("gateway restart FAILED: %s", exc)
        notify(cfg, f"[wan-watchdog] gateway restart FAILED: {exc}")
        return False

    state.last_reboot_ts = now
    state.reboot_count += 1
    state.last_reboot_reason = reason
    state.consecutive_failures = 0
    state.history = (state.history + [{"ts": now, "reason": reason}])[-20:]
    state.save(state_path)

    LOG.warning("restart sent; waiting %ds for the gateway to come back",
                cfg.post_reboot_grace)
    notify(cfg, f"[wan-watchdog] restarted the gateway: {reason}")
    return True


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


def run_cycle(cfg: Config, state: State, state_path: Path, instance_id: str,
              act: bool = True) -> str:
    t = cfg.probe_timeout

    external = http_probe(
        "external", cfg.external_url, t, cache_bust=True,
        expect_instance=instance_id if cfg.verify_instance else "",
    )
    local = http_probe("local", cfg.local_url, t) if cfg.local_url else None
    proxy = (
        http_probe("proxy", cfg.proxy_url, t, cache_bust=True,
                   host_header=cfg.proxy_host_header)
        if cfg.proxy_url else None
    )
    outbound = outbound_probe(cfg.outbound_url, t)

    for probe in (external, local, proxy, outbound):
        if probe is not None:
            LOG.info("  %-9s %-5s %s", probe.name,
                     "OK" if probe.ok else "DOWN", probe.detail)

    verdict, reason = evaluate(external, local, proxy, outbound)
    previous_verdict = state.last_verdict
    state.last_verdict = verdict
    state.last_check_ts = time.time()

    if verdict == HEALTHY:
        if state.consecutive_failures:
            LOG.info("recovered after %d consecutive failures", state.consecutive_failures)
        state.consecutive_failures = 0
        state.save(state_path)
        return verdict

    if verdict != previous_verdict:
        # Count each kind of fault separately. The reverse proxy holds the TLS
        # certificate, so an nginx restart reliably produces a run of failures;
        # letting those accumulate would mean a genuine gateway fault arriving
        # afterwards skipped its debounce entirely and rebooted immediately.
        state.consecutive_failures = 1
    else:
        state.consecutive_failures += 1
    state.save(state_path)
    LOG.warning("%s (%d/%d): %s", verdict, state.consecutive_failures,
                cfg.failures_before_reboot, reason)

    if verdict not in REBOOT_WORTHY:
        # A stopped container, a dead nginx or a broken CA bundle cannot be
        # fixed by rebooting the gateway. Report and stop.
        if state.consecutive_failures == cfg.failures_before_reboot:
            notify(cfg, f"[wan-watchdog] {verdict}, gateway not at fault: {reason}")
        return verdict

    if act and state.consecutive_failures >= cfg.failures_before_reboot:
        if reboot_gateway(cfg, state, reason, state_path):
            _STOP.wait(cfg.post_reboot_grace)
    return verdict


def maybe_scheduled_reboot(cfg: Config, state: State, state_path: Path) -> None:
    """Optional unconditional reboot on a fixed interval. Off by default.

    The probe-driven logic is strictly better: it reboots when something is
    actually wrong rather than on a timer.
    """
    if not cfg.scheduled_reboot_days:
        return
    interval = cfg.scheduled_reboot_days * 86400
    if not state.last_reboot_ts:
        # First run: anchor the schedule rather than rebooting immediately.
        state.last_reboot_ts = time.time()
        state.save(state_path)
        return
    if (time.time() - state.last_reboot_ts) < interval:
        return
    reboot_gateway(cfg, state,
                   f"scheduled reboot every {cfg.scheduled_reboot_days}d", state_path)
    _STOP.wait(cfg.post_reboot_grace)


def build_status_provider(cfg: Config, state: State, instance_id: str):
    def provider() -> dict:
        return {
            "status": "ok",
            "instance": instance_id,
            "last_verdict": state.last_verdict,
            "last_check_ts": state.last_check_ts,
            "consecutive_failures": state.consecutive_failures,
            "reboot_count": state.reboot_count,
            "last_reboot_ts": state.last_reboot_ts,
            "last_reboot_reason": state.last_reboot_reason,
            "history": state.history,
            "dry_run": cfg.dry_run,
        }
    return provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--test", action="store_true",
                        help="probe and report, but never reboot")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    _install_signal_handlers()

    cfg = load_config(require_code=not args.test)
    state_path = Path(cfg.state_file)
    state = State.load(state_path)
    instance_id = health.new_instance_id()

    health.serve_in_background(
        cfg.port, instance_id, build_status_provider(cfg, state, instance_id),
        cfg.status_token, cfg.bind,
    )

    if args.test:
        LOG.info("=== test mode: probes run, nothing is rebooted ===")
        gw = Gateway(cfg.gateway_host, cfg.access_code, cfg.probe_timeout)
        public_ip = None
        try:
            info = gw.broadband_status()
            public_ip = info.get("Broadband IPv4 Address") or None
            LOG.info("gateway: WAN %s, public IP %s, uptime %.1f days",
                     info.get("Broadband Connection"), public_ip,
                     (gw.uptime_seconds() or 0) / 86400)
        except GatewayError as exc:
            LOG.error("cannot read gateway status: %s", exc)
        if cfg.access_code:
            try:
                gw.login()
                LOG.info("access code accepted — a reboot could be issued if needed")
            except GatewayError as exc:
                LOG.error("ACCESS CODE CHECK FAILED: %s", exc)
        else:
            LOG.warning("no BGW_ACCESS_CODE set — reboots would not work")
        LOG.info(check_dns_is_proxied(cfg.external_url, public_ip))
        LOG.info("verdict: %s", run_cycle(cfg, state, state_path, instance_id, act=False))
        return 0

    if args.once:
        run_cycle(cfg, state, state_path, instance_id)
        return 0

    LOG.info("wan-watchdog starting: probing %s every %ds, rebooting after %d "
             "consecutive failures, at most once per %.1f h%s",
             cfg.external_url, cfg.check_interval, cfg.failures_before_reboot,
             cfg.min_seconds_between_reboots / 3600,
             "  [DRY RUN]" if cfg.dry_run else "")

    if cfg.startup_delay:
        # Give the reverse proxy and the network a moment after a host reboot,
        # so the first cycle does not count a cold start as a fault.
        LOG.info("waiting %ds before the first check", cfg.startup_delay)
        if _STOP.wait(cfg.startup_delay):
            return 0

    try:
        Gateway(cfg.gateway_host, cfg.access_code, cfg.probe_timeout).login()
        LOG.info("gateway access code verified")
    except GatewayError as exc:
        # Fail loudly now rather than at 3am when it actually matters.
        LOG.error("gateway login failed at startup: %s — reboots will not work", exc)

    while not _STOP.is_set():
        try:
            run_cycle(cfg, state, state_path, instance_id)
            maybe_scheduled_reboot(cfg, state, state_path)
        except Exception as exc:  # a watchdog that dies is worse than useless
            LOG.exception("unhandled error in check cycle: %s", exc)
        _STOP.wait(cfg.check_interval)

    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
