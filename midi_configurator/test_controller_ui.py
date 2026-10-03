"""Check the GUI's read/write boundary without opening hardware MIDI ports."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import controller_ui
from .ui_scale import ZoomManager


@dataclass
class FakeIdentity:
    name: str = "FootCtrlPlus_016"
    firmware_version: int = 16


class ControllerUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        ZoomManager(self.root, persist=False)
        self.root.withdraw()
        self.temp = TemporaryDirectory()
        self.original = bytes(23646)
        self.written = b"\x03" + self.original[1:]
        self.calls = []
        outer = self
        class Transport:
            def __init__(self, *args):
                self.image = outer.original
            def open(self):
                outer.calls.append("open")
            def close(self):
                outer.calls.append("close")
            def query(self):
                return FakeIdentity()
            def read_config(self, **kwargs):
                outer.calls.append("read")
                return self.image
            def write_config(self, before, after, **kwargs):
                backups = list(Path(outer.temp.name).glob("*.json"))
                outer.assertEqual(len(backups), 1, "The backup must exist before a hardware write")
                saved = json.loads(backups[0].read_text())
                outer.assertEqual(bytes.fromhex(saved["image_hex"]), before)
                outer.calls.append("write")
                self.image = after
                return after
        self.transport_factory = Transport
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.wait_idle()
            self.dialog.close()
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.dialog.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.dialog.busy, "The controller worker did not finish")

    def create_dialog(self, on_load=None, discovery=None):
        from .project import new_project
        device = SimpleNamespace(name="FootCtrlPlus", input_name="FootCtrlPlus 0", output_name="FootCtrlPlus 1")
        self.dialog = controller_ui.ControllerDialog(
            self.root, None if on_load is not None else new_project(), {}, on_load=on_load,
            transport_factory=self.transport_factory, discovery=discovery or (lambda: [device]),
            backup_directory=self.temp.name)
        self.wait_idle()

    def plan(self, project, profiles, image):
        return SimpleNamespace(image=self.written, summaries=["Switch A: Tremolo"], warnings=[])

    def test_read_never_writes_and_explicit_write_saves_backup_first(self):
        with patch.object(controller_ui, "plan_project", self.plan), \
                patch.object(controller_ui, "config_summary", lambda image: "Controller settings"):
            self.create_dialog()
            self.dialog.read()
            self.wait_idle()
            self.assertNotIn("write", self.calls)
            self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])
            self.dialog.write()
            self.wait_idle()
            self.assertEqual(self.calls.count("write"), 1)
            self.assertEqual(self.dialog.image, self.written)
            self.assertIn("readback verified", self.dialog.status.get())

    def test_backup_failure_prevents_write(self):
        with patch.object(controller_ui, "plan_project", self.plan), \
                patch.object(controller_ui, "config_summary", lambda image: "Controller settings"), \
                patch.object(controller_ui.messagebox, "showerror") as error:
            self.create_dialog()
            self.dialog.read()
            self.wait_idle()
            with patch.object(controller_ui, "save_backup", side_effect=OSError("Disk is full")):
                self.dialog.write()
                self.wait_idle()
            self.assertNotIn("write", self.calls)
            self.assertIsNone(self.dialog.plan)
            error.assert_called_once()

    def test_configuration_changed_after_preview_prevents_write(self):
        with patch.object(controller_ui, "plan_project", self.plan), \
                patch.object(controller_ui, "config_summary", lambda image: "Controller settings"), \
                patch.object(controller_ui.messagebox, "showerror") as error:
            self.create_dialog()
            self.dialog.read()
            self.wait_idle()
            self.dialog.transport.image = b"\x01" + self.original[1:]
            self.dialog.write()
            self.wait_idle()
            self.assertNotIn("write", self.calls)
            self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])
            self.assertIn("changed since the preview", self.dialog.status.get())
            error.assert_called_once()

    def test_incompatible_configuration_prevents_write(self):
        with patch.object(controller_ui, "plan_project", side_effect=ValueError("Unsupported configuration")), \
                patch.object(controller_ui, "config_summary", lambda image: "Controller settings"), \
                patch.object(controller_ui.messagebox, "showerror"):
            self.create_dialog()
            self.dialog.read()
            self.wait_idle()
            self.dialog.write()
            self.assertNotIn("write", self.calls)
            self.assertIsNone(self.dialog.plan)

    def test_backup_hash_and_exact_bytes(self):
        image = b"\x00\xFF\x59" * 8
        path = controller_ui.save_backup(image, FakeIdentity(), self.temp.name)
        saved = json.loads(path.read_text())
        self.assertEqual(bytes.fromhex(saved["image_hex"]), image)
        self.assertEqual(saved["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(saved["image_size"], len(image))

    def test_other_firmware_is_read_only(self):
        class OtherFirmware(self.transport_factory):
            def query(self):
                return FakeIdentity("FootCtrlPlus_015", 15)
        self.transport_factory = OtherFirmware
        with patch.object(controller_ui, "plan_project") as planner, \
                patch.object(controller_ui, "config_summary", lambda image: "Controller settings"):
            self.create_dialog()
            self.dialog.read()
            self.wait_idle()
            self.assertEqual(self.dialog.image, self.original)
            self.assertIsNone(self.dialog.plan)
            self.assertIn("firmware 15", self.dialog.status.get())
            self.dialog.write()
            self.assertNotIn("write", self.calls)
            planner.assert_not_called()

    def test_import_requires_explicit_load_after_read_and_closes_after_main_thread_callback(self):
        main_thread = threading.get_ident()
        def loaded(image, identity):
            self.assertEqual(image, self.original)
            self.assertEqual(identity, FakeIdentity())
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(self.calls[-1], "read")
            self.assertTrue(self.dialog.winfo_exists())
        callback = Mock(side_effect=loaded)
        with patch.object(controller_ui, "plan_project") as planner:
            self.create_dialog(on_load=callback)
            self.assertEqual(len(self.dialog.tabs.tabs()), 1)
            self.assertEqual(self.dialog.tabs.tab(self.dialog.tabs.select(), "text"),
                             "Current controller configuration")
            self.assertIsNone(self.dialog.write_button)
            self.assertTrue(self.dialog.load_button.instate(["disabled"]))
            self.dialog.load_into_editor()
            callback.assert_not_called()
            self.dialog.read()
            self.wait_idle()
            callback.assert_not_called()
            self.assertEqual(self.dialog.image, self.original)
            self.assertTrue(self.dialog.load_button.instate(["!disabled"]))
            self.assertIn("Working mode", self.dialog.current_text.get("1.0", "end"))
            self.dialog.plan = self.plan(None, None, self.original)
            self.dialog.write()
            self.assertNotIn("write", self.calls)
            self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])
            self.dialog.load_into_editor()
            callback.assert_called_once_with(self.original, FakeIdentity())
            self.assertEqual(self.calls[-1], "close")
            self.assertFalse(self.dialog.winfo_exists())
            self.dialog.load_into_editor()
            callback.assert_called_once()
            planner.assert_not_called()

    def test_import_cancel_after_successful_read_does_not_load_an_editor(self):
        callback = Mock()
        self.create_dialog(on_load=callback)
        self.dialog.read()
        self.wait_idle()
        self.assertTrue(self.dialog.close())
        callback.assert_not_called()
        self.assertNotIn("write", self.calls)
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_controller_zoom_changes_fonts_without_reading_writing_or_loading(self):
        callback = Mock()
        self.create_dialog(on_load=callback)
        self.dialog.read()
        self.wait_idle()
        calls = list(self.calls)
        image = self.dialog.image
        self.dialog.zoom.set_percent(200)
        self.root.update_idletasks()
        self.assertEqual(str(self.dialog.current_text.cget("font")), str(self.dialog.zoom.font("mono")))
        self.assertEqual(self.dialog.zoom.font("mono").cget("size"), 20)
        self.assertIsNotNone(self.dialog.scroll.horizontal_bar)
        self.assertEqual(self.dialog.image, image)
        self.assertEqual(self.calls, calls)
        self.assertIsNone(self.dialog.write_button)
        callback.assert_not_called()

    def test_import_discovery_failure_does_not_load_or_write(self):
        callback = Mock()
        with patch.object(controller_ui.messagebox, "showerror") as error:
            self.create_dialog(on_load=callback, discovery=Mock(side_effect=OSError("MIDI unavailable")))
            self.assertTrue(self.dialog.load_button.instate(["disabled"]))
            self.dialog.load_into_editor()
            self.dialog.write()
            self.assertTrue(self.dialog.close())
            callback.assert_not_called()
            self.assertEqual(self.calls, [])
            error.assert_called_once()

    def test_import_read_failure_closes_connection_and_keeps_load_disabled(self):
        class FailedRead(self.transport_factory):
            def read_config(self, **kwargs):
                raise OSError("Controller did not answer")
        self.transport_factory = FailedRead
        callback = Mock()
        with patch.object(controller_ui.messagebox, "showerror") as error:
            self.create_dialog(on_load=callback)
            self.dialog.read()
            self.wait_idle()
            self.assertIsNone(self.dialog.image)
            self.assertIsNone(self.dialog.transport)
            self.assertTrue(self.dialog.load_button.instate(["disabled"]))
            self.assertEqual(self.calls, ["open", "close"])
            self.dialog.load_into_editor()
            self.dialog.write()
            callback.assert_not_called()
            self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])
            error.assert_called_once()

    def test_import_failed_refresh_prevents_loading_the_previous_image(self):
        callback = Mock()
        with patch.object(controller_ui.messagebox, "showerror"):
            self.create_dialog(on_load=callback)
            self.dialog.read()
            self.wait_idle()
            self.assertTrue(self.dialog.load_button.instate(["!disabled"]))
            with patch.object(self.dialog.transport, "read_config", side_effect=OSError("USB disconnected")):
                self.dialog.read()
                self.wait_idle()
            self.assertTrue(self.dialog.load_button.instate(["disabled"]))
            self.dialog.load_into_editor()
            callback.assert_not_called()
            self.assertNotIn("write", self.calls)

    def test_import_callback_failure_keeps_configuration_visible_and_allows_retry(self):
        callback = Mock(side_effect=ValueError("Unsupported controller configuration"))
        with patch.object(controller_ui.messagebox, "showerror") as error:
            self.create_dialog(on_load=callback)
            self.dialog.read()
            self.wait_idle()
            summary = self.dialog.current_text.get("1.0", "end")
            self.dialog.load_into_editor()
            callback.assert_called_once_with(self.original, FakeIdentity())
            self.assertTrue(self.dialog.winfo_exists())
            self.assertTrue(self.root.winfo_exists())
            self.assertNotIn("close", self.calls)
            self.assertEqual(self.dialog.current_text.get("1.0", "end"), summary)
            self.assertIsNotNone(self.dialog.transport)
            self.assertTrue(self.dialog.load_button.instate(["!disabled"]))
            self.assertNotIn("write", self.calls)
            error.assert_called_once_with("Unable to load controller configuration",
                                          "Unsupported controller configuration", parent=self.dialog)
            callback.side_effect = None
            self.dialog.load_into_editor()
            self.assertEqual(callback.call_count, 2)
            self.assertFalse(self.dialog.winfo_exists())
            self.assertEqual(self.calls[-1], "close")


if __name__ == "__main__":
    unittest.main()
