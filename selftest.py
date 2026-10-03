#!/usr/bin/env python3
"""Self-test for the watchdog's decision logic, health endpoint and HTML parser.

    python3 selftest.py            # everything, including probes that need internet
    python3 selftest.py --offline  # deterministic subset, for CI

The offline subset covers the parts worth gating a release on: the decision
table that determines whether the gateway gets rebooted, the instance
verification that makes the round trip trustworthy, and the parsing of the
gateway's HTML pages.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import urllib.request
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bgw320  # noqa: E402
import health  # noqa: E402
import watchdog as w  # noqa: E402
from watchdog import (  # noqa: E402
    APP_DOWN, HEALTHY, INBOUND_BROKEN, KIND_DNS, KIND_MISMATCH, KIND_OK,
    KIND_ORIGIN, KIND_ORIGIN_TLS, KIND_TLS, KIND_TRANSPORT, LOCAL_FAULT,
    PROXY_DOWN, REBOOT_WORTHY, WAN_DOWN, Probe, State, evaluate, http_probe,
    outbound_probe, tcp_probe, _verify_instance,
)

TESTDATA = HERE / "testdata"
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r} want {want!r}")


UP = Probe("x", True, "HTTP 200", 200, KIND_OK)
CF5XX = Probe("x", False, "HTTP 522", 522, KIND_ORIGIN)
REFUSED = Probe("x", False, "refused", None, KIND_TRANSPORT)
TLSBAD = Probe("x", False, "cert bad", None, KIND_TLS)
DNSBAD = Probe("x", False, "no such host", None, KIND_DNS)
WRONGBOX = Probe("x", False, "wrong instance", 200, KIND_MISMATCH)
CFTLS = Probe("x", False, "HTTP 526", 526, KIND_ORIGIN_TLS)


def test_decisions() -> None:
    print("== evaluate(): reboot-worthy ==")
    check("external ok -> healthy", evaluate(UP, UP, UP, UP)[0], HEALTHY)
    check("ext down, everything else fine -> inbound_broken",
          evaluate(CF5XX, UP, UP, UP)[0], INBOUND_BROKEN)
    check("ext down + outbound down -> wan_down",
          evaluate(CF5XX, UP, UP, REFUSED)[0], WAN_DOWN)
    check("no local/proxy configured -> inbound_broken",
          evaluate(CF5XX, None, None, UP)[0], INBOUND_BROKEN)

    print("\n== evaluate(): must NOT reboot the gateway ==")
    check("container not serving -> app_down",
          evaluate(CF5XX, REFUSED, UP, UP)[0], APP_DOWN)
    check("nginx down -> proxy_down",
          evaluate(CF5XX, UP, REFUSED, UP)[0], PROXY_DOWN)
    check("wrong instance answered -> local_fault",
          evaluate(WRONGBOX, UP, UP, UP)[0], LOCAL_FAULT)
    # 525/526 mean Cloudflare completed TCP to the origin and only then failed
    # on TLS, which proves inbound delivery works. The plain-HTTP proxy probe
    # cannot see an expired certificate, so without this the verdict would be
    # inbound_broken and the gateway would be rebooted to fix a cert.
    check("expired origin cert (526) -> proxy_down, NOT inbound_broken",
          evaluate(CFTLS, UP, UP, UP)[0], PROXY_DOWN)
    check("  and still proxy_down when only the proxy probe is absent",
          evaluate(CFTLS, UP, None, UP)[0], PROXY_DOWN)
    check("external TLS failure -> local_fault",
          evaluate(TLSBAD, UP, UP, UP)[0], LOCAL_FAULT)
    check("external DNS failure, outbound up -> local_fault",
          evaluate(DNSBAD, UP, UP, UP)[0], LOCAL_FAULT)
    check("external DNS failure, outbound down -> wan_down (real outage)",
          evaluate(DNSBAD, UP, UP, REFUSED)[0], WAN_DOWN)

    print("\n== only two verdicts may reboot ==")
    check("reboot-worthy set", REBOOT_WORTHY, {WAN_DOWN, INBOUND_BROKEN})
    for verdict in (APP_DOWN, PROXY_DOWN, LOCAL_FAULT, HEALTHY):
        check(f"  {verdict} never reboots", verdict in REBOOT_WORTHY, False)


def test_instance_verification() -> None:
    print("\n== instance verification ==")
    good = json.dumps({"status": "ok", "instance": "abc123"}).encode()
    check("matching instance passes", _verify_instance("e", 200, good, "abc123").ok, True)

    other = json.dumps({"status": "ok", "instance": "deadbeef"}).encode()
    probe = _verify_instance("e", 200, other, "abc123")
    check("different instance fails", probe.ok, False)
    check("  classified as mismatch", probe.kind, KIND_MISMATCH)

    probe = _verify_instance("e", 200, b"<html>hello</html>", "abc123")
    check("non-JSON body fails", probe.ok, False)
    check("  classified as mismatch", probe.kind, KIND_MISMATCH)


def test_health_server() -> None:
    print("\n== health endpoint ==")
    instance = health.new_instance_id()
    state = {"served": True}
    server = health.serve_in_background(
        0, instance, lambda: dict(state, instance=instance),
        status_token="", bind="127.0.0.1")
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    body = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=5).read())
    check("/healthz returns ok", body.get("status"), "ok")
    check("/healthz carries the instance id", body.get("instance"), instance)

    resp = urllib.request.urlopen(f"{base}/healthz", timeout=5)
    check("/healthz forbids caching",
          "no-store" in resp.headers.get("Cache-Control", ""), True)

    # The probe path used against the public URL must accept this reply.
    probe = http_probe("external", f"{base}/healthz", 5, expect_instance=instance)
    check("probe accepts a matching instance", probe.ok, True)
    probe = http_probe("external", f"{base}/healthz", 5, expect_instance="not-this-one")
    check("probe rejects a mismatched instance", (probe.ok, probe.kind),
          (False, KIND_MISMATCH))

    try:
        urllib.request.urlopen(f"{base}/status", timeout=5)
        check("/status is closed without a token", "opened", "404")
    except urllib.error.HTTPError as exc:
        check("/status is closed without a token", exc.code, 404)
    server.shutdown()

    # With a token configured it opens, but only to the right token.
    server = health.serve_in_background(
        0, instance, lambda: {"secret": "detail"}, status_token="s3cret",
        bind="127.0.0.1")
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(f"{base}/status?token=wrong", timeout=5)
        check("/status rejects a wrong token", "opened", "403")
    except urllib.error.HTTPError as exc:
        check("/status rejects a wrong token", exc.code, 403)
    body = json.loads(urllib.request.urlopen(f"{base}/status?token=s3cret", timeout=5).read())
    check("/status opens with the right token", body.get("secret"), "detail")
    server.shutdown()


def test_state() -> None:
    print("\n== State round-trip ==")
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    State(consecutive_failures=3, reboot_count=2, last_verdict=INBOUND_BROKEN).save(tmp)
    loaded = State.load(tmp)
    check("survives save/load",
          (loaded.consecutive_failures, loaded.reboot_count, loaded.last_verdict),
          (3, 2, INBOUND_BROKEN))
    check("missing file -> defaults",
          State.load(pathlib.Path("does-not-exist.json")).consecutive_failures, 0)


def test_html_parsing() -> None:
    print("\n== bgw320 HTML parsing (captured pages) ==")
    if not TESTDATA.is_dir():
        print("  [SKIP] testdata/ not present")
        return
    lines = bgw320.visible_lines((TESTDATA / "sysinfo.ha.html").read_text(encoding="utf-8"))
    check("model parsed", bgw320.field(lines, "Model Number"), "BGW320-500")
    check("help-section duplicate ignored", bgw320.field(lines, "Manufacturer"), "HUMAX")

    band = bgw320.visible_lines(
        (TESTDATA / "broadbandstatistics.ha.html").read_text(encoding="utf-8"))
    check("WAN state parsed", bgw320.field(band, "Broadband Connection"), "Up")

    fw = bgw320.visible_lines((TESTDATA / "firewall.ha.html").read_text(encoding="utf-8"))
    # This page's nav menu reuses the field labels, so the first match is a nav
    # link and only last=True returns the real value.
    check("nav collision needs last=True",
          bgw320.field(fw, "IP Passthrough", last=True), "On")
    check("first match is the nav link",
          bgw320.field(fw, "IP Passthrough"), "Firewall Advanced")

    try:
        bgw320.parse_form((TESTDATA / "ippass_login.html").read_text(encoding="utf-8"))
        check("pre-cookie page should have no form", True, False)
    except bgw320.GatewayError:
        check("pre-cookie page raises GatewayError", True, True)


def test_network() -> None:
    print("\n== live network probes ==")
    check("bad hostname -> dns",
          http_probe("t", "http://no-such-host-xyzzy.invalid/", 8).kind, KIND_DNS)
    # A certificate this host cannot validate still proves outbound works. If
    # this regressed, a CA problem would trigger spurious gateway reboots.
    probe = outbound_probe("https://1.1.1.1/cdn-cgi/trace", 15)
    check("outbound survives a bad CA bundle", probe.ok, True)
    print(f"       detail: {probe.detail}")
    check("tcp probe reaches 1.1.1.1", tcp_probe("t", "1.1.1.1", 443, 8).ok, True)


def main() -> int:
    offline = "--offline" in sys.argv
    test_decisions()
    test_instance_verification()
    test_health_server()
    test_state()
    test_html_parsing()
    suite = unittest.defaultTestLoader.loadTestsFromName("test_notifier")
    result = unittest.TextTestRunner().run(suite)
    if not result.wasSuccessful():
        failures.append("local notifier/release policy tests failed")
    print("\n== local-only probes ==")
    check("refused connection -> transport",
          http_probe("t", "http://127.0.0.1:9/", 4).kind, KIND_TRANSPORT)
    check("502 is a fault", 502 in w.ORIGIN_UNREACHABLE, True)
    check("404 is not a fault", 404 in w.ORIGIN_UNREACHABLE, False)
    check("525 is NOT origin-unreachable", 525 in w.ORIGIN_UNREACHABLE, False)
    check("526 is NOT origin-unreachable", 526 in w.ORIGIN_UNREACHABLE, False)
    check("525/526 are origin-TLS", w.ORIGIN_TLS_ERROR, {525, 526})
    check("the two sets never overlap",
          w.ORIGIN_UNREACHABLE & w.ORIGIN_TLS_ERROR, set())
    if offline:
        print("\n== live network probes ==\n  [SKIP] --offline")
    else:
        test_network()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILURE(S):"))
    for line in failures:
        print("  - " + line)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
