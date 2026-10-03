"""Offline tests for persistent local diagnostics and release promotion policy."""

import importlib.util
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import notifier
import watchdog as watchdog


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "state.json"
        with patch.dict("os.environ", {
            "WATCHDOG_EXTERNAL_URL": "https://health.example.test/healthz",
            "BGW_ACCESS_CODE": "test-code", "WATCHDOG_NOTIFY_URL": "https://ntfy.example.test/topic",
            "WATCHDOG_DRY_RUN": "false", "WATCHDOG_POST_REBOOT_GRACE": "0",
        }, clear=True):
            self.cfg = watchdog.load_config()
        self.state = watchdog.State()
        self.now = 1000000.0
        self.clock = patch("time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.opener = Mock()
        self.opener.open.return_value.__enter__ = Mock(return_value=Mock(status=200))
        self.opener.open.return_value.__exit__ = Mock(return_value=False)
        self.http = patch("notifier.urllib.request.build_opener", return_value=self.opener)
        self.http.start()
        self.addCleanup(self.http.stop)

    def queue(self, event="cooldown"):
        notifier.queue(self.cfg, self.state, self.path, event, "diagnostic detail")
        notifier.flush(self.cfg, self.state, self.path)

    def test_one_event_across_4_5_hours_and_restart(self):
        self.queue()
        incident = self.state.notification_incident["id"]
        self.state = watchdog.State.load(self.path)
        for _cycle in range(270):
            self.now += 60
            self.queue()
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.state.notification_incident["id"], incident)

    def test_recovery_requires_three_successes_and_rearms(self):
        self.queue()
        for _cycle in range(2):
            notifier.observe(self.cfg, self.state, self.path, True)
        notifier.observe(self.cfg, self.state, self.path, False)
        self.assertTrue(self.state.notification_incident)
        for _cycle in range(3):
            notifier.observe(self.cfg, self.state, self.path, True)
        notifier.flush(self.cfg, self.state, self.path)
        self.assertFalse(self.state.notification_incident)
        self.assertEqual(self.opener.open.call_count, 2)
        self.queue()
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.state.notification_healthy_checks, 0)

    def test_backoff_survives_restart_and_delivers_in_order(self):
        self.opener.open.side_effect = OSError("offline")
        self.queue("diagnosis:proxy_down")
        self.state = watchdog.State.load(self.path)
        self.queue("cooldown")
        self.assertEqual(self.opener.open.call_count, 1)
        self.now += 60
        notifier.flush(self.cfg, self.state, self.path)
        self.assertEqual(self.state.notification_outbox[0]["next_attempt_at"], self.now + 120)
        self.now += 120
        self.opener.open.side_effect = None
        notifier.flush(self.cfg, self.state, self.path)
        self.assertEqual(len(self.state.notification_outbox), 0)
        messages = [call.args[0].data.decode() for call in self.opener.open.call_args_list[-2:]]
        self.assertTrue(all("observed" in message for message in messages))

    def test_retry_after_and_bearer_token(self):
        self.cfg.notify_token = "private-token"
        self.opener.open.side_effect = urllib.error.HTTPError(
            self.cfg.notify_url, 429, "rate limited", {"Retry-After": "600"}, None)
        self.queue()
        self.assertEqual(self.state.notification_outbox[0]["next_attempt_at"], self.now + 600)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertNotIn("private-token", self.path.read_text())

    def test_persistence_failure_does_not_send(self):
        with patch.object(self.state, "save", return_value=False):
            self.queue()
        self.opener.open.assert_not_called()

    def test_disabled_notifier_does_not_queue(self):
        self.cfg.notify_url = ""
        self.queue()
        self.assertFalse(self.state.notification_incident)
        self.opener.open.assert_not_called()

    def test_old_state_upgrades(self):
        self.path.write_text(json.dumps({"last_reboot_ts": 123, "reboot_count": 4}))
        loaded = watchdog.State.load(self.path)
        self.assertEqual(loaded.notification_outbox, [])
        self.assertEqual(loaded.last_reboot_ts, 123)

    def test_reboot_notice_precedes_gateway_request(self):
        events = []
        self.opener.open.side_effect = lambda *args, **kwargs: events.append("notify") or self.opener.open.return_value
        with patch.object(watchdog, "Gateway") as gateway:
            gateway.return_value.reboot.side_effect = lambda: events.append("reboot")
            self.assertTrue(watchdog.reboot_gateway(self.cfg, self.state, "test", self.path))
        self.assertEqual(events, ["notify", "reboot"])
        self.assertIn("Recovery is not yet verified", self.opener.open.call_args.args[0].data.decode())

    def test_repeated_restart_failures_and_cooldown_do_not_spam(self):
        with patch.object(watchdog, "Gateway") as gateway:
            gateway.return_value.reboot.side_effect = watchdog.GatewayError("failed")
            for _attempt in range(3):
                watchdog.reboot_gateway(self.cfg, self.state, "test", self.path)
        self.assertEqual(self.opener.open.call_count, 2)
        self.state.last_reboot_ts = self.now - 60
        for _attempt in range(3):
            watchdog.reboot_gateway(self.cfg, self.state, "test", self.path)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_probe_only_mode_does_not_notify_or_reboot(self):
        self.state.consecutive_failures = 10
        self.state.last_verdict = watchdog.APP_DOWN
        down = watchdog.Probe("test", False, "down", kind=watchdog.KIND_TRANSPORT)
        with patch.object(watchdog, "http_probe", return_value=down), \
                patch.object(watchdog, "outbound_probe", return_value=down), \
                patch.object(watchdog, "Gateway") as gateway:
            watchdog.run_cycle(self.cfg, self.state, self.path, "instance", act=False)
        self.opener.open.assert_not_called()
        gateway.assert_not_called()

    def test_cycle_debounces_diagnosis_and_announces_recovery_once(self):
        healthy = watchdog.Probe("test", True, "HTTP 200")
        with patch.object(watchdog, "http_probe", return_value=healthy), \
                patch.object(watchdog, "outbound_probe", return_value=healthy), \
                patch.object(watchdog, "evaluate", return_value=(watchdog.PROXY_DOWN, "proxy unavailable")):
            for _cycle in range(4):
                watchdog.run_cycle(self.cfg, self.state, self.path, "instance")
            self.opener.open.assert_not_called()
            for _cycle in range(10):
                watchdog.run_cycle(self.cfg, self.state, self.path, "instance")
            self.assertEqual(self.opener.open.call_count, 1)
        self.state = watchdog.State.load(self.path)
        with patch.object(watchdog, "http_probe", return_value=healthy), \
                patch.object(watchdog, "outbound_probe", return_value=healthy):
            for _cycle in range(4):
                watchdog.run_cycle(self.cfg, self.state, self.path, "instance")
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertFalse(self.state.notification_incident)

    def test_dry_run_does_not_announce_or_request_reboot(self):
        self.cfg.dry_run = True
        with patch.object(watchdog, "Gateway") as gateway:
            self.assertFalse(watchdog.reboot_gateway(self.cfg, self.state, "test", self.path))
        gateway.assert_not_called()
        self.opener.open.assert_not_called()

    def test_redirects_are_not_followed(self):
        handler = notifier._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://other.test"))


class ReleasePolicyTests(unittest.TestCase):
    def test_release_channels(self):
        path = pathlib.Path(__file__).parent / ".github" / "image_tags.py"
        if not path.exists():
            self.skipTest("release tooling is not shipped in the image")
        spec = importlib.util.spec_from_file_location("image_tags", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for version, stable in [("1.1.0", True), ("2.0.0", True), ("1.1.1", False), ("1.2.0-rc.0", False)]:
            self.assertEqual(module.promotes_stable(f"refs/tags/v{version}", version), stable)
        self.assertFalse(module.promotes_stable("refs/heads/main", "1.1.0"))
        for tag in ["v1.1", "v01.1.0", "v1.2.0", "v1.1.0-01"]:
            with self.assertRaises(ValueError):
                module.promotes_stable(f"refs/tags/{tag}", "1.1.0")


if __name__ == "__main__":
    unittest.main()
