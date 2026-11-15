"""The Android app's Python layer against the simulated controller and TANK.

MidiHub (Kotlin) is replaced by a fake with the same Python-facing methods, so
these tests cover android_midi.py and stompilot_android.py as the app runs them.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from . import chocolate_transport
from .chocolate import CURRENT_PAGE_OFFSET, PAGE_COUNT_OFFSET
from .test_chocolate_transport import FakeController, FakeMessage
from .test_tank_check import FakeTank

ANDROID_PYTHON = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "python"
sys.path.insert(0, str(ANDROID_PYTHON))
import stompilot_android  # noqa: E402


class FakeSubscription:
    def __init__(self, device):
        self.device = device
        self.closed = False

    def poll(self):
        if not self.device.input.pending:
            return None
        return bytes(self.device.input.pending.pop(0).bytes()).hex(" ").upper()

    def close(self):
        self.closed = True


class FakeHub:
    """The methods of MidiHub that Python calls, with Android-style port names."""

    def __init__(self, devices):
        self.devices = devices

    def inputNames(self):
        return [f"{label} 0" for label in self.devices]

    def outputNames(self):
        return [f"{label} 1" for label in self.devices]

    def _device(self, name):
        return self.devices[name.rsplit(" ", 1)[0]]

    def connection(self, name):
        return self._device(name)

    def subscribe(self, name):
        return FakeSubscription(self._device(name))

    def send(self, name, hex_message):
        self._device(name).send(FakeMessage(bytes.fromhex(hex_message)))


class FakeDocuments:
    def __init__(self, path=None):
        self.path = path
        self.kinds = []

    def pick(self, kind, suggested_name):
        self.kinds.append((kind, suggested_name))
        return self.path


class AndroidServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.controller = FakeController()
        self.controller.name = b"FootCtrlPlus_013\0"
        self.tank = FakeTank()
        self.hub = FakeHub({"FootCtrlPlus (3)": self.controller, "USB-Midi (4)": self.tank})
        self.documents = FakeDocuments()
        self.app = stompilot_android.create(self.directory / "data", self.documents, self.hub)

    def tearDown(self):
        chocolate_transport.use_backend(None)
        self.temporary.cleanup()

    def request(self, method, payload=None):
        result = json.loads(self.app.request_json(method, json.dumps(payload or {})))
        self.assertTrue(result["ok"], result)
        return result["data"]

    def test_the_editor_finds_and_reads_the_controller_through_the_android_backend(self):
        devices = self.request("controller_scan")["controller"]["devices"]
        self.assertEqual([device["name"] for device in devices], ["FootCtrlPlus (3)"])
        controller = self.request("controller_read", {"device_name": "FootCtrlPlus (3)"})["controller"]
        self.assertTrue(controller["connected"])
        self.assertEqual(controller["identity"], {"name": "FootCtrlPlus_013", "firmware_version": 13})

    def test_live_requests_read_the_tank_and_the_current_page(self):
        tank = self.request("tank_state")
        self.assertEqual((tank["name"], tank["preset"], tank["volume"]), ("TANK MINI_009", "7B", 76))
        self.assertEqual(next(block for block in tank["blocks"] if block["id"] == "mod"),
                         {"id": "mod", "label": "MOD", "on": False, "model": "Chorus"})
        self.controller.image[PAGE_COUNT_OFFSET] = 2
        self.controller.image[CURRENT_PAGE_OFFSET] = 1
        self.assertEqual(self.request("chocolate_page"), {"page": 2, "pages": 3, "device": "FootCtrlPlus (3)"})

    def test_the_current_page_is_not_read_while_the_editor_holds_the_controller(self):
        self.request("controller_scan")
        self.request("controller_read", {"device_name": "FootCtrlPlus (3)"})
        result = json.loads(self.app.request_json("chocolate_page", "{}"))
        self.assertFalse(result["ok"])
        self.assertIn("Disconnect it to start the live view", result["error"]["message"])

    def test_files_come_from_the_android_picker(self):
        project = Path(__file__).resolve().parent.parent / "examples" / "tank_mini_live_boost.json"
        self.documents.path = str(project)
        snapshot = self.request("open_project")
        self.assertEqual(snapshot["project"]["name"], "Tremolo and solo boost")
        self.assertEqual(self.documents.kinds, [("open_project", "")])
        self.documents.path = None
        self.request("save_project", {"save_as": True})
        self.assertEqual(self.documents.kinds[-1], ("save_project", "tank_mini_live_boost.json"))

    def test_invalid_payloads_are_reported(self):
        result = json.loads(self.app.request_json("bootstrap", "{"))
        self.assertEqual(result, {"ok": False, "error": {"message": "The request payload is not valid JSON."}})


if __name__ == "__main__":
    unittest.main()
