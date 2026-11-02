"""Offline tests for the router mesh monitor: syslog sink, ASUS client, alert policy."""

import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import Mock, patch

import mesh
import notifier
import watchdog

T0 = datetime(2026, 11, 2, 7, 0, 0).timestamp()


def _reply(payload) -> Mock:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    response = Mock()
    response.read.return_value = body
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def _nodes(*online, uptime=86400):
    names = ["Basement", "Living Room", "Gaming Room"]
    macs = ["02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"]
    return {
        "get_cfg_clientlist": [
            {"alias": names[i], "mac": macs[i], "online": "1" if up else "0",
             "re_path": "0" if i == 0 else "8"}
            for i, up in enumerate(online)
        ],
        "uptime": f"Mon, 02 Nov 2026 07:51:15 -0500({uptime} secs since boot)",
    }


class SyslogSinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = pathlib.Path(self.directory.name)
        self.sink = mesh.SyslogSink(0, self.dir, retention_days=30, max_bytes_per_day=4096)
        self.addCleanup(self.sink.close)

    def test_writes_daily_file_without_priority_prefix(self):
        self.sink.handle(b"<6>Nov  2 07:44:22 kernel: hello\n", "192.168.1.1", now=T0)
        text = (self.dir / "router-2026-11-02.log").read_text()
        self.assertIn("192.168.1.1 Nov  2 07:44:22 kernel: hello", text)
        self.assertNotIn("<6>", text)

    def test_counts_pattern_hits_in_window(self):
        for i in range(10):
            self.sink.handle(b"<4>kernel: Beacon set security failed", "r", now=T0 + i)
        self.sink.handle(b"<4>kernel: unrelated", "r", now=T0 + 11)
        self.assertEqual(self.sink.hits_since(300, now=T0 + 12), 10)
        self.assertEqual(self.sink.hits_since(300, now=T0 + 400), 0)

    def test_daily_cap_drops_with_single_marker(self):
        line = b"x" * 200
        for i in range(100):
            self.sink.handle(line, "r", now=T0 + i)
        text = (self.dir / "router-2026-11-02.log").read_text()
        self.assertEqual(text.count("daily cap"), 1)
        self.assertLessEqual(len(text.encode()), 4096 + 200)
        self.assertGreater(self.sink.snapshot()["dropped_today"], 0)
        # Pattern counting continues past the cap: detection must not depend on disk.
        self.sink.handle(b"ADD/SET beacon failed", "r", now=T0 + 101)
        self.assertEqual(self.sink.hits_since(300, now=T0 + 102), 1)

    def test_allowed_sources_filter(self):
        sink = mesh.SyslogSink(0, self.dir / "f", allowed_sources=frozenset({"192.168.1.1"}))
        self.addCleanup(sink.close)
        with self.assertLogs("wan-watchdog.mesh", "WARNING") as logs:
            sink.handle(b"Beacon set security failed", "10.0.0.9", now=T0)
            sink.handle(b"Beacon set security failed", "10.0.0.9", now=T0 + 1)
        self.assertEqual(len(logs.output), 1)
        sink.handle(b"Beacon set security failed", "192.168.1.1", now=T0 + 2)
        self.assertEqual(sink.snapshot()["received"], 1)
        self.assertEqual(sink.hits_since(300, now=T0 + 3), 1)

    def test_rotation_prunes_old_files(self):
        (self.dir / "router-2026-09-01.log").write_text("old\n")
        (self.dir / "router-2026-10-30.log").write_text("recent\n")
        self.sink.handle(b"new", "r", now=T0)
        self.assertFalse((self.dir / "router-2026-09-01.log").exists())
        self.assertTrue((self.dir / "router-2026-10-30.log").exists())


class AsusRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = mesh.AsusRouter("http://192.168.1.1/", "admin", "pw", timeout=2)
        self.urlopen = patch("mesh.urllib.request.urlopen")
        self.mock = self.urlopen.start()
        self.addCleanup(self.urlopen.stop)

    def test_login_then_status(self):
        self.mock.side_effect = [_reply({"asus_token": "tok"}), _reply(_nodes(True, False, True, uptime=285))]
        nodes, uptime = self.router.status()
        self.assertEqual(uptime, 285)
        self.assertEqual([n.alias for n in nodes], ["Basement", "Living Room", "Gaming Room"])
        self.assertTrue(nodes[0].primary)
        self.assertFalse(nodes[1].online)
        status_request = self.mock.call_args_list[1].args[0]
        self.assertEqual(status_request.get_header("Cookie"), "asus_token=tok")
        self.assertIn("asusrouter", status_request.get_header("User-agent"))

    def test_expired_session_relogs_once(self):
        self.router._token = "stale"
        self.mock.side_effect = [_reply(b"<html>login</html>"), _reply({"asus_token": "new"}),
                                 _reply(_nodes(True, True, True))]
        nodes, _ = self.router.status()
        self.assertEqual(len(nodes), 3)
        self.assertEqual(self.router._token, "new")

    def test_rejected_login_is_auth_error(self):
        self.mock.side_effect = [_reply({"error_status": "3"})]
        with self.assertRaises(mesh.RouterAuthError):
            self.router.status()

    def test_http_401_is_auth_error(self):
        self.mock.side_effect = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO())
        with self.assertRaises(mesh.RouterAuthError):
            self.router.login()

    def test_unreachable_is_router_error(self):
        self.mock.side_effect = urllib.error.URLError("timed out")
        with self.assertRaises(mesh.RouterError) as caught:
            self.router.status()
        self.assertNotIsInstance(caught.exception, mesh.RouterAuthError)


class MeshMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "state.json"
        with patch.dict("os.environ", {
            "WATCHDOG_EXTERNAL_URL": "https://health.example.test/healthz",
            "BGW_ACCESS_CODE": "x", "WATCHDOG_NOTIFY_URL": "https://ntfy.example.test/topic",
            "MESH_ROUTER_URL": "http://192.168.1.1", "MESH_ROUTER_PASSWORD": "pw",
            "MESH_OFFLINE_CHECKS": "3",
        }, clear=True):
            self.cfg = watchdog.load_config()
        self.state = watchdog.State()
        self.now = T0
        self.clock = patch("time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.flush = patch("notifier.flush")
        self.flush.start()
        self.addCleanup(self.flush.stop)
        self.router = Mock(spec=mesh.AsusRouter)
        self.sink = mesh.SyslogSink(0, pathlib.Path(self.directory.name) / "logs")
        self.addCleanup(self.sink.close)
        self.monitor = mesh.MeshMonitor(self.cfg, self.router, self.sink)

    def status(self, *online, uptime=86400):
        nodes = [mesh.Node(mac=f"M{i}", alias=f"N{i}", online=up, primary=i == 0)
                 for i, up in enumerate(online)]
        self.router.status.return_value = (nodes, uptime)

    def tick(self, minutes=1):
        self.now += 60 * minutes
        return self.monitor.check(self.state, self.path)

    def events(self):
        return [entry["event"] for entry in self.state.notification_outbox]

    def test_offline_satellite_alerts_once_after_debounce_then_recovers(self):
        self.status(True, False, True)
        self.tick()
        self.tick()
        self.assertEqual(self.events(), [])
        self.tick()
        self.assertEqual(self.events(), ["node_offline:M1"])
        self.assertIn("needs a reboot", self.state.notification_outbox[0]["message"])
        self.assertEqual(self.state.notification_outbox[0]["title"], "Router mesh")
        for _ in range(30):
            self.tick()
        self.assertEqual(self.events(), ["node_offline:M1"])
        self.status(True, True, True)
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.events(), ["node_offline:M1", "recovered"])
        self.assertFalse(self.state.mesh_incident)
        self.assertEqual(self.state.mesh_offline_counts, {})

    def test_ongoing_fault_logs_once(self):
        self.status(True, False, True)
        with self.assertLogs("wan-watchdog.mesh", "INFO") as logs:
            for _ in range(10):
                self.tick()
            self.status(True, True, True)
            self.tick()
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(any("cleared node_offline:M1" in line for line in logs.output))

    def test_mesh_incident_independent_of_local_incident(self):
        self.status(True, False, True)
        for _ in range(3):
            self.tick()
        # Healthy WAN checks must not close the mesh incident.
        for _ in range(5):
            notifier.observe(self.cfg, self.state, self.path, True)
        self.assertTrue(self.state.mesh_incident)
        self.assertNotIn("recovered", self.events())

    def test_boot_grace_suppresses_rejoin_and_reports_restart(self):
        self.status(True, True, True, uptime=86400)
        self.tick()
        self.status(True, False, False, uptime=120)
        for _ in range(5):
            self.tick()
        self.assertEqual(self.events(), ["router_rebooted"])
        self.assertEqual(self.state.notification_outbox[0]["id"].split("-")[0], "notice")
        self.assertFalse(self.state.mesh_incident)

    def test_syslog_beacon_loop_alerts(self):
        self.status(True, True, True)
        for i in range(25):
            self.sink.handle(b"kernel: Beacon set security failed", "r", now=self.now + i)
        self.tick()
        self.assertEqual(self.events(), ["syslog_pattern"])

    def test_auth_failure_backs_off(self):
        self.router.status.side_effect = mesh.RouterAuthError("login rejected")
        self.tick()
        self.tick(5)
        self.assertEqual(self.router.status.call_count, 1)
        self.assertEqual(self.events(), ["login_failed"])
        self.tick(15)
        self.assertEqual(self.router.status.call_count, 2)

    def test_unreachable_router_alerts_after_debounce(self):
        self.router.status.side_effect = mesh.RouterError("timed out")
        self.tick()
        self.tick()
        self.assertEqual(self.events(), [])
        self.tick()
        self.assertEqual(self.events(), ["router_unreachable"])

    def test_old_state_file_loads(self):
        self.path.write_text(json.dumps({"reboot_count": 2, "notification_outbox": [
            {"id": "a:b", "event": "cooldown", "message": "m", "attempts": 0, "next_attempt_at": 0}]}))
        state = watchdog.State.load(self.path)
        self.assertEqual(state.mesh_incident, {})
        self.assertEqual(state.notification_outbox[0].get("title", notifier.DEFAULT_TITLE),
                         "wan-watchdog local diagnostics")


