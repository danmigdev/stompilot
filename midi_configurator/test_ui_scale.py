"""Global zoom and persistence checks with isolated settings and no MIDI I/O."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest
from unittest.mock import patch

from . import ui_dialogs
from .ui_scale import ScrollableFrame, ZoomManager, get_zoom


class ZoomTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.temp = TemporaryDirectory()
        self.settings = Path(self.temp.name) / "settings.json"

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def manager(self, persist=False):
        return ZoomManager(self.root, settings_path=self.settings, persist=persist)

    def test_all_windows_share_live_named_fonts_and_default_font_scaling(self):
        default_size = tkfont.nametofont("TkDefaultFont", root=self.root).cget("size")
        zoom = self.manager()
        modal = tk.Toplevel(self.root)
        self.assertIs(get_zoom(modal), zoom)
        label = ttk.Label(modal, text="Effect parameter", font=zoom.font("body"))
        original_font = label.cget("font")
        zoom.set_percent(150)
        self.assertEqual(label.cget("font"), original_font)
        self.assertEqual(zoom.font("body").cget("size"), 15)
        self.assertEqual(zoom.font("title").cget("size"), 32)
        self.assertEqual(zoom.font("preview").cget("size"), 17)
        self.assertEqual(abs(tkfont.nametofont("TkDefaultFont", root=self.root).cget("size")),
                         zoom.px(abs(default_size)))
        self.assertEqual(int(ttk.Style(self.root).lookup("Treeview", "rowheight")), 53)

    def test_zoom_bounds_steps_reset_and_label(self):
        zoom = self.manager()
        zoom.set_percent(1)
        self.assertEqual(zoom.percent.get(), 75)
        zoom.step(25)
        self.assertEqual(zoom.percent_label.get(), "100%")
        zoom.set_percent(999)
        zoom.step(25)
        self.assertEqual(zoom.percent.get(), 200)
        zoom.reset()
        self.assertEqual(zoom.percent.get(), 100)

    def test_theme_reconfiguration_can_restore_zoomed_style_metrics(self):
        zoom = self.manager()
        zoom.set_percent(150)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", rowheight=35)
        style.configure("TButton", padding=(12, 8))
        zoom.refresh_styles()
        self.assertEqual(int(style.lookup("Treeview", "rowheight")), 53)
        self.assertEqual(tuple(int(item) for item in self.root.tk.splitlist(style.lookup("TButton", "padding"))),
                         (18, 12))

    def test_destroyed_window_stops_receiving_zoom_callbacks(self):
        zoom = self.manager()
        modal = tk.Toplevel(self.root)
        calls = []
        zoom.listen(modal, calls.append)
        zoom.set_percent(125)
        modal.destroy()
        zoom.set_percent(150)
        self.assertEqual(calls, [100, 125])

    def test_ctrl_wheel_scales_a_modal_without_changing_spinbox_value(self):
        zoom = self.manager()
        self.root.deiconify()
        modal = tk.Toplevel(self.root)
        value = tk.StringVar(modal, "20")
        spin = ttk.Spinbox(modal, from_=0, to=100, textvariable=value)
        spin.pack()
        self.root.update()
        spin.event_generate("<Control-MouseWheel>", delta=120)
        self.assertEqual(zoom.percent.get(), 125)
        self.assertEqual(value.get(), "20")
        spin.focus_force()
        self.root.update()
        spin.event_generate("<Control-minus>")
        self.assertEqual(zoom.percent.get(), 100)
        spin.event_generate("<Control-plus>")
        spin.event_generate("<Control-0>")
        self.assertEqual(zoom.percent.get(), 100)
        self.assertEqual(value.get(), "20")

    def test_combobox_dropdown_uses_a_live_scaled_font(self):
        zoom = self.manager()
        combo = ttk.Combobox(self.root, values=["Hall", "Dual"])
        popdown = self.root.tk.call("ttk::combobox::PopdownWindow", str(combo))
        listbox = str(popdown) + ".f.l"
        font_name = self.root.tk.call(listbox, "cget", "-font")
        zoom.set_percent(200)
        dropdown_font = tkfont.Font(root=self.root, font=font_name)
        self.assertEqual(dropdown_font.cget("size"), 20)

    def test_saved_zoom_is_loaded_and_other_settings_survive_atomic_updates(self):
        self.settings.write_text(json.dumps({"ui_zoom_percent": 150, "other_preference": "keep"}))
        zoom = self.manager(persist=True)
        self.assertEqual(zoom.percent.get(), 150)
        zoom.set_percent(175)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"ui_zoom_percent": 175, "other_preference": "keep"})
        self.assertEqual(list(self.settings.parent.glob(".stompilot-settings-*.tmp")), [])

    def test_invalid_settings_fall_back_to_one_hundred_percent(self):
        cases = ["{", "[]", '{"ui_zoom_percent": true}', '{"ui_zoom_percent": 201}',
                 '{"ui_zoom_percent": 74}', '{"ui_zoom_percent": "150"}']
        for content in cases:
            with self.subTest(content=content):
                self.settings.write_text(content)
                root = tk.Tk()
                root.withdraw()
                try:
                    zoom = ZoomManager(root, settings_path=self.settings)
                    self.assertEqual(zoom.percent.get(), 100)
                finally:
                    root.update_idletasks()
                    root.destroy()

    def test_disabled_persistence_ignores_existing_preferences(self):
        original = '{"ui_zoom_percent": 175}'
        self.settings.write_text(original)
        zoom = self.manager(persist=False)
        self.assertEqual(zoom.percent.get(), 100)
        zoom.set_percent(200)
        self.assertEqual(self.settings.read_text(), original)

    def test_storage_failure_does_not_revert_zoom_or_corrupt_settings(self):
        original = '{"ui_zoom_percent": 100, "other": 7}'
        self.settings.write_text(original)
        zoom = self.manager(persist=True)
        with patch("midi_configurator.ui_scale.os.replace", side_effect=OSError("Access denied")):
            zoom.set_percent(150)
        self.assertEqual(zoom.percent.get(), 150)
        self.assertEqual(zoom.font("body").cget("size"), 15)
        self.assertEqual(self.settings.read_text(), original)
        self.assertEqual(list(self.settings.parent.glob(".stompilot-settings-*.tmp")), [])

    def test_large_fonts_remain_reachable_in_a_small_scrolling_viewport(self):
        zoom = self.manager()
        zoom.set_percent(200)
        self.root.geometry("420x280")
        self.root.deiconify()
        frame = ScrollableFrame(self.root, horizontal=True)
        frame.pack(fill="both", expand=True)
        for _ in range(20):
            ttk.Label(frame.content, text="Effect settings " * 12, font=zoom.font("body")).pack(anchor="w")
        self.root.update()
        self.assertLess(frame.canvas.xview()[1], 1)
        self.assertLess(frame.canvas.yview()[1], 1)
        frame.canvas.xview_moveto(1)
        frame.canvas.yview_moveto(1)
        self.assertAlmostEqual(frame.canvas.xview()[1], 1)
        self.assertAlmostEqual(frame.canvas.yview()[1], 1)

    def test_prompt_shares_zoom_and_restores_the_parent_modal_grab(self):
        zoom = self.manager()
        parent = tk.Toplevel(self.root)
        parent.grab_set()
        self.root.update()
        observed = []
        def answer():
            dialog = next(child for child in parent.winfo_children() if isinstance(child, ui_dialogs._Dialog))
            observed.append(get_zoom(dialog) is zoom)
            zoom.set_percent(150)
            observed.append(str(dialog.message.cget("font")) == str(zoom.font("body")))
            dialog.buttons[0].invoke()
        self.root.after(80, answer)
        self.assertIs(ui_dialogs.askyesno("Discard changes?", "Keep this local edit?", parent=parent), True)
        self.assertEqual(observed, [True, True])
        self.assertIs(self.root.grab_current(), parent)

    def test_zoomable_text_prompt_returns_the_edited_value(self):
        self.manager()
        def answer():
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, ui_dialogs._Dialog))
            dialog.entry.delete(0, "end")
            dialog.entry.insert(0, "Solo page")
            dialog.buttons[0].invoke()
        self.root.after(80, answer)
        self.assertEqual(ui_dialogs.askstring("Page name", "Choose a page name:", initialvalue="Page 1",
                                             parent=self.root), "Solo page")


if __name__ == "__main__":
    unittest.main()
