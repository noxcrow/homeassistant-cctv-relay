import importlib.util
import pathlib
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
                event_key="history:10", camera_key="front", event_type="motion",
                event_time=1000.0, source="action_rule_history", history_id=10,
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:front:motion:1008", camera_key="front",
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
                event_key="webhook:front:motion:1000", camera_key="front",
                event_type="motion", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:front:motion:1008", camera_key="front",
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
                event_key="history:10", camera_key="front",
                event_type="motion", event_time=1000.0,
                source="action_rule_history", history_id=10,
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="history:11", camera_key="front",
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
                event_key="webhook:front:motion:1000", camera_key="front",
                event_type="motion", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:front:motion:1016", camera_key="front",
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
                event_key="webhook:front:lost:1000", camera_key="front",
                event_type="lost", event_time=1000.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            second_id, created = store.enqueue(
                event_key="webhook:front:lost:1008", camera_key="front",
                event_type="lost", event_time=1008.0, source="webhook",
                match_window_seconds=15,
            )
            self.assertTrue(created)
            self.assertNotEqual(first_id, second_id)
            store.close()



class CameraConfigTests(unittest.TestCase):
    def test_uses_dsm_camera_name(self):
        camera = relay.CameraConfig(
            key="front", camera_id=7, slot_name="카메라 1", camera_name="현관 CCTV"
        )
        self.assertEqual(camera.display_name, "현관 CCTV")
        self.assertIn("현관 CCTV", camera.caption)

    def test_falls_back_to_slot_and_id_without_dsm_name(self):
        camera = relay.CameraConfig(
            key="front", camera_id=7, slot_name="카메라 1"
        )
        self.assertEqual(camera.display_name, "카메라 1 (ID 7)")


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


if __name__ == "__main__":
    unittest.main()
