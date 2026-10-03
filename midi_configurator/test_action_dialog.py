"""Exercise global preset actions and the editor without touching MIDI devices."""
import json
from pathlib import Path
import tkinter as tk
import unittest

from .app import ActionDialog, RELOAD_KIND, StompilotApp, load_device_profiles
from .test_core import cc_fixture, RELOAD_CURRENT


class ReloadEditorTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.profiles = load_device_profiles()
        self.reload = {"device_id": "tank_mini_v09", "type": "reload_current_preset"}
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.root.update_idletasks()
        self.root.destroy()

    def test_reload_editor_hides_effect_controls_and_emits_minimal_action(self):
        self.dialog = ActionDialog(self.root, self.profiles, self.reload)
        self.root.update()
        self.assertEqual(self.dialog.kind.get(), RELOAD_KIND)
        for label in ("Block", "Effect already selected in the preset", "Parameter"):
            self.assertEqual(self.dialog.boxes[label].winfo_manager(), "")
            self.assertEqual(self.dialog.field_labels[label].winfo_manager(), "")
        self.assertEqual(self.dialog.value_frame.winfo_manager(), "")
        self.assertEqual(self.dialog.channel_box.winfo_manager(), "")
        self.dialog.number.set("invalid")
        self.dialog.channel.set("invalid")
        self.assertEqual(self.dialog.get_action(), self.reload)
        self.assertIn("entire saved preset", self.dialog.hint.get())
        self.assertIn("experimental", self.dialog.preview.get().lower())

    def test_switching_action_and_device_restores_normal_editing(self):
        profiles = {**self.profiles, "generic": cc_fixture()}
        self.dialog = ActionDialog(self.root, profiles, self.reload)
        self.dialog.kind.set("Set parameter")
        self.dialog._kind_changed()
        self.dialog.number.set("17")
        self.assertEqual(self.dialog.get_action()["value"], 17)
        self.assertEqual(self.dialog.get_action()["type"], "parameter")
        self.assertEqual(self.dialog.block_box.winfo_manager(), "grid")
        self.dialog.kind.set(RELOAD_KIND)
        self.dialog._kind_changed()
        self.assertEqual(self.dialog.get_action(), self.reload)
        self.dialog.device.set("Generic CC")
        self.dialog._device_changed()
        self.assertNotIn(RELOAD_KIND, self.dialog.kind_box.cget("values"))
        self.assertEqual(self.dialog.channel_box.winfo_manager(), "grid")
        self.assertEqual(str(self.dialog.channel_box.cget("state")), "normal")
        self.assertEqual(self.dialog.get_action()["type"], "enable")
        self.assertEqual(self.dialog.get_action()["device_id"], "generic")

    def test_reload_project_preview_export_and_combination_guard(self):
        project_path = Path(__file__).resolve().parent.parent / "examples" / "tank_mini_boost_reload.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        app = StompilotApp(self.root, project)
        app.state_index = 1
        app.refresh()
        self.root.update()
        self.assertEqual(app.current_compiled.byte_count, 21)
        self.assertEqual(app.output, RELOAD_CURRENT)
        self.assertEqual(app.actions.item("0", "values")[1:], ("Preset", RELOAD_KIND, "Saved state"))
        self.assertIn("Reload current preset", app.export_text())
        self.assertIn("whole saved preset", app.guidance.get())
        app.selected_state()["actions"].append(project["pages"][0]["switches"]["B"]["states"][0]["actions"][0])
        app.refresh_state()
        self.assertIsNone(app.current_compiled)
        self.assertEqual(app.output, "")
        self.assertEqual(str(app.copy_button.cget("state")), "disabled")


if __name__ == "__main__":
    unittest.main()
