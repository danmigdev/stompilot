"""Shared application zoom, named fonts, and scrollable dialog content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import font as tkfont, ttk

try:
    from .runtime_paths import data_directory
except ImportError:
    from runtime_paths import data_directory


MIN_PERCENT, MAX_PERCENT = 75, 200
FONT_ROLES = {
    "body": ("Segoe UI", 10, "normal"),
    "page": ("Segoe UI", 11, "normal"),
    "switch": ("Segoe UI", 12, "bold"),
    "section": ("Segoe UI", 12, "bold"),
    "title": ("Segoe UI", 21, "bold"),
    "welcome": ("Segoe UI", 14, "bold"),
    "mono": ("Consolas", 10, "normal"),
    "preview": ("Consolas", 11, "normal"),
}
SHORTCUT_TAG = "StompilotZoom"


class ZoomManager:
    def __init__(self, root, settings_path=None, persist=True):
        self.root = root
        self.settings_path = Path(settings_path) if settings_path is not None else data_directory() / "settings.json"
        self.persist = persist
        self._listeners, self._next_listener = {}, 0
        root._stompilot_zoom = self
        self.percent = tk.IntVar(root, value=self._read_percent() if persist else 100)
        self.percent_label = tk.StringVar(root, value=f"{self.percent.get()}%")
        self._fonts = {
            role: tkfont.Font(root=root, name=f"Stompilot{role.title()}", family=family,
                             size=size, weight=weight)
            for role, (family, size, weight) in FONT_ROLES.items()
        }
        self._defaults = {}
        for name in tkfont.names(root):
            if name.startswith("Tk"):
                item = tkfont.nametofont(name, root=root)
                self._defaults[name] = (item, item.cget("size"))
        root.option_add("*Font", str(self.font("body")))
        root.option_add("*TCombobox*Listbox.font", str(self.font("body")))
        self._apply_fonts()
        self._install_shortcuts()

    def _read_percent(self):
        try:
            content = json.loads(self.settings_path.read_text(encoding="utf-8"))
            value = content.get("ui_zoom_percent", 100)
            if isinstance(value, int) and not isinstance(value, bool) and MIN_PERCENT <= value <= MAX_PERCENT:
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return 100

    def _save_percent(self):
        if not self.persist:
            return
        temporary = None
        try:
            try:
                content = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                content = {}
            if not isinstance(content, dict):
                content = {}
            content["ui_zoom_percent"] = self.percent.get()
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings_path.parent,
                                             prefix=".stompilot-settings-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(content, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings_path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def font(self, role="body"):
        return self._fonts[role]

    def px(self, value):
        return int(float(value) * self.percent.get() / 100 + 0.5)

    def _apply_fonts(self):
        for role, (_, size, _) in FONT_ROLES.items():
            self._fonts[role].configure(size=max(1, self.px(size)))
        for item, original_size in self._defaults.values():
            sign = -1 if original_size < 0 else 1
            item.configure(size=sign * max(1, self.px(abs(original_size))))
        style = ttk.Style(self.root)
        style.configure(".", font=self.font("body"))
        style.configure("Title.TLabel", font=self.font("title"))
        style.configure("Section.TLabel", font=self.font("section"))
        style.configure("Treeview", font=self.font("body"), rowheight=self.px(35))
        style.configure("Treeview.Heading", font=self.font("body"), padding=self.px(8))
        style.configure("TButton", padding=(self.px(12), self.px(8)))
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(name, padding=self.px(7))
        style.configure("TSpinbox", arrowsize=self.px(16))
        style.configure("TNotebook.Tab", padding=(self.px(18), self.px(8)))

    def set_percent(self, value):
        value = max(MIN_PERCENT, min(MAX_PERCENT, int(value)))
        if value == self.percent.get():
            return
        self.percent.set(value)
        self.percent_label.set(f"{value}%")
        self._apply_fonts()
        for token, (widget, callback) in list(self._listeners.items()):
            if widget.winfo_exists():
                callback(value)
            else:
                self._listeners.pop(token, None)
        self._save_percent()

    def refresh_styles(self):
        """Reapply zoom metrics after an application theme has been configured."""
        self._apply_fonts()

    def step(self, delta=25):
        self.set_percent(self.percent.get() + delta)

    def reset(self):
        self.set_percent(100)

    def listen(self, widget, callback):
        self._next_listener += 1
        token = self._next_listener
        self._listeners[token] = (widget, callback)
        def unsubscribe():
            self._listeners.pop(token, None)
        widget.bind("<Destroy>", lambda event: unsubscribe() if event.widget is widget else None, add="+")
        callback(self.percent.get())
        return unsubscribe

    def add_listener(self, callback, widget=None):
        return self.listen(widget or self.root, callback)

    def toolbar(self, parent):
        frame = ttk.Frame(parent)
        ttk.Label(frame, text="Zoom", font=self.font("body")).pack(side="left", padx=(0, 8))
        ttk.Button(frame, text="−", width=3, command=lambda: self.step(-25)).pack(side="left")
        ttk.Label(frame, textvariable=self.percent_label, font=self.font("body"), width=5,
                  anchor="center").pack(side="left", padx=5)
        ttk.Button(frame, text="+", width=3, command=lambda: self.step(25)).pack(side="left")
        ttk.Button(frame, text="Reset", command=self.reset).pack(side="left", padx=(8, 0))
        return frame

    def fit_window(self, window, width, height):
        width = min(self.px(width), max(320, window.winfo_screenwidth() - 60))
        height = min(self.px(height), max(240, window.winfo_screenheight() - 100))
        window.minsize(min(width, self.px(360)), min(height, self.px(240)))
        window.geometry(f"{width}x{height}")

    def _key_zoom(self, event):
        if event.keysym in ("plus", "equal", "KP_Add"):
            self.step(25)
        elif event.keysym in ("minus", "KP_Subtract"):
            self.step(-25)
        elif event.keysym in ("0", "KP_0"):
            self.reset()
        else:
            return None
        return "break"

    def _wheel_zoom(self, event):
        if event.delta:
            self.step(25 if event.delta > 0 else -25)
        return "break"

    def _tag_widget(self, widget):
        path = str(widget)
        try:
            tags = self.root.tk.call("bindtags", path)
            if SHORTCUT_TAG not in tags:
                self.root.tk.call("bindtags", path, (SHORTCUT_TAG, *tags))
            if ".popdown." in path and self.root.tk.call("winfo", "class", path) == "Listbox":
                self.root.tk.call(path, "configure", "-font", str(self.font("body")))
        except tk.TclError:
            pass

    def _install_shortcuts(self):
        self.root.bind_class(SHORTCUT_TAG, "<Control-KeyPress>", self._key_zoom)
        self.root.bind_class(SHORTCUT_TAG, "<Control-MouseWheel>", self._wheel_zoom)
        self.root.bind_all("<Control-KeyPress>", self._key_zoom, add="+")
        self.root.bind_all("<Control-MouseWheel>", self._wheel_zoom, add="+")
        self.root.bind_all("<Map>", lambda event: self._tag_widget(event.widget), add="+")
        self.root.bind_all("<MouseWheel>", self._scroll_wheel, add="+")
        def visit(widget):
            self._tag_widget(widget)
            for child in widget.winfo_children():
                visit(child)
        visit(self.root)

    def _scroll_wheel(self, event):
        if event.state & 4:
            return None
        widget = event.widget
        while isinstance(widget, tk.Misc):
            if isinstance(widget, ScrollableFrame):
                units = -3 if event.delta > 0 else 3
                if widget.horizontal and event.state & 1:
                    widget.canvas.xview_scroll(units, "units")
                else:
                    widget.canvas.yview_scroll(units, "units")
                return "break"
            widget = widget.master
        return None


def get_zoom(widget):
    root = widget._root()
    manager = getattr(root, "_stompilot_zoom", None)
    return manager if manager is not None else ZoomManager(root)


class ScrollableFrame(ttk.Frame):
    """A content frame that stays reachable when fonts exceed the viewport."""
    def __init__(self, parent, horizontal=False, **options):
        super().__init__(parent, **options)
        self.horizontal = horizontal
        self.zoom = get_zoom(self)
        background = ttk.Style(self).lookup("TFrame", "background") or "#111721"
        self.canvas = tk.Canvas(self, background=background, borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical_bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.vertical_bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.vertical_bar.set)
        self.horizontal_bar = None
        if horizontal:
            self.horizontal_bar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
            self.horizontal_bar.grid(row=1, column=0, sticky="ew")
            self.canvas.configure(xscrollcommand=self.horizontal_bar.set)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.content = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.bind("<Configure>", self._size_changed)
        self.content.bind("<Configure>", self._size_changed)
        self.zoom.listen(self, self._zoom_changed)

    def _zoom_changed(self, _percent):
        self.canvas.configure(yscrollincrement=max(1, self.zoom.px(20)),
                              xscrollincrement=max(1, self.zoom.px(20)))
        self._size_changed()

    def _size_changed(self, _event=None):
        width = self.canvas.winfo_width()
        if self.horizontal:
            width = max(width, self.content.winfo_reqwidth())
        height = max(self.canvas.winfo_height(), self.content.winfo_reqheight())
        self.canvas.itemconfigure(self.window, width=width, height=height)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