class CadenceTests(unittest.TestCase):
    """Mesh checks keep their own one-minute cadence inside a longer gateway interval."""

    def setUp(self):
        self.t = 0.0
        self.cfg = Mock(check_interval=300, mesh_check_interval=60)
        self.monitor = Mock()
        self.monitor.check.side_effect = lambda *a, **k: self.calls.append(self.t)
        self.calls = []

        def wait(seconds):
            self.t += seconds
            return False
        patches = [
            patch("watchdog.time.monotonic", side_effect=lambda: self.t),
            patch.object(watchdog._STOP, "wait", side_effect=wait),
            patch.object(watchdog._STOP, "is_set", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_mesh_runs_every_minute_across_gateway_cycles(self):
        next_mesh = 0.0
        next_mesh = watchdog.wait_for_next_cycle(self.cfg, self.monitor, None, None, next_mesh)
        self.assertEqual(self.t, 300)
        self.t += 20  # the gateway cycle's own probes take time
        watchdog.wait_for_next_cycle(self.cfg, self.monitor, None, None, next_mesh)
        # A steady minute cadence, unaffected by when the gateway cycle runs.
        self.assertEqual(self.calls, list(range(0, 601, 60)))

    def test_without_monitor_waits_once(self):
        watchdog.wait_for_next_cycle(self.cfg, None, None, None, 0.0)
        self.assertEqual(self.t, 300)
        self.assertEqual(watchdog._STOP.wait.call_count, 1)


class ParseTests(unittest.TestCase):
    def test_parse_uptime(self):
        self.assertEqual(mesh.parse_uptime("x(42 secs since boot)"), 42)
        self.assertIsNone(mesh.parse_uptime(None))


if __name__ == "__main__":
    unittest.main()
