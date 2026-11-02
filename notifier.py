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


# Each scope keeps its own incident and recovery streak, so a healthy WAN check
# cannot close a router-mesh incident (or the reverse). All scopes share one
# outbox, so delivery order and retry policy are the same for every message.
#   scope: (incident attribute, healthy-streak attribute, message prefix, ntfy title)
SCOPES = {
    "local": ("notification_incident", "notification_healthy_checks",
              "[wan-watchdog local]", "wan-watchdog local diagnostics"),
    "mesh": ("mesh_incident", "mesh_healthy_checks",
             "[router mesh]", "Router mesh"),
}
DEFAULT_TITLE = SCOPES["local"][3]


def _append(state, entry_id: str, event: str, text: str, title: str, now: float) -> None:
    state.notification_outbox.append({
        "id": entry_id,
        "event": event,
        "message": text,
        "title": title,
        "attempts": 0,
        "next_attempt_at": now,
    })


def queue(cfg, state, path, event: str, message: str, scope: str = "local") -> None:
    if not cfg.notify_url:
        return
    incident_attr, healthy_attr, prefix, title = SCOPES[scope]
    now = time.time()
    incident = getattr(state, incident_attr)
    if not incident:
        setattr(state, healthy_attr, 0)
        incident = {"id": uuid.uuid4().hex[:12], "started_at": now, "events": []}
        setattr(state, incident_attr, incident)
    if event in incident["events"]:
        return
    incident["events"].append(event)
    observed = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    _append(state, f"{incident['id']}:{event}", event,
            f"{prefix} {message}\nIncident {incident['id']}; observed {observed}", title, now)
    state.save(path)


def notice(cfg, state, path, event: str, message: str, scope: str) -> None:
    """Queue a one-off message outside any incident: no dedup, no recovery.

    For facts worth knowing that are not faults in themselves, such as the
    router having restarted. The caller is responsible for sending it once.
    """
    if not cfg.notify_url:
        return
    _incident_attr, _healthy_attr, prefix, title = SCOPES[scope]
    now = time.time()
    observed = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    _append(state, f"notice-{uuid.uuid4().hex[:12]}:{event}", event,
            f"{prefix} {message}\nObserved {observed}", title, now)
    state.save(path)


def observe(cfg, state, path, healthy: bool, scope: str = "local") -> None:
    incident_attr, healthy_attr, _prefix, _title = SCOPES[scope]
    if not healthy:
        setattr(state, healthy_attr, 0)
        return
    streak = min(getattr(state, healthy_attr) + 1, 3)
    setattr(state, healthy_attr, streak)
    incident = getattr(state, incident_attr)
    if incident and streak >= 3:
        duration = max(0, round((time.time() - incident["started_at"]) / 60))
        what = "local" if scope == "local" else scope
        queue(cfg, state, path, "recovered",
              f"Recovery confirmed by 3 healthy checks, {duration} min after the first {what} event.",
              scope=scope)
        setattr(state, incident_attr, {})
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
            "Title": entry.get("title", DEFAULT_TITLE),
            "Priority": "default" if entry["event"] in ("recovered", "router_rebooted") else "high",
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
