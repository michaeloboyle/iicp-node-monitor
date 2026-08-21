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
