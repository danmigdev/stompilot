"""Host lifecycle and file-picker checks without a GUI or MIDI ports."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .desktop import DesktopBridge, frontend_directory
from .desktop_service import DesktopService
from .runtime_paths import data_directory, profile_directory, resource_dir


class DesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.discovery = Mock(return_value=[])
        self.service = DesktopService(data_directory=self.temp.name, discovery=self.discovery)
        self.bridge = DesktopBridge(self.service)

    def edited_project(self):
        self.service.request("new_project")
        project = deepcopy(self.service.snapshot()["project"])
        project["name"] = "Unsaved edit"
        self.assertTrue(self.service.request("set_project", {"project": project})["ok"])

    def test_host_bootstrap_exposes_real_service_without_midi_discovery(self):
        response = self.bridge.request("bootstrap")
        self.assertTrue(response["ok"])
        self.assertIsNone(response["data"]["project"])
        self.discovery.assert_not_called()

    def test_dirty_native_close_requests_a_ui_decision_and_keeps_service_open(self):
        self.edited_project()
        with patch.object(self.bridge, "_dispatch") as dispatch:
            self.assertFalse(self.bridge._on_closing())
        dispatch.assert_called_once_with("stompilot-close-request")
        self.assertTrue(self.service.snapshot()["dirty"])
        self.assertTrue(self.service.request("set_zoom", {"percent": 150})["ok"])

    def test_busy_native_close_is_blocked_and_notifies_interface(self):
        self.service._commands.acquire()
        try:
            with patch.object(self.bridge, "_dispatch") as dispatch:
                self.assertFalse(self.bridge._on_closing())
            self.assertEqual(dispatch.call_args.args[0], "stompilot-notice")
            self.assertFalse(self.bridge.request("close_application", {"discard": True})["ok"])
        finally:
            self.service._commands.release()

    def test_close_application_requires_explicit_discard_of_unsaved_changes(self):
        self.edited_project()
        before = self.service.snapshot()
        self.assertFalse(self.bridge.request("close_application")["ok"])
        self.assertEqual(self.service.snapshot(), before)
        self.assertTrue(self.bridge.request("close_application", {"discard": True})["ok"])
        self.assertTrue(self.bridge._allow_close)
        self.assertFalse(self.service.request("new_project")["ok"])

    def test_clean_native_close_releases_service_and_unknown_requests_are_rejected(self):
        self.assertFalse(self.bridge.request("arbitrary_command", {"value": "ignored"})["ok"])
        self.assertTrue(self.bridge._on_closing())
        self.assertFalse(self.service.request("new_project")["ok"])

    def test_host_title_reflects_saved_or_modified_working_copy(self):
        window = self.bridge._window = Mock()
        self.bridge.request("new_project")
        window.set_title.assert_called_with("Stompilot")
        project = deepcopy(self.service.snapshot()["project"])
        project["name"] = "Changed title"
        self.bridge.request("set_project", {"project": project})
        window.set_title.assert_called_with("Stompilot *")

    def test_picker_uses_native_open_save_filters_and_handles_cancellation(self):
        self.bridge._window = Mock()
        selected = Path(self.temp.name) / "selected.json"
        self.bridge._window.create_file_dialog.return_value = (str(selected),)
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, SAVE=30))
        with patch.dict("sys.modules", {"webview": fake_webview}):
            self.assertEqual(self.bridge._pick_file("open_project", file_types=(("JSON files", "*.json"),)), str(selected))
            self.bridge._window.create_file_dialog.assert_called_with(
                10, allow_multiple=False, save_filename="", file_types=("JSON files (*.json)",))
            self.bridge._pick_file("save_project", "stompilot.json", (("JSON files", "*.json"),))
            self.bridge._window.create_file_dialog.assert_called_with(
                30, allow_multiple=False, save_filename="stompilot.json", file_types=("JSON files (*.json)",))
            self.bridge._window.create_file_dialog.return_value = None
            self.assertIsNone(self.bridge._pick_file("import_profile"))

    def test_custom_profiles_and_compiled_frontend_are_outside_bundled_definitions(self):
        self.assertEqual(frontend_directory(), resource_dir().parent / "frontend" / "build")
        self.assertEqual(profile_directory(), data_directory() / "profiles")
        self.assertNotEqual(profile_directory(), resource_dir() / "profiles")


if __name__ == "__main__":
    unittest.main()
