"""Persistent, incident-scoped diagnostic notifications for the local watchdog."""

import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from health import VERSION

LOG = logging.getLogger("wan-watchdog.notify")


def queue(cfg, state, path, event: str, message: str) -> None:
    if not cfg.notify_url:
        return
    now = time.time()
    if not state.notification_incident:
        state.notification_healthy_checks = 0
        state.notification_incident = {
            "id": uuid.uuid4().hex[:12], "started_at": now, "events": [],
        }
    incident = state.notification_incident
    if event in incident["events"]:
        return
    incident["events"].append(event)
    observed = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    state.notification_outbox.append({
        "id": f"{incident['id']}:{event}",
        "event": event,
        "message": f"[wan-watchdog local] {message}\nIncident {incident['id']}; observed {observed}",
        "attempts": 0,
        "next_attempt_at": now,
    })
    state.save(path)


def observe(cfg, state, path, healthy: bool) -> None:
    if not healthy:
        state.notification_healthy_checks = 0
        return
    state.notification_healthy_checks = min(state.notification_healthy_checks + 1, 3)
    if state.notification_incident and state.notification_healthy_checks >= 3:
        duration = max(0, round((time.time() - state.notification_incident["started_at"]) / 60))
        queue(cfg, state, path, "recovered",
              f"Recovery confirmed by 3 healthy checks, {duration} min after the first local event.")
        state.notification_incident = {}
        state.save(path)


def _retry_after(value: str | None, now: float) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


def flush(cfg, state, path) -> None:
    if not cfg.notify_url or not state.notification_outbox:
        return
    if not state.save(path):
        return
    for _attempt in range(4):
        if not state.notification_outbox:
            break
        entry = state.notification_outbox[0]
        now = time.time()
        if entry["next_attempt_at"] > now:
            break
        headers = {
            "User-Agent": f"wan-watchdog/{VERSION}",
            "Content-Type": "text/plain; charset=utf-8",
            "Title": "wan-watchdog local diagnostics",
            "Priority": "default" if entry["event"] == "recovered" else "high",
        }
        if cfg.notify_token:
            headers["Authorization"] = f"Bearer {cfg.notify_token}"
        retry_after = 0
        try:
            request = urllib.request.Request(cfg.notify_url, data=entry["message"].encode(), headers=headers)
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise OSError("Notification endpoint did not accept the message")
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                retry_after = _retry_after(exc.headers.get("Retry-After"), now)
                exc.close()
            entry["attempts"] += 1
            delay = min(3600, 60 * 2 ** min(entry["attempts"] - 1, 6))
            entry["next_attempt_at"] = now + max(delay, retry_after)
            state.save(path)
            LOG.warning("notification %s failed (%s); queued for retry", entry["id"], type(exc).__name__)
            break
        state.notification_outbox.pop(0)
        LOG.info("notification %s accepted", entry["id"])
        if not state.save(path):
            break


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
