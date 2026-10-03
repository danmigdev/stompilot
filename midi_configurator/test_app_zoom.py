"""Verify application-wide zoom without changing projects or MIDI messages."""
import json
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest
from unittest.mock import patch

from .app import ActionDialog, RawMidiDialog, StompilotApp
from .project import new_project
from .ui_scale import ScrollableFrame, ZoomManager


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ApplicationZoomTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.zoom = ZoomManager(self.root, persist=False)
        self.app = StompilotApp(self.root, new_project())
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            if window.winfo_exists():
                window.destroy()
        self.root.update_idletasks()
        self.root.destroy()

    def test_zoom_updates_editor_fonts_and_preserves_project_and_commands(self):
        before = json.dumps(self.app.project, sort_keys=True)
        source, output = self.app.source_text.get(), self.app.output
        export = self.app.export_text()
        for percent in (150, 200, 75, 100):
            self.zoom.set_percent(percent)
            self.root.update_idletasks()
            self.assertEqual(self.zoom.font("title").cget("size"), self.zoom.px(21))
            self.assertEqual(self.zoom.font("preview").cget("size"), self.zoom.px(11))
            self.assertEqual(str(self.app.page_list.cget("font")), str(self.zoom.font("page")))
            self.assertEqual(str(self.app.hex_text.cget("font")), str(self.zoom.font("preview")))
            self.assertEqual(int(ttk.Style(self.root).lookup("Treeview", "rowheight")), self.zoom.px(35))
            self.assertEqual(self.app.source_text.get(), source)
            self.assertEqual(self.app.output, output)
            self.assertEqual(self.app.export_text(), export)
            self.assertEqual(json.dumps(self.app.project, sort_keys=True), before)
            self.assertFalse(self.app.dirty)
            self.assertIsNone(self.app.path)

    def test_open_and_new_dialogs_share_zoom_and_leave_action_values_unchanged(self):
        action = self.app.project["pages"][0]["switches"]["B"]["states"][0]["actions"][1]
        dialog = ActionDialog(self.root, self.app.profiles, action)
        self.windows.append(dialog)
        original = dialog.get_action()
        self.zoom.set_percent(200)
        self.root.update_idletasks()
        self.assertIs(dialog.zoom, self.zoom)
        self.assertEqual(dialog.get_action(), original)
        self.assertEqual(int(dialog.hint_label.cget("wraplength")), self.zoom.px(440))
        dialog.destroy()

        raw = RawMidiDialog(self.root, self.app.profiles,
                            {"type": "raw_midi", "message_hex": "F0 7D 01 F7"})
        self.windows.append(raw)
        self.assertIs(raw.zoom, self.zoom)
        self.assertEqual(str(raw.hex_text.cget("font")), str(self.zoom.font("mono")))
        self.assertEqual(tkfont.nametofont(str(raw.hex_text.cget("font")), root=self.root).cget("size"), 20)
        self.zoom.set_percent(75)
        self.root.update_idletasks()
        self.assertEqual(raw.get_action()["message_hex"], "F0 7D 01 F7")
        self.assertEqual(self.zoom.font("mono").cget("size"), 8)

    def test_profiles_and_startup_use_shared_fonts_at_current_zoom(self):
        self.zoom.set_percent(150)
        before = set(self.root.winfo_children())
        self.app.manage_profiles()
        window = next(child for child in self.root.winfo_children()
                      if child not in before and isinstance(child, tk.Toplevel))
        self.windows.append(window)
        texts = [child for child in descendants(window) if isinstance(child, tk.Text)]
        self.assertTrue(texts)
        self.assertTrue(all(str(child.cget("font")) == str(self.zoom.font("body")) for child in texts))
        self.zoom.set_percent(200)
        self.root.deiconify()
        window.geometry("740x500")
        self.root.update()
        scroll = next(child for child in descendants(window) if isinstance(child, ScrollableFrame))
        self.assertLess(scroll.canvas.xview()[1], 1)
        scroll.canvas.xview_moveto(1)
        self.assertAlmostEqual(scroll.canvas.xview()[1], 1)
        window.destroy()
        self.app.show_startup()
        titles = [child for child in descendants(self.app.startup_frame)
                  if isinstance(child, ttk.Label) and child.cget("text") == "Stompilot"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(str(titles[0].cget("font")), str(self.zoom.font("title")))
        self.zoom.reset()
        self.assertEqual(tkfont.nametofont(str(titles[0].cget("font")), root=self.root).cget("size"), 21)

    def test_app_theme_preserves_zoom_selected_before_construction(self):
        self.app.destroy()
        self.zoom.set_percent(175)
        self.app = StompilotApp(self.root, new_project())
        self.root.update_idletasks()
        self.assertEqual(self.zoom.font("body").cget("size"), 18)
        self.assertEqual(int(ttk.Style(self.root).lookup("TSpinbox", "arrowsize")), self.zoom.px(16))
        self.assertEqual(int(ttk.Style(self.root).lookup("Treeview", "rowheight")), self.zoom.px(35))

    def test_initial_window_and_scrollbars_fit_on_a_small_screen(self):
        with patch.object(self.root, "winfo_screenwidth", return_value=1024), \
                patch.object(self.root, "winfo_screenheight", return_value=768):
            self.app._theme()
        self.root.deiconify()
        self.root.update()
        self.assertLessEqual(self.root.winfo_width(), 964)
        self.assertLessEqual(self.root.winfo_height(), 668)
        self.assertEqual(self.root.minsize(), (964, 668))
        self.zoom.set_percent(200)
        self.root.update()
        for bar in (self.app.scroll_view.vertical_bar, self.app.scroll_view.horizontal_bar):
            self.assertTrue(bar.winfo_viewable())
            self.assertLessEqual(bar.winfo_rootx() + bar.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
            self.assertLessEqual(bar.winfo_rooty() + bar.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())


if __name__ == "__main__":
    unittest.main()
