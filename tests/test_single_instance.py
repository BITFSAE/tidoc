"""Single-instance process lock and secondary-launch handoff tests."""

from __future__ import annotations

import threading

from tidoc.app import _serve_instance_requests
from tidoc.single_instance import SingleInstance


def test_single_instance_lock_is_exclusive_and_recoverable(tmp_path):
    first = SingleInstance(tmp_path)
    second = SingleInstance(tmp_path)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_secondary_activation_request_round_trip(tmp_path):
    primary = SingleInstance(tmp_path)
    secondary = SingleInstance(tmp_path)
    bindle = tmp_path / "材料包.tidoc"
    bindle.write_bytes(b"test")

    assert primary.acquire() is True
    assert secondary.acquire() is False
    secondary.send_activation(str(bindle))
    assert primary.pop_requests() == [
        {
            "action": "activate",
            "target_instance_id": primary.instance_id,
            "launch_file": str(bindle),
        }
    ]
    assert primary.pop_requests() == []
    primary.release()


def test_primary_forwards_launch_file_and_activates_window(tmp_path):
    instance = SingleInstance(tmp_path / "runtime")
    secondary = SingleInstance(tmp_path / "runtime")
    bindle = tmp_path / "材料包.tidoc"
    bindle.write_bytes(b"test")
    assert instance.acquire() is True
    assert secondary.acquire() is False
    secondary.send_activation(str(bindle))

    class FakeApi:
        def __init__(self):
            self.paths = []

        def queue_launch_file(self, path):
            self.paths.append(path)

    stop = threading.Event()

    class FakeWindow:
        native = None

        def __init__(self):
            self.shown = 0
            self.scripts = []

        def show(self):
            self.shown += 1

        def evaluate_js(self, script):
            self.scripts.append(script)
            stop.set()

    api = FakeApi()
    window = FakeWindow()
    _serve_instance_requests(instance, api, window, stop)

    assert api.paths == [str(bindle)]
    assert window.shown == 1
    assert window.scripts == [
        "window.handleSecondaryLaunch && window.handleSecondaryLaunch();"
    ]
    instance.release()


def test_stale_request_for_previous_instance_is_ignored(tmp_path):
    first = SingleInstance(tmp_path)
    stale_sender = SingleInstance(tmp_path)
    assert first.acquire() is True
    assert stale_sender.acquire() is False
    first.release()
    stale_sender.send_activation("旧.tidoc")

    current = SingleInstance(tmp_path)
    assert current.acquire() is True
    assert current.pop_requests() == []
    current.release()
