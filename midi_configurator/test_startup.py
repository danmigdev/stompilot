"""Verify source selection and local editing boundaries without hardware access."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import app as app_module
from .app import RawMidiDialog, StompilotApp
from .chocolate import ADVANCED_MODE, BANK_A_ROWS, CONFIG_SIZE, switch_offset
from .core import ValidationError
from .project import new_project


def controller_fixture():
    image = bytearray(CONFIG_SIZE)
    image[0] = ADVANCED_MODE
    offset = switch_offset(0, "A") + BANK_A_ROWS
    image[offset:offset + 5] = bytes([1, 0, 1, 2, 0])
    return bytes(image)


class StartupTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app = StompilotApp(self.root, start_screen=True)
        self.temp = TemporaryDirectory()
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def test_startup_requires_choice_and_does_not_preload_example_or_usb(self):
        self.assertIsNotNone(self.app.startup_frame)
        self.assertEqual(self.app.winfo_manager(), "")
        self.assertIsNone(self.app.controller_dialog)
        self.assertIsNone(self.app.path)
        self.assertFalse(self.app.dirty)
        self.assertTrue(all(not switch["states"][0]["actions"]
                            for switch in self.app.project["pages"][0]["switches"].values()))
        with patch.object(app_module.filedialog, "asksaveasfilename") as save:
            self.assertFalse(self.app.save_project())
            save.assert_not_called()

    def test_cancelled_or_invalid_file_keeps_startup_screen(self):
        with patch.object(app_module.filedialog, "askopenfilename", return_value=""):
            self.app.open_project()
        self.assertIsNotNone(self.app.startup_frame)
        path = Path(self.temp.name) / "broken.json"
        path.write_text("{}", encoding="utf-8")
        with patch.object(app_module.filedialog, "askopenfilename", return_value=str(path)), \
                patch.object(app_module.messagebox, "showerror") as error:
            self.app.open_project()
        error.assert_called_once()
        self.assertIsNotNone(self.app.startup_frame)
        self.assertFalse(self.app.dirty)

    def test_file_origin_is_visible_without_local_directory(self):
        path = Path(self.temp.name) / "my-project.json"
        path.write_text(json.dumps(new_project()), encoding="utf-8")
        with patch.object(app_module.filedialog, "askopenfilename", return_value=str(path)):
            self.app.open_project()
        self.assertIsNone(self.app.startup_frame)
        self.assertEqual(self.app.winfo_manager(), "pack")
        self.assertIn("Saved project · my-project.json", self.app.source_text.get())
        self.assertNotIn(self.temp.name, self.app.source_text.get())
        self.assertIn("Local working copy", self.app.source_text.get())
        self.app.project_name.set("Edited project")
        self.assertTrue(self.app.dirty)
        self.assertIn("modified", self.app.source_text.get())

    def test_new_choice_creates_empty_local_project(self):
        self.app.new()
        self.assertIsNone(self.app.startup_frame)
        self.assertIn("New empty project", self.app.source_text.get())
        self.assertEqual(self.app.output, "")
        self.assertFalse(self.app.dirty)

    def test_controller_read_populates_editor_with_actual_cc_and_source(self):
        identity = SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16)
        self.app._load_controller(controller_fixture(), identity)
        self.assertIsNone(self.app.startup_frame)
        self.assertIn("Chocolate Plus · FootCtrlPlus_016 · read at", self.app.source_text.get())
        self.assertIn("Local working copy", self.app.source_text.get())
        self.assertEqual(self.app.selected_state()["actions"], [{"type": "raw_midi", "message_hex": "B0 02 00"}])
        self.assertEqual(self.app.actions.item("0", "values"), ("Imported MIDI", "Ch 1", "CC 2", "0"))
        self.assertIn("Data 1 2", self.app.output)
        self.assertIsNone(self.app.path)
        self.assertFalse(self.app.dirty)

    def test_unsupported_controller_import_keeps_startup(self):
        identity = SimpleNamespace(name="FootCtrlPlus_015", firmware_version=15)
        with self.assertRaises(ValidationError):
            self.app._load_controller(controller_fixture(), identity)
        self.assertIsNotNone(self.app.startup_frame)
        image = bytearray(controller_fixture())
        image[0] = 2
        with self.assertRaises(ValidationError):
            self.app._load_controller(bytes(image), SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16))
        self.assertIsNotNone(self.app.startup_frame)

    def test_imported_cc_can_be_edited_and_invalid_values_are_rejected(self):
        self.app.new()
        self.dialog = RawMidiDialog(self.root, self.app.profiles, {"type": "raw_midi", "message_hex": "BF 7F 00"})
        self.assertEqual(self.dialog.get_action()["message_hex"], "BF 7F 00")
        self.dialog.channel.set("2")
        self.dialog.cc.set("7")
        self.dialog.value.set("10")
        self.assertEqual(self.dialog.get_action(), {"type": "raw_midi", "message_hex": "B1 07 0A"})
        self.dialog.value.set("128")
        with self.assertRaises(ValidationError):
            self.dialog.get_action()

    def test_imported_sysex_preserves_bytes_and_validates_hex_edits(self):
        self.app.new()
        self.dialog = RawMidiDialog(self.root, self.app.profiles, {"type": "raw_midi", "message_hex": "F0 7D 01 F7"})
        self.assertEqual(self.dialog.get_action(), {"type": "raw_midi", "message_hex": "F0 7D 01 F7"})
        self.dialog.hex_text.delete("1.0", "end")
        self.dialog.hex_text.insert("1.0", "F0 7D 02 F7")
        self.assertEqual(self.dialog.get_action()["message_hex"], "F0 7D 02 F7")
        self.dialog.hex_text.insert("end", " F0 01 F7")
        with self.assertRaises(ValidationError):
            self.dialog.get_action()


if __name__ == "__main__":
    unittest.main()
