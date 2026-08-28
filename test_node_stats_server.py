import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "node_stats_server", Path(__file__).with_name("node-stats-server.py")
)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class MonitorCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.old = (monitor.LOGS, monitor.EVENTS, monitor.HISTORY, monitor.DIRECTORY,
                    monitor.DIRECTORY_EXPLICIT, monitor.MODE, monitor.HEALTH_URL)
        monitor._poll["task_beats"] = []
        monitor._public_probe.update({"endpoint": None, "node_id": None,
                                      "checked": 0.0, "result": None})

    def tearDown(self):
        (monitor.LOGS, monitor.EVENTS, monitor.HISTORY, monitor.DIRECTORY,
         monitor.DIRECTORY_EXPLICIT, monitor.MODE, monitor.HEALTH_URL) = self.old

    def write_events(self, rows):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        monitor.configure(log_dir=td.name)
        with open(monitor.EVENTS, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_current_and_legacy_registration_failures_are_bad(self):
        self.write_events([
            {"ts": "2026-08-21T10:00:00Z", "event": "register_fail"},
            {"ts": "2026-08-21T10:00:01Z", "event": "register_error"},
        ])
        self.assertEqual([e["cls"] for e in monitor.read_events()], ["bad", "bad"])

    def test_unknown_event_is_neutral_not_heartbeat(self):
        self.write_events([{"ts": "2026-08-21T10:00:00Z", "event": "future_failure"}])
        event = monitor.read_events()[0]
        self.assertEqual(event["cls"], "unknown")
        self.assertEqual(event["source"], "node_event")

    def test_registry_inventory_is_dynamic_and_paginated(self):
        monitor.configure(directory_url="https://directory.example/api", mode="public")
        def get(url, timeout=15):
            if url.endswith("registry/stats"):
                return {"active_nodes": 1, "version": "test"}
            if url.endswith("registry/intents"):
                return {"intents": [{"intent": "urn:iicp:intent:image:generate:v1", "active_nodes": 1}]}
            if "registry/nodes" in url:
                return {"total": 1, "nodes": [{"node_id_prefix": "abc12345", "intents": [
                    "urn:iicp:intent:image:generate:v1"], "models": ["example"],
                    "health_summary": {"label": "healthy"}}]}
            raise AssertionError(url)
        with mock.patch.object(monitor, "_get", side_effect=get), \
             mock.patch.object(monitor, "current_node", return_value=None):
            result = monitor.network_snapshot()
        self.assertEqual(result["inventory_source"], "registry_api")
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["intents"][0]["urn"], "urn:iicp:intent:image:generate:v1")

    def test_registry_failure_uses_explicit_partial_fallback(self):
        monitor.configure(directory_url="https://old.example/api", mode="public")
        def get(url, timeout=15):
            if "registry/" in url:
                raise OSError("not found")
            return {"nodes": []}
        with mock.patch.object(monitor, "_get", side_effect=get), \
             mock.patch.object(monitor, "current_node", return_value=None):
            result = monitor.network_snapshot()
        self.assertEqual(result["inventory_source"], "legacy_discovery_partial")
        self.assertFalse(result["inventory_complete"])

    def test_local_only_performs_no_directory_request(self):
        monitor.configure(mode="local_only")
        with mock.patch.object(monitor, "_get") as get, \
             mock.patch.object(monitor, "current_node", return_value=None):
            result = monitor.network_snapshot()
        get.assert_not_called()
        self.assertEqual(result["inventory_source"], "disabled_local_only")

    def test_configuration_overrides_paths(self):
        monitor.configure(directory_url="https://private.example/api", log_dir="/tmp/example-logs",
                          health_url="http://127.0.0.1:9484/iicp/health", mode="private")
        self.assertEqual(monitor.DIRECTORY, "https://private.example/api")
        self.assertEqual(monitor.EVENTS, "/tmp/example-logs/events.jsonl")
        self.assertEqual(monitor.MODE, "private")

    def test_health_endpoint_precedes_snapshot_and_logs(self):
        monitor.configure(health_url="http://127.0.0.1:9484/iicp/health")
        with mock.patch.object(monitor, "_get", return_value={"status": "ok"}):
            result = monitor.runtime_health({"alive": False})
        self.assertEqual(result["source"], "runtime_endpoint")
        self.assertTrue(result["healthy"])

    def test_health_falls_back_to_log_inference(self):
        monitor.configure(health_url="http://127.0.0.1:1/iicp/health")
        with mock.patch.object(monitor, "_get", side_effect=OSError("offline")), \
             mock.patch.dict(os.environ, {"IICP_NODE_NAME": "missing-test-node"}):
            result = monitor.runtime_health({"alive": True, "log_age_s": 2})
        self.assertEqual(result["source"], "log_freshness_inference")

    def test_public_probe_refuses_non_https_and_non_public_targets(self):
        with mock.patch.object(monitor.socket, "getaddrinfo") as resolve:
            result = monitor.probe_public_endpoint("http://127.0.0.1:9484/private")
        resolve.assert_not_called()
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason"], "unsafe_or_unsupported_endpoint")

        with mock.patch.object(monitor.socket, "getaddrinfo", return_value=[
                (monitor.socket.AF_INET, monitor.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            result = monitor.probe_public_endpoint("https://node.example")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason"], "unsafe_address")

    def test_public_probe_pins_validated_address_and_checks_health_identity(self):
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps(
            {"status": "ok", "node_id": "node-1"}).encode()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(monitor.socket, "getaddrinfo", return_value=[
                (monitor.socket.AF_INET, monitor.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), \
             mock.patch.object(monitor.http.client, "HTTPSConnection", return_value=connection), \
             mock.patch.object(monitor.socket, "create_connection", return_value=mock.Mock()) as connect:
            result = monitor.probe_public_endpoint("https://node.example/anything", "node-1")
            connection._create_connection(("ignored", 443), 2)
        self.assertEqual(result["state"], "reachable")
        self.assertEqual(result["reason"], "health_verified")
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ("GET", "/iicp/health"))
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    def test_public_probe_does_not_follow_redirects_or_expose_exception_text(self):
        response = mock.Mock(status=302)
        response.read.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(monitor.socket, "getaddrinfo", return_value=[
                (monitor.socket.AF_INET, monitor.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), \
             mock.patch.object(monitor.http.client, "HTTPSConnection", return_value=connection):
            result = monitor.probe_public_endpoint("https://node.example", "node-1")
        self.assertEqual(result["state"], "unreachable")
        self.assertEqual(result["reason"], "http_status")
        self.assertEqual(result["http_status"], 302)
        self.assertNotIn("exception", json.dumps(result).lower())

    def test_snapshot_keeps_monitor_and_directory_reachability_independent(self):
        local = {"node_id": "node-1", "endpoint": "https://node.example", "alive": True}
        network = {"mesh": {}, "nodes": [{"ours": True, "reachable": True}],
                   "inventory_source": "registry_api", "inventory_complete": True}
        measured = {"state": "unreachable", "source": "monitor_measured",
                    "reason": "connect_or_tls_failure", "latency_ms": 4}
        with mock.patch.object(monitor, "current_node", return_value=local), \
             mock.patch.object(monitor, "network_snapshot", return_value=network), \
             mock.patch.object(monitor, "runtime_health", return_value={"healthy": True}), \
             mock.patch.object(monitor, "public_endpoint_reachability", return_value=measured):
            result = monitor.snapshot()
        self.assertEqual(result["directory"]["reachable"], True)
        self.assertEqual(result["public_endpoint_reachability"]["state"], "unreachable")
        self.assertIn("directory and monitor reachability evidence disagree",
                      result["security"]["anomalies"])

    def test_task_delta_is_one_observation_not_fabricated_events(self):
        event = monitor.record_task_delta(10, 14, 1234)
        self.assertEqual(event["event"], "directory_task_delta")
        self.assertEqual(event["t"], 1234)
        self.assertIn("increased by 4", event["detail"])
        self.assertEqual(len(monitor._poll["task_beats"]), 1)

    def test_operator_secret_file_is_never_opened(self):
        with mock.patch("builtins.open", side_effect=AssertionError("secret file opened")), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = monitor.operator_secret_status()
        self.assertEqual(result["state"], "unavailable")

    def test_render_escapes_server_and_client_controlled_values(self):
        payload = "<img src=x onerror=alert(1)>"
        snap = {"ts": payload, "local": {"alive": True, "node_id": payload},
                "directory": {"models": [payload]}, "mesh": {},
                "security": {"operator": payload, "anomalies": [payload]}, "error": payload}
        rendered = monitor.render(snap)
        self.assertNotIn(payload, rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("const h=", rendered)


if __name__ == "__main__":
    unittest.main()
