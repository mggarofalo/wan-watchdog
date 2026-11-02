"""Watch an ASUS AiMesh router: keep its syslog, poll its nodes, report faults.

The failure this targets: the main router's wireless gets stuck restarting a
radio ("Beacon set security failed" every few seconds), the satellites lose
their backhaul, and every device on or wired to a satellite goes offline while
devices on the main router carry on as normal. Only a router reboot clears it.

Three parts, all optional and independent:

    SyslogSink  : receives the router's remote syslog over UDP, writes it to
                  one file per day under the data volume, and counts lines that
                  match the failure pattern. The router's own log buffer holds
                  well under an hour when the loop is running, so without this
                  the start of an incident is gone by the time anyone looks.
    AsusRouter  : logs in to the router's web API and reads AiMesh node status
                  and uptime -- the same data the AiMesh page shows.
    MeshMonitor : runs once per watchdog cycle on the main thread, turns both
                  signals into ntfy alerts under their own incident scope, and
                  never reboots anything.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import notifier

LOG = logging.getLogger("wan-watchdog.mesh")

DEFAULT_ALERT_PATTERN = r"Beacon set security failed|ADD/SET beacon failed"
PATTERN_WINDOW = 300  # seconds of syslog considered for the failure pattern
LOGIN_BACKOFF = 900   # ASUS firmware locks the login after repeated failures


# --------------------------------------------------------------------------
# Router API
# --------------------------------------------------------------------------


class RouterError(Exception):
    """The router could not be reached or answered with something unusable."""


class RouterAuthError(RouterError):
    """The router rejected the credentials."""


@dataclass
class Node:
    mac: str
    alias: str
    online: bool
    primary: bool


class AsusRouter:
    """Minimal client for the ASUS (Asuswrt / AiMesh) web API.

    Uses the mobile-app user agent: the firmware gives app sessions their own
    slot, so polling does not sign the owner out of the web UI.
    """

    USER_AGENT = "asusrouter-Android-DUTUtil-1.0.0.245"

    def __init__(self, url: str, user: str, password: str, timeout: int = 10):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self._token = ""

    def _post(self, path: str, form: dict) -> bytes:
        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": f"{self.url}/Main_Login.asp",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self._token:
            headers["Cookie"] = f"asus_token={self._token}"
        request = urllib.request.Request(
            f"{self.url}{path}", data=urllib.parse.urlencode(form).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                raise RouterAuthError(f"HTTP {exc.code} from {path}") from None
            raise RouterError(f"HTTP {exc.code} from {path}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RouterError(f"cannot reach {self.url}: {reason}") from None

    def login(self) -> None:
        self._token = ""
        auth = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        body = self._post("/login.cgi", {"login_authorization": auth})
        try:
            reply = json.loads(body)
        except ValueError:
            raise RouterError("login reply was not JSON") from None
        token = reply.get("asus_token") if isinstance(reply, dict) else None
        if not token:
            status = reply.get("error_status", "?") if isinstance(reply, dict) else "?"
            raise RouterAuthError(f"login rejected (error_status {status})")
        self._token = token

    def hooks(self, *names: str) -> dict:
        """Call appGet.cgi hooks, logging in (again) when the session is gone.

        An expired session does not get a 401: the router answers with an HTML
        page instead of JSON, so a non-JSON reply means "log in and retry once".
        """
        for attempt in (1, 2):
            if not self._token:
                self.login()
            body = self._post("/appGet.cgi", {"hook": ";".join(names)})
            try:
                reply = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                reply = None
            if isinstance(reply, dict):
                return reply
            self._token = ""
            if attempt == 2:
                raise RouterError("appGet.cgi did not return JSON after a fresh login")
        raise AssertionError("unreachable")

    def status(self) -> tuple[list[Node], int | None]:
        """Return (AiMesh nodes, router uptime in seconds or None)."""
        reply = self.hooks("get_cfg_clientlist()", "uptime()")
        raw = reply.get("get_cfg_clientlist")
        if not isinstance(raw, list) or not raw:
            raise RouterError("reply has no AiMesh node list (is AiMesh enabled?)")
        nodes = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            mac = str(item.get("mac", "")).upper()
            nodes.append(Node(
                mac=mac,
                alias=str(item.get("alias") or mac),
                online=str(item.get("online", "")) == "1",
                # The main router is listed first and has no backhaul path.
                primary=index == 0 or str(item.get("re_path", "")) == "0",
            ))
        return nodes, parse_uptime(reply.get("uptime"))


def parse_uptime(value: object) -> int | None:
    """'Mon, 02 Nov 2026 07:51:15 -0500(285 secs since boot)' -> 285."""
    match = re.search(r"\((\d+) secs since boot\)", str(value or ""))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Syslog receiver
# --------------------------------------------------------------------------


_PRI = re.compile(r"^<\d{1,3}>")


class SyslogSink:
    """UDP syslog receiver writing one file per local day.

    Lines are prefixed with the receive time and sender address: the router's
    own timestamp has no year or zone and is wrong for the first minute after
    a boot, before NTP syncs.
    """

    def __init__(self, port: int, log_dir: str | Path, retention_days: int = 30,
                 max_bytes_per_day: int = 100 * 1024 * 1024,
                 alert_pattern: str = DEFAULT_ALERT_PATTERN, bind: str = "0.0.0.0",
                 allowed_sources: frozenset[str] = frozenset()):
        self.port = port
        self.bind = bind
        # Empty allows any sender. Docker's userland proxy can rewrite the
        # source address, so check what arrives before restricting it.
        self.allowed_sources = allowed_sources
        self._refused: set[str] = set()
        self.log_dir = Path(log_dir)
        self.retention_days = retention_days
        self.max_bytes_per_day = max_bytes_per_day
        self.pattern = re.compile(alert_pattern) if alert_pattern else None
        self._lock = threading.Lock()
        self._hits: collections.deque[float] = collections.deque()
        self._day = ""
        self._file = None
        self._bytes_today = 0
        self._dropped_today = 0
        self.received = 0
        self.last_message_ts = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self, stop: threading.Event) -> threading.Thread:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))  # fail at startup, not silently later
        sock.settimeout(1.0)
        LOG.info("syslog: listening on udp/%d, writing to %s", self.port, self.log_dir)
        thread = threading.Thread(target=self._serve, args=(sock, stop),
                                  name="syslog", daemon=True)
        thread.start()
        return thread

    def _serve(self, sock: socket.socket, stop: threading.Event) -> None:
        with sock:
            while not stop.is_set():
                try:
                    data, (source, _port) = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError as exc:
                    LOG.warning("syslog: receive failed: %s", exc)
                    stop.wait(1)
                    continue
                try:
                    self.handle(data, source)
                except Exception:  # one bad datagram must not end logging
                    LOG.exception("syslog: could not record a message")
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

    # -- recording -----------------------------------------------------------

    def handle(self, data: bytes, source: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.allowed_sources and source not in self.allowed_sources:
            if source not in self._refused:
                self._refused.add(source)
                LOG.warning("syslog: ignoring messages from %s (not in MESH_SYSLOG_ALLOWED_SOURCES)",
                            source)
            return
        text = _PRI.sub("", data.decode("utf-8", "replace").strip()).replace("\n", " ")
        if not text:
            return
        stamp = datetime.fromtimestamp(now).astimezone()
        line = f"{stamp.isoformat(timespec='seconds')} {source} {text}\n"
        with self._lock:
            self.received += 1
            self.last_message_ts = now
            if self.pattern and self.pattern.search(text):
                self._hits.append(now)
            self._write(stamp, line)

    def _write(self, stamp: datetime, line: str) -> None:
        day = stamp.strftime("%Y-%m-%d")
        if day != self._day:
            self._rotate(day, stamp)
        encoded = line.encode("utf-8")
        if self._bytes_today + len(encoded) > self.max_bytes_per_day:
            if not self._dropped_today:
                marker = (f"{stamp.isoformat(timespec='seconds')} wan-watchdog daily cap of "
                          f"{self.max_bytes_per_day} bytes reached; dropping until midnight\n")
                self._file.write(marker)
                self._file.flush()
            self._dropped_today += 1
            return
        self._file.write(line)
        self._file.flush()
        self._bytes_today += len(encoded)

    def _rotate(self, day: str, stamp: datetime) -> None:
        if self._file:
            self._file.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"router-{day}.log"
        self._file = path.open("a", encoding="utf-8")
        self._bytes_today = path.stat().st_size
        self._dropped_today = 0
        self._day = day
        self._prune(stamp)

    def _prune(self, stamp: datetime) -> None:
        if self.retention_days <= 0:
            return
        cutoff = (stamp - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for old in self.log_dir.glob("router-*.log"):
            if old.stem.removeprefix("router-") < cutoff:
                try:
                    old.unlink()
                except OSError as exc:
                    LOG.warning("syslog: could not delete %s: %s", old, exc)

    # -- queries -------------------------------------------------------------

    def hits_since(self, seconds: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            while self._hits and self._hits[0] < now - PATTERN_WINDOW:
                self._hits.popleft()
            return sum(1 for ts in self._hits if ts >= now - seconds)

    def snapshot(self) -> dict:
        with self._lock:
            return {"received": self.received, "last_message_ts": self.last_message_ts,
                    "dropped_today": self._dropped_today}


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------


class MeshMonitor:
    """Turns router status and syslog evidence into alerts. Main thread only."""

    def __init__(self, cfg, router: AsusRouter | None, sink: SyslogSink | None):
        self.cfg = cfg
        self.router = router
        self.sink = sink
        self._login_backoff_until = 0.0
        self._logged: set[str] = set()

    def start(self, stop: threading.Event) -> None:
        if self.sink:
            self.sink.start(stop)

    def check(self, state, state_path, act: bool = True) -> list[tuple[str, str]]:
        """One pass. Returns the problems found as (event, message) pairs."""
        cfg = self.cfg
        now = time.time()
        problems: list[tuple[str, str]] = []
        summary: dict = {"ts": now}

        if self.router:
            problems += self._check_router(state, state_path, now, summary, act)

        if self.sink:
            hits = self.sink.hits_since(PATTERN_WINDOW, now)
            summary["syslog"] = self.sink.snapshot() | {"pattern_hits_5min": hits}
            if hits >= cfg.mesh_syslog_threshold:
                problems.append((
                    "syslog_pattern",
                    f"Router needs a reboot: {hits} wireless beacon failures in the last "
                    f"5 min. A radio is stuck restarting, which drops the satellites' backhaul.",
                ))

        summary["problems"] = [event for event, _message in problems]
        state.mesh_last = summary
        # Log transitions, not every minute of an ongoing fault.
        current = {event for event, _message in problems}
        for event, message in problems:
            if event not in self._logged:
                LOG.warning("mesh: %s", message)
        for event in self._logged - current:
            LOG.info("mesh: cleared %s", event)
        self._logged = current

        if act:
            notifier.observe(cfg, state, state_path, not problems, scope="mesh")
            for event, message in problems:
                notifier.queue(cfg, state, state_path, event, message, scope="mesh")
            state.save(state_path)
            notifier.flush(cfg, state, state_path)
        return problems

    def _check_router(self, state, state_path, now, summary, act) -> list[tuple[str, str]]:
        cfg = self.cfg
        if now < self._login_backoff_until:
            summary["router"] = "login backoff"
            return [("login_failed", "Cannot log in to the router; see earlier message.")]
        try:
            nodes, uptime = self.router.status()
        except RouterAuthError as exc:
            # Retrying every minute with a bad password locks the router's
            # login for everyone, including the owner. Back off hard.
            self._login_backoff_until = now + LOGIN_BACKOFF
            summary["router"] = f"login failed: {exc}"
            return [("login_failed",
                     f"Cannot log in to the router ({exc}). Check MESH_ROUTER_USER and "
                     f"MESH_ROUTER_PASSWORD; retrying in {LOGIN_BACKOFF // 60} min.")]
        except RouterError as exc:
            state.mesh_router_failures += 1
            summary["router"] = f"unreachable ({state.mesh_router_failures}): {exc}"
            if state.mesh_router_failures >= cfg.mesh_offline_checks:
                return [("router_unreachable",
                         f"Router API unreachable for {state.mesh_router_failures} checks: {exc}")]
            return []

        state.mesh_router_failures = 0
        summary["router"] = "ok"
        summary["uptime"] = uptime
        summary["nodes"] = [
            {"alias": n.alias, "mac": n.mac, "online": n.online, "primary": n.primary}
            for n in nodes
        ]

        previous_uptime = state.mesh_router_uptime
        state.mesh_router_uptime = uptime or 0
        if uptime is not None and previous_uptime and uptime + 60 < previous_uptime:
            booted = datetime.fromtimestamp(now - uptime).astimezone()
            message = f"Router restarted at {booted:%Y-%m-%d %H:%M} (up {uptime // 60} min)."
            LOG.info("mesh: %s", message)
            if act:
                notifier.notice(cfg, state, state_path, "router_rebooted", message, scope="mesh")

        # Satellites take a few minutes to rejoin after the router boots. Do not
        # count that as a fault, or every manual reboot would page.
        in_boot_grace = uptime is not None and uptime < cfg.mesh_boot_grace
        counts = state.mesh_offline_counts
        problems = []
        for node in nodes:
            if node.primary or node.online:
                counts.pop(node.mac, None)
                continue
            if in_boot_grace:
                continue
            counts[node.mac] = counts.get(node.mac, 0) + 1
            if counts[node.mac] >= cfg.mesh_offline_checks:
                problems.append((
                    f"node_offline:{node.mac}",
                    f"Router needs a reboot: satellite {node.alias} ({node.mac}) has been "
                    f"offline for {counts[node.mac]} checks. Devices on it or wired to it "
                    f"have no internet.",
                ))
        known = {n.mac for n in nodes}
        for mac in list(counts):
            if mac not in known:
                del counts[mac]
        return problems


def build(cfg) -> MeshMonitor | None:
    """Create the monitor from config, or None when every part is disabled."""
    router = None
    if cfg.mesh_router_url:
        router = AsusRouter(cfg.mesh_router_url, cfg.mesh_router_user,
                            cfg.mesh_router_password, cfg.probe_timeout)
    sink = None
    if cfg.mesh_syslog_port:
        sink = SyslogSink(cfg.mesh_syslog_port, cfg.mesh_log_dir, cfg.mesh_log_retention_days,
                          cfg.mesh_log_max_mb_per_day * 1024 * 1024, cfg.mesh_syslog_pattern,
                          allowed_sources=frozenset(
                              s.strip() for s in cfg.mesh_syslog_allowed_sources.split(",")
                              if s.strip()))
    if not router and not sink:
        return None
    return MeshMonitor(cfg, router, sink)
