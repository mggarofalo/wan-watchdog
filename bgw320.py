#!/usr/bin/env python3
"""Client for the AT&T / HUMAX BGW320-500 residential gateway.

Speaks the gateway's session+nonce login scheme so its authenticated pages can
be driven from a script:

    1. GET a protected page         -> gateway sets a SessionID cookie
    2. GET it again with the cookie -> gateway renders the login form + nonce
    3. POST md5(accesscode + nonce) -> /cgi-bin/login.ha

The gateway rejects requests that arrive without a session or a plausible
Referer with a bare "400 Bad Request", so both are always supplied.

Standard library only: this has to run on a stock Raspberry Pi OS image with no
pip packages installed.

CLI:
    python3 bgw320.py status                  # uptime + WAN state (no auth needed)
    python3 bgw320.py login  --code 1234...   # verify the access code works
    python3 bgw320.py reboot --code 1234...   # restart the gateway
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The factory default. If the gateway's LAN subnet was changed to keep it out
# of the way of a downstream router, this will differ -- check the address of
# the second hop in a traceroute.
DEFAULT_HOST = "192.168.1.254"
DEFAULT_TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Pages that render without authentication.
PAGE_SYSINFO = "/cgi-bin/sysinfo.ha"
PAGE_BROADBAND = "/cgi-bin/broadbandstatistics.ha"
PAGE_FIREWALL = "/cgi-bin/firewall.ha"
# Pages that require the device access code.
PAGE_RESTART = "/cgi-bin/restart.ha"
PAGE_IPPASS = "/cgi-bin/ippass.ha"
PAGE_DOSPROTECT = "/cgi-bin/dosprotect.ha"
PAGE_LOGIN = "/cgi-bin/login.ha"

LOGIN_MARKER = "Access Code Required"


class GatewayError(RuntimeError):
    """Raised when the gateway refuses a request or answers unexpectedly."""


# --------------------------------------------------------------------------
# HTML helpers. The gateway serves simple server-rendered pages, so targeted
# regexes are sufficient and avoid pulling in a parser dependency.
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style.*?</style>", re.IGNORECASE | re.DOTALL)
_FORM_RE = re.compile(r"<form[^>]*>.*?</form>", re.IGNORECASE | re.DOTALL)
_ACTION_RE = re.compile(r"""action\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""")


def visible_lines(html: str) -> list[str]:
    """Strip markup and return the page's non-empty text lines in order.

    The gateway renders each field as a label line followed immediately by its
    value line, which `field()` relies on.
    """
    text = _SCRIPT_RE.sub("", html)
    text = _STYLE_RE.sub("", text)
    text = _TAG_RE.sub("\n", text)
    return [line.strip() for line in text.split("\n") if line.strip()]


def field(lines: list[str], label: str, last: bool = False) -> str | None:
    """Return the value rendered directly beneath `label`, if present.

    The first occurrence is used by default. Every page repeats its labels in a
    trailing Help section, but those copies carry a trailing colon
    ("Manufacturer:") so exact matching already skips them.

    `last=True` is needed for pages whose left-hand nav menu uses the very same
    wording as their field labels — firewall.ha lists "Packet Filter" and
    "IP Passthrough" as nav links before rendering them as fields, so the first
    match there returns the adjacent nav link instead of the value.
    """
    found = None
    for i, line in enumerate(lines):
        if line == label and i + 1 < len(lines):
            found = lines[i + 1]
            if not last:
                return found
    return found


def parse_form(html: str) -> tuple[str, dict[str, str]]:
    """Extract the first form's action and its pre-filled fields.

    Hidden and text inputs are collected along with the first submit button,
    which lets a form be replayed without hardcoding the gateway's field names.
    """
    match = _FORM_RE.search(html)
    if not match:
        raise GatewayError("no <form> found on page")
    form = match.group(0)

    action_match = _ACTION_RE.search(form)
    action = action_match.group(1) if action_match else ""

    fields: dict[str, str] = {}
    submit_added = False
    for tag in _INPUT_RE.findall(form):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        name = attrs.get("name")
        if not name:
            continue
        input_type = attrs.get("type", "text").lower()
        if input_type == "submit":
            # Replay only the first submit button; a form may carry several
            # (e.g. Save / Cancel) and sending all of them is ambiguous.
            if not submit_added:
                fields[name] = attrs.get("value", "")
                submit_added = True
            continue
        fields[name] = attrs.get("value", "")
    return action, fields


class Gateway:
    """An authenticated session against the residential gateway."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        access_code: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.access_code = access_code
        self.timeout = timeout
        self.base = f"http://{host}"
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._logged_in = False

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        path: str,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> str:
        url = urllib.parse.urljoin(self.base, path)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": referer or urllib.parse.urljoin(self.base, PAGE_SYSINFO),
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"{path} -> HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"{path} -> unreachable: {exc.reason}") from exc

    # -- unauthenticated reads --------------------------------------------

    def system_info(self) -> dict[str, str]:
        """Model, firmware and uptime. Readable without the access code."""
        lines = visible_lines(self._request(PAGE_SYSINFO))
        keys = [
            "Manufacturer",
            "Model Number",
            "Serial Number",
            "Software Version",
            "MAC Address",
            "First Use Date",
            "Time Since Last Reboot",
            "Current Date/Time",
        ]
        return {k: field(lines, k) or "" for k in keys}

    def uptime_seconds(self) -> int | None:
        """Seconds since the gateway last restarted, or None if unreadable."""
        raw = self.system_info().get("Time Since Last Reboot", "")
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        # Some firmware renders this as D:HH:MM:SS instead of raw seconds.
        if ":" in raw:
            try:
                parts = [int(p) for p in raw.split(":")]
            except ValueError:
                return None
            seconds = 0
            for part in parts:
                seconds = seconds * 60 + part
            if len(parts) == 4:  # days:hours:minutes:seconds
                days, hours, minutes, secs = parts
                seconds = ((days * 24 + hours) * 60 + minutes) * 60 + secs
            return seconds
        return None

    def broadband_status(self) -> dict[str, str]:
        """WAN-side state: link, addressing and error counters."""
        lines = visible_lines(self._request(PAGE_BROADBAND))
        keys = [
            "Broadband Connection",
            "Broadband IPv4 Address",
            "Gateway IPv4 Address",
            "Line State",
            "PON Link Status",
            "Receive Drops",
            "Receive Errors",
            "Transmit Errors",
        ]
        return {k: field(lines, k) or "" for k in keys}

    def firewall_status(self) -> dict[str, str]:
        lines = visible_lines(self._request(PAGE_FIREWALL))
        keys = [
            "Packet Filter",
            "IP Passthrough",
            "NAT Default Server",
            "Firewall Advanced",
        ]
        # This page's nav menu reuses the field labels verbatim, so match the
        # last occurrence rather than the first. See field().
        return {k: field(lines, k, last=True) or "" for k in keys}

    # -- authentication ----------------------------------------------------

    def login(self) -> None:
        """Authenticate with the device access code.

        The gateway will not render the login form until it has issued a
        SessionID cookie, so the protected page is fetched twice: the first
        request exists purely to pick up the cookie.
        """
        if not self.access_code:
            raise GatewayError("no access code provided")
        if self._logged_in:
            return

        self._request(PAGE_IPPASS)  # first GET: obtain SessionID
        html = self._request(PAGE_IPPASS)  # second GET: form + nonce

        if LOGIN_MARKER not in html:
            self._logged_in = True  # already authenticated
            return

        action, fields = parse_form(html)
        nonce = fields.get("nonce")
        if not nonce:
            raise GatewayError("login form carried no nonce")

        digest = hashlib.md5(
            (self.access_code + nonce).encode("utf-8")
        ).hexdigest()
        # The page's own JS blanks the plaintext field to asterisks before
        # submitting and sends the digest alongside; mirror that exactly.
        fields["password"] = "*" * len(self.access_code)
        fields["hashpassword"] = digest

        result = self._request(
            action or PAGE_LOGIN,
            data=fields,
            referer=urllib.parse.urljoin(self.base, PAGE_IPPASS),
        )
        if LOGIN_MARKER in result:
            raise GatewayError("login rejected — check the device access code")
        self._logged_in = True

    def authenticated_page(self, path: str) -> str:
        self.login()
        return self._request(path)

    # -- actions -----------------------------------------------------------

    def reboot(self) -> None:
        """Trigger a gateway restart via the Restart Device page.

        The restart form is replayed rather than reconstructed, so a firmware
        update that renames its fields will not silently break this.
        """
        self.login()
        html = self._request(PAGE_RESTART)
        if LOGIN_MARKER in html:
            raise GatewayError("session expired before restart could be sent")
        action, fields = parse_form(html)
        self._request(
            action or PAGE_RESTART,
            data=fields,
            referer=urllib.parse.urljoin(self.base, PAGE_RESTART),
        )
        # The gateway drops the connection as it goes down; reaching this point
        # without an exception means the request was accepted.


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h {minutes}m ({seconds}s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["status", "login", "reboot"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--code", help="device access code (on the gateway label)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt on reboot",
    )
    args = parser.parse_args(argv)

    gw = Gateway(host=args.host, access_code=args.code, timeout=args.timeout)

    try:
        if args.action == "status":
            info = gw.system_info()
            print(f"{info['Manufacturer']} {info['Model Number']}  fw {info['Software Version']}")
            print(f"  uptime        : {_format_uptime(gw.uptime_seconds())}")
            print(f"  gateway time  : {info['Current Date/Time']}")
            for key, value in gw.broadband_status().items():
                print(f"  {key:<24}: {value}")
            for key, value in gw.firewall_status().items():
                print(f"  {key:<24}: {value}")
            return 0

        if args.action == "login":
            gw.login()
            print("login OK — access code accepted")
            return 0

        if args.action == "reboot":
            before = gw.uptime_seconds()
            print(f"gateway uptime before reboot: {_format_uptime(before)}")
            if not args.yes:
                confirm = input("Restart the gateway? Internet drops ~3 min [y/N] ")
                if confirm.strip().lower() not in ("y", "yes"):
                    print("aborted")
                    return 1
            gw.reboot()
            print("restart command accepted — gateway is going down")
            return 0
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
