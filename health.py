#!/usr/bin/env python3
"""The health endpoint the watchdog serves, and then calls from the outside.

This is the target of the round trip that gives the watchdog its signal. The
container serves `/healthz`; nginx publishes it at https://health.example.com/;
the watchdog then fetches that public URL. The request leaves the house, hits
Cloudflare's edge, and comes back in through the gateway and the reverse proxy,
so a success proves the entire inbound path is intact.

Every response carries an instance id generated once at start-up. The watchdog
checks it on the way back in, which is what separates "my service answered"
from "something answered" — a cached Cloudflare response or a different backend
would return 200 with the wrong id, and that is a routing fault, not a gateway
fault.

The public endpoint deliberately exposes nothing about the network it protects.
Richer detail lives on `/status`, which stays off unless a token is configured.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

LOG = logging.getLogger("wan-watchdog.health")

VERSION = "1.0"


def new_instance_id() -> str:
    return uuid.uuid4().hex[:16]


class _Handler(BaseHTTPRequestHandler):
    server_version = "wan-watchdog"
    sys_version = ""  # do not advertise the Python version publicly

    # Injected by make_server().
    instance_id: str = ""
    status_provider: Callable[[], dict] = staticmethod(dict)
    status_token: str = ""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Cloudflare must never serve this from cache: a cached 200 would make
        # the watchdog blind to a real outage.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (http.server's required spelling)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/healthz", "/health"):
            self._send(200, {
                "status": "ok",
                "instance": self.instance_id,
                "ts": int(time.time()),
                "version": VERSION,
            })
            return

        if path == "/status":
            # Off by default. This endpoint is reachable from the public
            # internet through the same proxy, so it stays shut unless a token
            # is set, and it never returns the gateway access code.
            if not self.status_token:
                self._send(404, {"error": "not found"})
                return
            supplied = ""
            if "?" in self.path:
                from urllib.parse import parse_qs
                supplied = parse_qs(self.path.split("?", 1)[1]).get("token", [""])[0]
            header_token = self.headers.get("X-Status-Token", "")
            if supplied != self.status_token and header_token != self.status_token:
                self._send(403, {"error": "forbidden"})
                return
            self._send(200, self.status_provider())
            return

        self._send(404, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args) -> None:
        # Keep access logging out of the service log at normal verbosity; the
        # watchdog polls this endpoint constantly and would drown everything.
        LOG.debug("health: " + fmt, *args)


def make_server(
    port: int,
    instance_id: str,
    status_provider: Callable[[], dict],
    status_token: str = "",
    bind: str = "0.0.0.0",
) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "instance_id": instance_id,
            "status_provider": staticmethod(status_provider),
            "status_token": status_token,
        },
    )
    return ThreadingHTTPServer((bind, port), handler)


def serve_in_background(
    port: int,
    instance_id: str,
    status_provider: Callable[[], dict],
    status_token: str = "",
    bind: str = "0.0.0.0",
) -> ThreadingHTTPServer:
    """Start the health server on its own thread and return it.

    It runs independently of the probe loop so the endpoint keeps answering
    even if a probe is blocked on a slow timeout.
    """
    server = make_server(port, instance_id, status_provider, status_token, bind)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    LOG.info("health endpoint listening on %s:%d (instance %s)", bind, port, instance_id)
    return server
