import ast
import importlib.util
import pathlib
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys

MODULE = pathlib.Path(__file__).parents[1] / "custom_components/cctv_relay/relay.py"
spec = importlib.util.spec_from_file_location("cctv_relay_relay", MODULE)
relay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay
assert spec.loader is not None
spec.loader.exec_module(relay)


class EventStoreDedupTests(unittest.TestCase):
    def test_cross_source_events_match_within_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, created = store.enqueue(
                event_key="history:10", camera_key="7", event_type="motion",
                event_time=1000.0, source="action_rule_history", history_id=10,
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:7:motion:1008", camera_key="7",
                event_type="motion", event_time=1008.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertFalse(created)
            self.assertEqual(first_id, second_id)
            store.close()

    def test_same_source_motion_burst_is_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, created = store.enqueue(
                event_key="webhook:7:motion:1000", camera_key="7",
                event_type="motion", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:7:motion:1008", camera_key="7",
                event_type="motion", event_time=1008.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertFalse(created)
            self.assertEqual(first_id, second_id)
            store.close()

    def test_same_source_history_motion_burst_is_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, created = store.enqueue(
                event_key="history:10", camera_key="7",
                event_type="motion", event_time=1000.0,
                source="action_rule_history", history_id=10,
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="history:11", camera_key="7",
                event_type="motion", event_time=1008.0,
                source="action_rule_history", history_id=11,
                match_window_seconds=15,
            )
            self.assertFalse(created)
            self.assertEqual(first_id, second_id)
            store.close()

    def test_motion_outside_burst_window_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, created = store.enqueue(
                event_key="webhook:7:motion:1000", camera_key="7",
                event_type="motion", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:7:motion:1016", camera_key="7",
                event_type="motion", event_time=1016.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            self.assertNotEqual(first_id, second_id)
            store.close()

    def test_same_source_non_motion_events_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, created = store.enqueue(
                event_key="webhook:7:lost:1000", camera_key="7",
                event_type="lost", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:7:lost:1008", camera_key="7",
                event_type="lost", event_time=1008.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            self.assertNotEqual(first_id, second_id)
            store.close()



class EventStoreSafetyTests(unittest.TestCase):
    def test_active_queue_limit_rejects_new_unique_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            store.enqueue(
                event_key="one", camera_key="7", event_type="lost",
                event_time=1000.0, source="webhook", max_active_events=1,
            )
            with self.assertRaises(relay.QueueFullError):
                store.enqueue(
                    event_key="two", camera_key="7", event_type="lost",
                    event_time=1001.0, source="webhook", max_active_events=1,
                )
            store.close()

    def test_deduplicated_event_is_allowed_when_queue_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            first_id, _ = store.enqueue(
                event_key="same", camera_key="7", event_type="motion",
                event_time=1000.0, source="webhook", max_active_events=1,
            )
            second_id, created = store.enqueue(
                event_key="same", camera_key="7", event_type="motion",
                event_time=1000.0, source="webhook", max_active_events=1,
            )
            self.assertFalse(created)
            self.assertEqual(first_id, second_id)
            store.close()

    def test_failed_event_is_not_claimed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = relay.EventStore(str(pathlib.Path(tmp) / "queue.db"))
            event_id, _ = store.enqueue(
                event_key="fail", camera_key="7", event_type="lost",
                event_time=1000.0, source="webhook",
            )
            claimed = store.claim_next("7")
            self.assertEqual(claimed["id"], event_id)
            store.mark_failed(event_id, "permanent error")
            self.assertIsNone(store.claim_next("7"))
            self.assertEqual(store.summary()["counts"].get("failed"), 1)
            store.close()

    def test_parse_event_time_rejects_far_future_timestamp(self):
        with patch.object(relay.time, "time", return_value=1_700_000_000.0):
            with self.assertRaises(ValueError):
                relay.parse_event_time(1_700_000_301.0)


class CameraConfigTests(unittest.TestCase):
    def test_uses_dsm_camera_name(self):
        camera = relay.CameraConfig(
            key="7", camera_id=7, slot_name="카메라 ID 7", camera_name="테스트 카메라"
        )
        self.assertEqual(camera.display_name, "테스트 카메라")
        self.assertIn("테스트 카메라", camera.caption)

    def test_falls_back_to_slot_and_id_without_dsm_name(self):
        camera = relay.CameraConfig(
            key="7", camera_id=7, slot_name="카메라 ID 7"
        )
        self.assertEqual(camera.display_name, "카메라 ID 7 (ID 7)")


class SynologyLoginCompatibilityTests(unittest.TestCase):
    def _client(self):
        client = relay.SynologyClient(
            "https://dsm.example", None, 5, 5, verify_ssl=False
        )
        client._api_info = {
            "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7},
            "SYNO.SurveillanceStation.Camera": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 9},
            "SYNO.SurveillanceStation.Recording": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 6},
            "SYNO.SurveillanceStation.ActionRule": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
        }
        return client

    def test_login_uses_minimal_surveillance_station_sid_request(self):
        client = self._client()
        client._request_json = Mock(return_value={"sid": "abc123"})
        session = client.login("relay", "secret")
        self.assertEqual(session.sid, "abc123")
        _, params = client._request_json.call_args.args[:2]
        self.assertEqual(params["session"], "SurveillanceStation")
        self.assertEqual(params["format"], "sid")
        self.assertNotIn("enable_syno_token", params)
        self.assertNotIn("enable_device_token", params)
        self.assertTrue(client._request_json.call_args.kwargs.get("post", False))


    def test_post_routes_api_in_query_without_credentials(self):
        client = self._client()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read = Mock(return_value=b'{"success": true, "data": {"sid": "abc123"}}')
        client._opener.open = Mock(return_value=response)
        client.login("relay-user", "super-secret")
        request = client._opener.open.call_args.args[0]
        self.assertIn("api=SYNO.API.Auth", request.full_url)
        self.assertNotIn("relay-user", request.full_url)
        self.assertNotIn("super-secret", request.full_url)
        body = request.data.decode("utf-8")
        self.assertIn("account=relay-user", body)
        self.assertIn("passwd=super-secret", body)


    def test_login_falls_back_to_get_when_post_returns_auth_400(self):
        client = self._client()
        client._request_json = Mock(side_effect=[
            relay.SynologyAPIError(400, "login"),
            {"sid": "fallback-sid"},
        ])
        session = client.login("relay", "secret")
        self.assertEqual(session.sid, "fallback-sid")
        self.assertEqual(client._request_json.call_count, 2)
        self.assertTrue(client._request_json.call_args_list[0].kwargs.get("post", False))
        self.assertFalse(client._request_json.call_args_list[1].kwargs.get("post", False))

    def test_login_does_not_fallback_for_non_400_auth_errors(self):
        client = self._client()
        client._request_json = Mock(side_effect=relay.SynologyAPIError(403, "login"))
        with self.assertRaises(relay.SynologyAPIError) as ctx:
            client.login("relay", "secret")
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(client._request_json.call_count, 1)

    def test_tls_context_requires_tls_1_2_or_newer(self):
        client = self._client()
        self.assertGreaterEqual(
            client.ssl_context.minimum_version, ssl.TLSVersion.TLSv1_2
        )

    def test_logout_uses_sid_without_synotoken(self):
        client = self._client()
        client._request_json = Mock(return_value={})
        client.logout(relay.SynologySession("abc123", "token-value"))
        _, params = client._request_json.call_args.args[:2]
        self.assertEqual(params["_sid"], "abc123")
        self.assertNotIn("SynoToken", params)
        self.assertTrue(client._request_json.call_args.kwargs.get("post", False))


class SynologyCameraDiscoveryTests(unittest.TestCase):
    def _client(self):
        client = relay.SynologyClient(
            "https://dsm.example", None, 5, 5, verify_ssl=False
        )
        client._api_info = {
            "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7},
            "SYNO.SurveillanceStation.Camera": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 9},
            "SYNO.SurveillanceStation.Recording": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 6},
            "SYNO.SurveillanceStation.ActionRule": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
        }
        session = Mock()
        session.__enter__ = Mock(return_value=Mock())
        session.__exit__ = Mock(return_value=False)
        client.session = Mock(return_value=session)
        return client

    def test_merges_cameras_from_v9_and_v1_compatibility_calls(self):
        client = self._client()
        client.call_json = Mock(side_effect=[
            {"cameras": []},
            {"cameras": [{"id": 1, "name": "A"}]},
            {"cameras": []},
            {"cameras": []},
            {"cameras": []},
            {"cameras": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
            {"cameras": []},
        ])
        self.assertEqual(client.camera_names("user", "pass"), {1: "A", 2: "B"})
        calls = client.call_json.call_args_list
        self.assertTrue(any(call.args[2] == 1 for call in calls))
        self.assertTrue(any(call.args[4].get("basic") == "true" for call in calls))

    def test_survives_failed_variants_when_another_returns_cameras(self):
        client = self._client()
        client.call_json = Mock(side_effect=[
            relay.SynologyAPIError(105, "List"),
            {"cameras": []},
            {"cameras": [{"id": 2, "name": "B"}]},
            {"cameras": []},
            {"cameras": []},
            {"cameras": []},
            {"cameras": []},
        ])
        self.assertEqual(client.camera_names("user", "pass"), {2: "B"})

    def test_camera_with_empty_name_remains_selectable_by_id(self):
        client = self._client()
        client.call_json = Mock(side_effect=[
            {"cameras": [{"id": 7, "name": ""}]},
            *[{"cameras": []} for _ in range(6)],
        ])
        self.assertEqual(client.camera_names("user", "pass"), {7: "카메라 ID 7"})

    def test_zero_cameras_is_not_reported_as_permission_error(self):
        client = self._client()
        client.call_json = Mock(side_effect=[{"cameras": []} for _ in range(7)])
        self.assertEqual(client.list_cameras("user", "pass"), [])

class DurationParsingTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertAlmostEqual(
            relay._parse_ffmpeg_duration("Duration: 00:01:02.50, start: 0.0"),
            62.5,
        )

    def test_zero_duration(self):
        self.assertEqual(
            relay._parse_ffmpeg_duration("Duration: 00:00:00.00, start: 0.0"),
            0.0,
        )

    def test_validator_rejects_zero_duration(self):
        result = Mock(stderr="Duration: 00:00:00.00, start: 0.0")
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "clip.mp4"
            video.write_bytes(b"not-empty")
            with patch.object(relay.subprocess, "run", return_value=result):
                with self.assertRaises(relay.RelayError):
                    relay._validate_video_duration(video, "/usr/bin/ffmpeg")

    def test_validator_accepts_nonzero_duration(self):
        result = Mock(stderr="Duration: 00:00:08.00, start: 0.0")
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "clip.mp4"
            video.write_bytes(b"not-empty")
            with patch.object(relay.subprocess, "run", return_value=result):
                relay._validate_video_duration(video, "/usr/bin/ffmpeg")


class WebhookNotificationRegressionTests(unittest.TestCase):
    def _function_source(self, path: pathlib.Path, function_name: str) -> str:
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                segment = ast.get_source_segment(source, node)
                self.assertIsNotNone(segment)
                return segment or ""
        self.fail(f"function {function_name} not found in {path}")

    def test_setup_entry_does_not_recreate_webhook_notice(self):
        path = pathlib.Path(__file__).parents[1] / "custom_components/cctv_relay/__init__.py"
        source = self._function_source(path, "async_setup_entry")
        self.assertNotIn("persistent_notification.async_create", source)
        self.assertNotIn("_async_show_initial_webhook_notification", source)

    def test_new_config_entry_creates_notice_after_reconfigure_branch(self):
        path = pathlib.Path(__file__).parents[1] / "custom_components/cctv_relay/config_flow.py"
        source = self._function_source(path, "async_step_cameras")
        self.assertEqual(source.count("_async_show_initial_webhook_notification("), 1)
        self.assertLess(source.index("if self._reconfigure:"), source.index("_async_show_initial_webhook_notification("))
        reconfigure_block = source[source.index("if self._reconfigure:"):source.index("webhook_id = webhook.async_generate_id()")]
        self.assertNotIn("_async_show_initial_webhook_notification", reconfigure_block)

    def test_notice_supports_only_production_event_types(self):
        path = pathlib.Path(__file__).parents[1] / "custom_components/cctv_relay/config_flow.py"
        source = self._function_source(path, "_async_show_initial_webhook_notification")
        self.assertIn('("motion", "lost", "restored")', source)
        self.assertNotIn('"test"', source)


if __name__ == "__main__":
    unittest.main()
