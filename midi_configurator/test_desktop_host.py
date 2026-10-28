"""Host lifecycle and file-picker checks without a GUI or MIDI ports."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .desktop import DesktopBridge, frontend_directory, window_placement
from .desktop_service import DesktopService
from .runtime_paths import data_directory, profile_directory, resource_dir


# A portrait monitor left of the main one, as pywebview reports them in logical pixels.
SCREENS = [SimpleNamespace(x=-1050, y=0, width=1050, height=1680),
           SimpleNamespace(x=0, y=0, width=3840, height=1600)]
DEFAULT = {"x": None, "y": None, "width": 1280, "height": 860, "maximized": False}


def place(saved=None, **changes):
    if saved is None and changes:
        saved = {"x": 100, "y": 80, "width": 1000, "height": 700, "maximized": False} | changes
    return window_placement(saved, SCREENS, (1280, 860), (620, 440))


def rectangle(x, y, width, height):
    return SimpleNamespace(X=x, Y=y, Width=width, Height=height)


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

    def test_saved_window_bounds_are_reused_only_where_the_title_bar_is_on_a_screen(self):
        self.assertEqual(place(), DEFAULT)
        self.assertEqual(place(x=100), {"x": 100, "y": 80, "width": 1000, "height": 700, "maximized": False})
        self.assertEqual(place(x=-1000, y=200, height=1000)["x"], -1000)
        self.assertTrue(place(maximized=True)["maximized"])
        # Windows snapped to the top start a few invisible border pixels above it.
        self.assertEqual(place(y=-10)["y"], -10)
        # A disconnected monitor, a title bar above or below every screen, or only 99 visible pixels.
        for changes in ({"x": 9000}, {"y": -40}, {"y": 1570}, {"x": 3741}):
            with self.subTest(changes=changes):
                self.assertEqual(place(**changes), DEFAULT)
        small = place(width=300, height=200)
        self.assertEqual((small["width"], small["height"]), (620, 440))
        large = place(x=-1050, width=2000, height=3000)
        self.assertEqual((large["width"], large["height"]), (1050, 1680))

    def test_closing_saves_the_normal_bounds_in_logical_pixels_and_never_blocks_closing(self):
        states = SimpleNamespace(Normal="Normal", Maximized="Maximized", Minimized="Minimized")
        forms = {"System": SimpleNamespace(), "System.Windows": SimpleNamespace(),
                 "System.Windows.Forms": SimpleNamespace(FormWindowState=states)}
        # Maximized at 150% scaling: the restore bounds are saved, not the maximized ones.
        self.bridge._window = SimpleNamespace(native=SimpleNamespace(
            WindowState=states.Maximized, Bounds=rectangle(-12, -12, 5784, 2424),
            RestoreBounds=rectangle(150, 120, 1500, 1050), _scale=1.5))
        with patch.dict("sys.modules", forms):
            self.assertTrue(self.bridge._on_closing())
        self.assertEqual(self.service.window_geometry(),
                         {"x": 100, "y": 80, "width": 1000, "height": 700, "maximized": True})
        self.bridge._allow_close = True
        self.bridge._window = SimpleNamespace(native=SimpleNamespace(
            WindowState=states.Normal, Bounds=rectangle(-1000, 200, 900, 1000),
            RestoreBounds=rectangle(0, 0, 1, 1), _scale=1.0))
        with patch.dict("sys.modules", forms):
            self.assertTrue(self.bridge._on_closing())
        self.assertEqual(self.service.window_geometry(),
                         {"x": -1000, "y": 200, "width": 900, "height": 1000, "maximized": False})
        self.bridge._window = SimpleNamespace(native=None)
        self.assertTrue(self.bridge._on_closing())
        self.assertEqual(self.service.window_geometry()["x"], -1000)

    def test_custom_profiles_and_compiled_frontend_are_outside_bundled_definitions(self):
        self.assertEqual(frontend_directory(), resource_dir().parent / "frontend" / "build")
        self.assertEqual(profile_directory(), data_directory() / "profiles")
        self.assertNotEqual(profile_directory(), resource_dir() / "profiles")


if __name__ == "__main__":
    unittest.main()
