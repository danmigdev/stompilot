"""Stompilot desktop application with offline editing and direct USB programming."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tkinter as tk
from types import SimpleNamespace
from tkinter import ttk, filedialog

if __package__:
    from .core import (ValidationError, load_profiles, validate_profile, compile_action,
                       compile_state, validate_config, load_config, save_config, sysex_limit)
    from .project import new_project, empty_page, normalize_project
    from .runtime_paths import resource_dir, profile_directory
    from .ui_scale import ZoomManager, ScrollableFrame, get_zoom
    from . import ui_dialogs as messagebox
else:
    from core import (ValidationError, load_profiles, validate_profile, compile_action,
                      compile_state, validate_config, load_config, save_config, sysex_limit)
    from project import new_project, empty_page, normalize_project
    from runtime_paths import resource_dir, profile_directory
    from ui_scale import ZoomManager, ScrollableFrame, get_zoom
    import ui_dialogs as messagebox

BUNDLED_PROFILE_DIR = resource_dir() / "profiles"
PROFILE_DIR = profile_directory()
BG, PANEL, RAISED = "#111721", "#1b2432", "#253247"
FG, MUTED, ACCENT, AMBER = "#edf4ff", "#aab9ce", "#49dcc8", "#ffd18b"
MODES = {"One sequence": "single", "Alternate two states": "alternating"}
MAX_UI_PAGES = 8
RELOAD_KIND = "Reload current preset"


def load_device_profiles():
    """Load bundled devices and persistent custom profiles without replacing defaults."""
    profiles = load_profiles(BUNDLED_PROFILE_DIR)
    if PROFILE_DIR != BUNDLED_PROFILE_DIR and PROFILE_DIR.is_dir() and any(PROFILE_DIR.glob("*.json")):
        custom = load_profiles(PROFILE_DIR)
        for profile_id, profile in custom.items():
            if profile_id in profiles:
                raise ValidationError(f"Duplicate profile ID: {profile_id!r}")
            profiles[profile_id] = profile
    names = set()
    for profile in profiles.values():
        if profile["name"] in names:
            raise ValidationError(f"Duplicate profile name: {profile['name']!r}")
        names.add(profile["name"])
    return profiles


def description(action, profiles):
    if action["type"] == "raw_midi":
        message = bytes.fromhex(action["message_hex"])
        if message[0] == 0xF0:
            return "Imported MIDI", "SysEx", "Original MIDI message", f"{len(message)} bytes"
        return "Imported MIDI", f"Ch {1 + (message[0] & 15)}", f"CC {message[1]}", str(message[2])
    profile = profiles[action["device_id"]]
    if action["type"] == "reload_current_preset":
        return profile["name"], "Preset", RELOAD_KIND, "Saved state"
    block = next(b for b in profile["blocks"] if b["id"] == action["block_id"])
    model = next((m for m in block.get("models", []) if m["id"] == action.get("model_id")), {})
    if action["type"] == "enable":
        target, value = model.get("label", block["label"]), "ON" if action["value"] else "OFF"
    elif action["type"] == "parameter":
        param = next(p for p in model["parameters"] if p["id"] == action["param_id"])
        target, value = f"{model['label']} · {param['label']}", str(action["value"])
    else:
        target, value = "Model", model.get("label", "")
    return profile["name"], block["label"], target, value


def fit_action_window(window, parent, content, zoom):
    """Keep the editor footer visible while larger text scrolls inside the window."""
    window.update_idletasks()
    width = min(max(zoom.px(480), content.winfo_reqwidth() + 16), window.winfo_screenwidth() - 60)
    height = min(content.winfo_reqheight() + zoom.px(115), window.winfo_screenheight() - 100)
    x = max(20, min(parent.winfo_rootx() + 180, window.winfo_screenwidth() - width - 20))
    y = max(20, min(parent.winfo_rooty() + 65, window.winfo_screenheight() - height - 70))
    window.geometry(f"{width}x{height}+{x}+{y}")


class ActionDialog(tk.Toplevel):
    def __init__(self, parent, profiles, action=None):
        super().__init__(parent)
        self.zoom = get_zoom(self)
        self.profiles, self.result, self.original = profiles, None, action or {}
        self.title("Edit action" if action else "Add action")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.zoom.toolbar(self).pack(fill="x", padx=24, pady=(10, 0))
        self.scroll = ScrollableFrame(self, horizontal=True)
        self.scroll.pack(fill="both", expand=True)
        panel = ttk.Frame(self.scroll.content, padding=24)
        panel.pack(fill="both", expand=True)
        self.device = tk.StringVar()
        self.block = tk.StringVar()
        self.model = tk.StringVar()
        self.kind = tk.StringVar(value="Turn on / off")
        self.param = tk.StringVar()
        self.enabled = tk.StringVar(value="ON")
        self.number = tk.StringVar(value="0")
        self.channel = tk.StringVar(value=str(self.original.get("channel", 1)))
        self.preview = tk.StringVar()
        self.hint = tk.StringVar()
        fields = [("Device", self.device), ("Action", self.kind), ("Block", self.block),
                  ("Effect already selected in the preset", self.model),
                  ("Parameter", self.param)]
        self.boxes = {}
        self.field_labels = {}
        for row, (label, var) in enumerate(fields):
            field_label = ttk.Label(panel, text=label)
            field_label.grid(row=row * 2, column=0, sticky="w", pady=(0, 4))
            self.field_labels[label] = field_label
            box = ttk.Combobox(panel, textvariable=var, state="readonly", width=48)
            box.grid(row=row * 2 + 1, column=0, columnspan=3, sticky="ew", pady=(0, 12))
            self.boxes[label] = box
        self.value_frame = ttk.Frame(panel)
        self.value_frame.grid(row=10, column=0, columnspan=3, sticky="ew")
        self.on_box = ttk.Combobox(self.value_frame, textvariable=self.enabled,
                                   values=("ON", "OFF"), state="readonly", width=10)
        self.slider = ttk.Scale(self.value_frame, from_=0, to=100, command=self._slide)
        self.spin = ttk.Spinbox(self.value_frame, textvariable=self.number, from_=0, to=100, width=7)
        self.hint_label = ttk.Label(panel, textvariable=self.hint, style="Muted.TLabel", wraplength=440)
        self.hint_label.grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 14))
        self.channel_label = ttk.Label(panel, text="MIDI channel")
        self.channel_label.grid(row=12, column=0, sticky="w")
        self.channel_box = ttk.Spinbox(panel, textvariable=self.channel, from_=1, to=16, width=7)
        self.channel_box.grid(row=12, column=1, sticky="w")
        self.preview_label = ttk.Label(panel, textvariable=self.preview, style="Muted.TLabel", wraplength=440)
        self.preview_label.grid(
            row=13, column=0, columnspan=3, sticky="w", pady=16)
        buttons = ttk.Frame(self, padding=(24, 0, 24, 12))
        buttons.pack(fill="x")
        buttons.columnconfigure(0, weight=1)
        buttons_inner = ttk.Frame(buttons)
        buttons_inner.pack(side="right")
        ttk.Button(buttons_inner, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        ttk.Button(buttons_inner, text="Save action", style="Accent.TButton", command=self.accept).pack(side="left")
        self.device_box = self.boxes["Device"]
        self.block_box = self.boxes["Block"]
        self.model_box = self.boxes["Effect already selected in the preset"]
        self.kind_box = self.boxes["Action"]
        self.param_box = self.boxes["Parameter"]
        self.device_box.configure(values=[p["name"] for p in profiles.values()])
        self.device_box.bind("<<ComboboxSelected>>", lambda e: self._device_changed())
        self.block_box.bind("<<ComboboxSelected>>", lambda e: self._block_changed())
        self.model_box.bind("<<ComboboxSelected>>", lambda e: self._model_changed())
        self.kind_box.bind("<<ComboboxSelected>>", lambda e: self._kind_changed())
        self.param_box.bind("<<ComboboxSelected>>", lambda e: self._parameter_changed())
        for var in (self.enabled, self.number, self.channel):
            var.trace_add("write", lambda *args: self._update_preview())
        first_id = self.original.get("device_id", "tank_mini_v09" if "tank_mini_v09" in profiles else next(iter(profiles)))
        self.device.set(profiles[first_id]["name"])
        self._device_changed(initial=True)
        self.bind("<Escape>", lambda e: self.destroy())
        def zoom_layout(percent):
            self.hint_label.configure(wraplength=self.zoom.px(440))
            self.preview_label.configure(wraplength=self.zoom.px(440))
            fit_action_window(self, parent, panel, self.zoom)
        self.zoom.listen(self, zoom_layout)

    def selected_profile(self):
        return next(p for p in self.profiles.values() if p["name"] == self.device.get())

    def selected_block(self):
        return next(b for b in self.selected_profile()["blocks"] if b["label"] == self.block.get())

    def selected_model(self):
        return next(m for m in self.selected_block().get("models", []) if m["label"] == self.model.get())

    def selected_param(self):
        return next(p for p in self.selected_model().get("parameters", []) if p["label"] == self.param.get())

    @staticmethod
    def pick(items, requested_id):
        return next((item for item in items if item["id"] == requested_id), items[0] if items else {})

    def _device_changed(self, initial=False):
        blocks = self.selected_profile()["blocks"]
        self.block_box.configure(values=[b["label"] for b in blocks])
        self.block.set(self.pick(blocks, self.original.get("block_id") if initial else None)["label"])
        self._block_changed(initial)

    def _block_changed(self, initial=False):
        profile, block = self.selected_profile(), self.selected_block()
        models = block.get("models", [])
        self.model_box.configure(values=[m["label"] for m in models])
        self.model.set(self.pick(models, self.original.get("model_id") if initial else None).get("label", ""))
        self._model_changed(initial)
        self.channel_box.configure(state="disabled" if profile["protocol"] == "tank_v09" else "normal")

    def _model_changed(self, initial=False):
        model = self.selected_model() if self.model.get() else {}
        params = model.get("parameters", [])
        self.param_box.configure(values=[p["label"] for p in params])
        self.param.set(self.pick(params, self.original.get("param_id") if initial else None).get("label", ""))
        block, profile = self.selected_block(), self.selected_profile()
        kinds = []
        if profile["protocol"] == "tank_v09" or "enable" in block:
            kinds.append("Turn on / off")
        if params:
            kinds.append("Set parameter")
        if any(item["id"] == "reload_current_preset" for item in profile.get("device_actions", [])):
            kinds.append(RELOAD_KIND)
        self.kind_box.configure(values=kinds)
        requested = {"parameter": "Set parameter", "reload_current_preset": RELOAD_KIND}.get(
            self.original.get("type"), self.kind.get()) if initial else self.kind.get()
        self.kind.set(requested if requested in kinds else kinds[0] if kinds else "")
        if initial and self.original.get("type") == "enable":
            self.enabled.set("ON" if self.original.get("value") else "OFF")
        self._kind_changed(initial)

    def _kind_changed(self, initial=False):
        for widget in (self.on_box, self.slider, self.spin):
            widget.pack_forget()
        is_reload = self.kind.get() == RELOAD_KIND
        for label in ("Block", "Effect already selected in the preset", "Parameter"):
            for widget in (self.field_labels[label], self.boxes[label]):
                widget.grid_remove() if is_reload else widget.grid()
        for widget in (self.value_frame, self.channel_label, self.channel_box):
            widget.grid_remove() if is_reload else widget.grid()
        is_param = self.kind.get() == "Set parameter"
        self.param_box.configure(state="readonly" if is_param else "disabled")
        if is_reload:
            self.hint.set("Experimental: restores the entire saved preset, including other effects, "
                          "and discards temporary edits. Stock V09 only; not yet tested on hardware. "
                          "Use this action alone in its state, in normal playing mode.")
        elif is_param:
            self.slider.pack(side="left", fill="x", expand=True, padx=(0, 16))
            self.spin.pack(side="left")
            self._parameter_changed(initial)
        else:
            self.on_box.pack(side="left")
            self.hint.set("The state applies to the block in the current preset. Select the effect on the device.")
        self._update_preview()

    def _parameter_changed(self, initial=False):
        if not self.param.get():
            return
        parameter = self.selected_param()
        low, high = parameter.get("min", 0), parameter.get("max", 100)
        self.slider.configure(from_=low, to=high)
        self.spin.configure(from_=low, to=high, increment=parameter.get("step", 1))
        value = self.original.get("value", parameter.get("default", low)) if initial else parameter.get("default", low)
        self.number.set(str(value))
        self.slider.set(value)
        unit = "control value" if self.selected_profile()["protocol"] == "tank_v09" else "MIDI value"
        self.hint.set(f"{parameter['label']}: {low}–{high} ({unit}). The selected effect must already be in the preset.")
        self._update_preview()

    def _slide(self, raw):
        if not self.param.get():
            return
        parameter = self.selected_param()
        low, high, step = parameter.get("min", 0), parameter.get("max", 100), parameter.get("step", 1)
        value = max(low, min(high, low + round((float(raw) - low) / step) * step))
        if self.number.get() != str(value):
            self.number.set(str(value))

    def get_action(self):
        profile = self.selected_profile()
        if self.kind.get() == RELOAD_KIND:
            action = {"device_id": profile["id"], "type": "reload_current_preset"}
            compile_action(action, self.profiles)
            return action
        block = self.selected_block()
        action = {"device_id": profile["id"], "block_id": block["id"], "channel": int(self.channel.get())}
        if self.model.get():
            action["model_id"] = self.selected_model()["id"]
        if self.kind.get() == "Set parameter":
            action.update(type="parameter", param_id=self.selected_param()["id"], value=int(self.number.get()))
        elif self.kind.get() == "Turn on / off":
            action.update(type="enable", value=self.enabled.get() == "ON")
        else:
            raise ValidationError("This block has no configurable actions.")
        compile_action(action, self.profiles)
        return action

    def _update_preview(self):
        try:
            action = self.get_action()
            compiled = compile_action(action, self.profiles)
            self.preview.set(compiled.summary)
            if action["type"] == "parameter":
                self.slider.set(action["value"])
        except (ValueError, StopIteration, tk.TclError):
            self.preview.set("Choose an action and a valid value.")

    def accept(self):
        try:
            self.result = self.get_action()
            self.destroy()
        except (ValueError, StopIteration) as exc:
            messagebox.showerror("Invalid action", str(exc), parent=self)


class RawMidiDialog(tk.Toplevel):
    """Edit an imported command without assigning an unproven device or model."""
    def __init__(self, parent, profiles, action):
        super().__init__(parent)
        self.zoom = get_zoom(self)
        self.profiles, self.result = profiles, None
        self.title("Edit imported MIDI")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        message = bytes.fromhex(action["message_hex"])
        self.sysex = message[0] == 0xF0
        self.channel = tk.StringVar(value=str((message[0] & 15) + 1))
        self.cc = tk.StringVar(value=str(message[1]))
        self.value = tk.StringVar(value=str(message[2] if len(message) > 2 else 0))
        self.preview = tk.StringVar()
        self.zoom.toolbar(self).pack(fill="x", padx=24, pady=(10, 0))
        self.scroll = ScrollableFrame(self, horizontal=True)
        self.scroll.pack(fill="both", expand=True)
        panel = ttk.Frame(self.scroll.content, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Imported controller command", style="Section.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Chocolate stores MIDI commands, not the current effect settings on the target device.",
                  style="Muted.TLabel", wraplength=500).pack(anchor="w", pady=(8, 18))
        if self.sysex:
            ttk.Label(panel, text="SysEx message (hex)").pack(anchor="w", pady=(0, 6))
            self.hex_text = tk.Text(panel, width=56, height=6, bg=PANEL, fg=ACCENT, insertbackground=FG,
                                    font=self.zoom.font("mono"), wrap="word", padx=10, pady=10, relief="flat")
            self.hex_text.pack(fill="x")
            self.hex_text.insert("1.0", action["message_hex"])
            self.hex_text.edit_modified(False)
            self.hex_text.bind("<<Modified>>", self._changed)
        else:
            for label, variable, low, high in (("MIDI channel", self.channel, 1, 16),
                                              ("CC number", self.cc, 0, 127), ("Value", self.value, 0, 127)):
                row = ttk.Frame(panel)
                row.pack(fill="x", pady=5)
                ttk.Label(row, text=label, width=20).pack(side="left")
                ttk.Spinbox(row, textvariable=variable, from_=low, to=high, width=8).pack(side="left")
                variable.trace_add("write", lambda *args: self._update_preview())
        ttk.Label(panel, textvariable=self.preview, style="Muted.TLabel", wraplength=500).pack(anchor="w", pady=18)
        buttons = ttk.Frame(self, padding=(24, 0, 24, 12))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save action", style="Accent.TButton", command=self.accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Escape>", lambda e: self.destroy())
        self._update_preview()
        self.zoom.listen(self, lambda percent: fit_action_window(self, parent, panel, self.zoom))

    def _changed(self, event=None):
        if self.hex_text.edit_modified():
            self.hex_text.edit_modified(False)
            self._update_preview()

    def get_action(self):
        if self.sysex:
            raw = self.hex_text.get("1.0", "end").strip()
        else:
            channel, cc, value = int(self.channel.get()), int(self.cc.get()), int(self.value.get())
            if not 1 <= channel <= 16 or not 0 <= cc <= 127 or not 0 <= value <= 127:
                raise ValidationError("Use MIDI channel 1–16 and CC/value 0–127.")
            raw = bytes([0xB0 + channel - 1, cc, value]).hex(" ").upper()
        action = {"type": "raw_midi", "message_hex": raw}
        compiled = compile_action(action, self.profiles)
        action["message_hex"] = compiled.hex
        return action

    def _update_preview(self):
        try:
            self.preview.set(compile_action(self.get_action(), self.profiles).summary)
        except ValueError as exc:
            self.preview.set(str(exc))

    def accept(self):
        try:
            self.result = self.get_action()
            self.destroy()
        except ValueError as exc:
            messagebox.showerror("Invalid MIDI command", str(exc), parent=self)


class StompilotApp(ttk.Frame):
    def __init__(self, root, project=None, *, start_screen=False):
        super().__init__(root)
        self.root, self.profiles = root, load_device_profiles()
        self.zoom = get_zoom(root)
        incoming = project or new_project(example=False)
        validate_config(incoming, self.profiles)
        self.project = normalize_project(incoming)
        self.path, self.dirty = None, False
        self.controller_dialog = None
        self.startup_frame = None
        self.origin = "Local project" if project else "New empty project"
        self.source_text = tk.StringVar()
        self.page_index, self.switch_letter, self.state_index = 0, "B", 0
        self.output, self.current_compiled = "", None
        self.refreshing = False
        self.project_name = tk.StringVar(value=self.project["name"])
        self.switch_name, self.state_name, self.mode = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.status, self.output_title, self.guidance = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self._theme()
        self.pack(fill="both", expand=True)
        self._build()
        self.project_name.trace_add("write", self._name_changed)
        self.switch_name.trace_add("write", self._switch_name_changed)
        self.state_name.trace_add("write", self._state_name_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-s>", lambda e: self.save_project())
        self.root.bind("<Control-o>", lambda e: self.open_project())
        self.refresh()
        self.zoom.listen(self, self._zoom_layout)
        self._refresh_source()
        if start_screen:
            self.show_startup()

    def _theme(self):
        self.root.title("Stompilot")
        available_width = max(320, self.root.winfo_screenwidth() - 60)
        available_height = max(240, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{min(1280, available_width)}x{min(820, available_height)}")
        self.root.minsize(min(1050, available_width), min(710, available_height))
        self.root.configure(bg=BG)
        self.root.option_add("*Font", self.zoom.font("body"))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, font=self.zoom.font("body"))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=self.zoom.font("title"))
        style.configure("Section.TLabel", font=self.zoom.font("section"))
        style.configure("TButton", background=RAISED, foreground=FG, padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", "#33435a"), ("disabled", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG)
        style.map("Accent.TButton", background=[("active", "#7bead9")])
        style.configure("TEntry", fieldbackground=RAISED, foreground=FG, insertcolor=FG, padding=7)
        style.configure("TEntry", bordercolor=RAISED, lightcolor=RAISED, darkcolor=RAISED)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=RAISED, background=RAISED, foreground=FG, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", RAISED)],
                  foreground=[("readonly", FG), ("disabled", MUTED)], selectbackground=[("readonly", RAISED)])
        style.configure("TSpinbox", fieldbackground=RAISED, foreground=FG, arrowsize=16, padding=7)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        rowheight=self.zoom.px(35), borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, font=self.zoom.font("body"))
        style.configure("Treeview.Heading", background=RAISED, foreground=MUTED, padding=8, bordercolor=RAISED, lightcolor=RAISED, darkcolor=RAISED)
        style.map("Treeview", background=[("selected", "#24524f")], foreground=[("selected", FG)])
        style.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.configure("TNotebook.Tab", background=RAISED, foreground=MUTED, padding=(18, 8), bordercolor=RAISED, lightcolor=RAISED, darkcolor=RAISED)
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
        style.configure("Horizontal.TScale", background=BG, troughcolor=RAISED)
        self.root.option_add("*TCombobox*Listbox.background", RAISED)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#24524f")
        self.zoom.refresh_styles()

    def _build(self):
        zoom_bar = ttk.Frame(self, padding=(24, 8, 24, 0))
        zoom_bar.pack(fill="x")
        self.zoom.toolbar(zoom_bar).pack(side="right")
        self.scroll_view = ScrollableFrame(self, horizontal=True)
        self.scroll_view.pack(fill="both", expand=True)
        content = self.scroll_view.content
        header = ttk.Frame(content, padding=(24, 19, 24, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Stompilot", style="Title.TLabel", font=self.zoom.font("title")).pack(side="left")
        ttk.Label(header, text="MIDI Configurator", style="Muted.TLabel").pack(side="left", padx=16)
        for label, command in [("Export messages", self.export_messages), ("Save project", self.save_project),
                               ("Open", self.open_project), ("New", self.new)]:
            ttk.Button(header, text=label, command=command).pack(side="right", padx=(8, 0))
        name_row = ttk.Frame(content, padding=(24, 0, 24, 16))
        name_row.pack(fill="x")
        ttk.Label(name_row, text="Project", style="Muted.TLabel").pack(side="left", padx=(0, 12))
        ttk.Entry(name_row, textvariable=self.project_name, width=38).pack(side="left")
        ttk.Button(name_row, text="Write to controller", command=self.configure_controller).pack(side="right")
        ttk.Button(name_row, text="Read controller", command=self.read_controller).pack(side="right", padx=8)
        source_row = ttk.Frame(content, padding=(24, 0, 24, 14))
        source_row.pack(fill="x")
        self.source_label = ttk.Label(source_row, textvariable=self.source_text, style="Muted.TLabel", wraplength=1150)
        self.source_label.pack(anchor="w", fill="x")
        source_row.bind("<Configure>", lambda event: self.source_label.configure(wraplength=max(200, event.width - 48)))
        body = ttk.Frame(content, padding=(24, 0, 24, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(body, padding=(0, 0, 18, 0))
        sidebar.grid(row=0, column=0, sticky="nsew")
        ttk.Label(sidebar, text="Pages", style="Section.TLabel").pack(anchor="w", pady=(4, 12))
        self.page_list = tk.Listbox(sidebar, width=18, height=7, bg=PANEL, fg=FG,
            selectbackground="#24524f", selectforeground=ACCENT, borderwidth=0, highlightthickness=0,
            activestyle="none", exportselection=False, font=self.zoom.font("page"))
        self.page_list.pack(fill="x")
        self.page_list.bind("<<ListboxSelect>>", self._page_selected)
        ttk.Button(sidebar, text="Add page", command=self.add_page).pack(fill="x", pady=(12, 6))
        ttk.Button(sidebar, text="Rename page", command=self.rename_page).pack(fill="x", pady=3)
        ttk.Button(sidebar, text="Remove page", command=self.remove_page).pack(fill="x", pady=3)
        ttk.Separator(sidebar).pack(fill="x", pady=22)
        ttk.Label(sidebar, text="Devices", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.profile_names = ttk.Label(sidebar, text="", style="Muted.TLabel", wraplength=165)
        self.profile_names.pack(anchor="w")
        ttk.Button(sidebar, text="Device profiles", command=self.manage_profiles).pack(fill="x", pady=14)
        main = ttk.Frame(body)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)
        cards = ttk.Frame(main)
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.switch_buttons = {}
        for letter in "ABCD":
            button = tk.Button(cards, text=letter, bg=PANEL, fg=FG, activebackground=RAISED,
                activeforeground=ACCENT, bd=0, relief="flat", cursor="hand2", font=self.zoom.font("switch"),
                height=2, command=lambda l=letter: self.select_switch(l))
            button.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.switch_buttons[letter] = button
        controls = ttk.Frame(main)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(controls, text="Switch name", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        ttk.Entry(controls, textvariable=self.switch_name, width=25).pack(side="left")
        mode_box = ttk.Combobox(controls, textvariable=self.mode, values=list(MODES), state="readonly", width=21)
        mode_box.pack(side="right")
        mode_box.bind("<<ComboboxSelected>>", self._mode_changed)
        ttk.Label(controls, text="Short press", style="Muted.TLabel").pack(side="right", padx=12)
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.tabs = [ttk.Frame(self.notebook, height=1) for _ in range(2)]
        for index, tab in enumerate(self.tabs):
            self.notebook.add(tab, text=f"State {index + 1}")
        self.notebook.bind("<<NotebookTabChanged>>", self._state_selected)
        state_row = ttk.Frame(main)
        state_row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(state_row, text="State name", style="Muted.TLabel").pack(side="left", padx=(0, 12))
        ttk.Entry(state_row, textvariable=self.state_name).pack(side="left", fill="x", expand=True)
        ttk.Button(state_row, text="Add action", style="Accent.TButton", command=self.add_action).pack(side="right", padx=(14, 0))
        action_area = ttk.Frame(main)
        action_area.grid(row=4, column=0, sticky="nsew")
        self.actions = ttk.Treeview(action_area, columns=("device", "block", "action", "value"), show="headings", selectmode="browse", height=5)
        for column, label, width in [("device", "Device", 240), ("block", "Block", 65),
                                      ("action", "Effect / parameter", 240), ("value", "Value", 100)]:
            self.actions.heading(column, text=label)
            self.actions.column(column, width=width, minwidth=55, stretch=column in ("device", "action"))
        self.actions.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(action_area, command=self.actions.yview)
        scroll.pack(side="right", fill="y")
        self.actions.configure(yscrollcommand=scroll.set)
        self.actions.bind("<Double-1>", lambda e: self.edit_action())
        self.actions.bind("<Delete>", lambda e: self.remove_action())
        toolbar = ttk.Frame(main)
        toolbar.grid(row=5, column=0, sticky="ew", pady=(10, 12))
        for label, command in [("Edit", self.edit_action), ("Remove", self.remove_action),
                               ("Move up", lambda: self.move_action(-1)), ("Move down", lambda: self.move_action(1))]:
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=(0, 8))
        preview_header = ttk.Frame(main)
        preview_header.grid(row=6, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(preview_header, textvariable=self.output_title, style="Section.TLabel").pack(side="left")
        self.copy_button = ttk.Button(preview_header, text="Copy for MidiSuite", command=self.copy_output)
        self.copy_button.pack(side="right")
        self.hex_text = tk.Text(main, height=3, bg=PANEL, fg=ACCENT, insertbackground=FG,
            font=self.zoom.font("preview"), wrap="word", relief="flat", padx=14, pady=10, state="disabled")
        self.hex_text.grid(row=7, column=0, sticky="ew")
        self.guidance_label = ttk.Label(main, textvariable=self.guidance, style="Muted.TLabel", wraplength=850)
        self.guidance_label.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        main.bind("<Configure>", lambda event: self.guidance_label.configure(wraplength=max(200, event.width - 12)))
        ttk.Label(content, textvariable=self.status, style="Muted.TLabel", padding=(24, 8, 24, 12)).pack(side="bottom", fill="x", before=body)

    def _zoom_layout(self, percent):
        for column, width in (("device", 240), ("block", 65), ("action", 240), ("value", 100)):
            self.actions.column(column, width=self.zoom.px(width), minwidth=self.zoom.px(55))
        self.profile_names.configure(wraplength=self.zoom.px(165))

    def selected_switch(self):
        return self.project["pages"][self.page_index]["switches"][self.switch_letter]

    def selected_state(self):
        return self.selected_switch()["states"][self.state_index]

    def mark_dirty(self):
        self.dirty = True
        self.root.title("Stompilot *")
        self._refresh_source()

    def _refresh_source(self):
        state = "Local working copy, modified" if self.dirty else "Local working copy"
        self.source_text.set(f"Source: {self.origin}\n{state}. Save project stores a file; Write to controller applies assignments.")

    def show_startup(self):
        self.pack_forget()
        self.root.title("Stompilot")
        self.startup_frame = ttk.Frame(self.root, padding=36)
        self.startup_frame.pack(fill="both", expand=True)
        self.zoom.toolbar(self.startup_frame).pack(anchor="e", pady=(0, 18))
        startup_scroll = ScrollableFrame(self.startup_frame, horizontal=True)
        startup_scroll.pack(fill="both", expand=True)
        startup_scroll.content.columnconfigure(0, weight=1)
        startup_scroll.content.rowconfigure((0, 2), weight=1)
        panel = ttk.Frame(startup_scroll.content, padding=12)
        panel.grid(row=1, column=0)
        ttk.Label(panel, text="Stompilot", style="Title.TLabel", font=self.zoom.font("title")).pack(anchor="w")
        ttk.Label(panel, text="Choose where to start", style="Section.TLabel", font=self.zoom.font("welcome")).pack(anchor="w", pady=(22, 8))
        ttk.Label(panel, text="Open a saved project or read the assignments stored on Chocolate Plus.",
                  style="Muted.TLabel", wraplength=680).pack(anchor="w", pady=(0, 24))
        cards = ttk.Frame(panel)
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1, uniform="start")
        for column, title, detail, button, command in (
            (0, "Saved project", "Load a Stompilot JSON file.\nIts values come from the file.", "Open saved project", self.open_project),
            (1, "Chocolate Plus", "Read the controller over USB.\nReading does not change its configuration.", "Read Chocolate Plus", self.read_controller),
        ):
            card = ttk.Frame(cards, style="Panel.TFrame", padding=24)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 14) if column == 0 else 0)
            ttk.Label(card, text=title, background=PANEL, font=self.zoom.font("section")).pack(anchor="w")
            ttk.Label(card, text=detail, background=PANEL, foreground=MUTED,
                      wraplength=300).pack(anchor="w", pady=(12, 24))
            ttk.Button(card, text=button, style="Accent.TButton", command=command).pack(fill="x")
        ttk.Button(panel, text="Create empty project", command=self.new).pack(anchor="w", pady=(24, 10))
        ttk.Label(panel, text="No configuration loaded. Nothing is sent to the controller on startup.",
                  style="Muted.TLabel", wraplength=680).pack(anchor="w")

    def _show_editor(self):
        if self.startup_frame is not None:
            self.startup_frame.destroy()
            self.startup_frame = None
        self.pack(fill="both", expand=True)

    def read_controller(self):
        if not self.can_discard():
            return
        if self.controller_dialog is not None and self.controller_dialog.winfo_exists():
            self.controller_dialog.lift()
            return
        try:
            if __package__:
                from .controller_ui import ControllerDialog
            else:
                from controller_ui import ControllerDialog
            self.controller_dialog = ControllerDialog(self.root, on_load=self._load_controller)
        except (ImportError, ValueError, OSError) as exc:
            messagebox.showerror("Controller connection", str(exc), parent=self.root)

    def _load_controller(self, image, identity):
        if identity.firmware_version != 16:
            raise ValidationError("Loading into the editor supports Chocolate Plus firmware 16. The controller configuration remains available for inspection.")
        if __package__:
            from .chocolate import import_controller_project
        else:
            from chocolate import import_controller_project
        project = import_controller_project(image, self.profiles)
        stamp = datetime.now().strftime("%H:%M:%S")
        self._replace_project(project, origin=f"Chocolate Plus · {identity.name} · read at {stamp}")
        self.status.set("Controller assignments loaded into a local working copy. Reading did not change the controller.")

    def _name_changed(self, *args):
        if not self.refreshing:
            self.project["name"] = self.project_name.get()
            self.mark_dirty()

    def _switch_name_changed(self, *args):
        if not self.refreshing:
            self.selected_switch()["label"] = self.switch_name.get()
            self.mark_dirty()
            self._refresh_cards()

    def _state_name_changed(self, *args):
        if not self.refreshing:
            self.selected_state()["label"] = self.state_name.get()
            self.mark_dirty()

    def _refresh_cards(self):
        for letter, button in self.switch_buttons.items():
            switch = self.project["pages"][self.page_index]["switches"][letter]
            button.configure(text=f"{letter}\n{switch['label']}", bg="#24524f" if letter == self.switch_letter else PANEL,
                             fg=ACCENT if letter == self.switch_letter else FG)

    def refresh(self):
        self.refreshing = True
        self.page_list.delete(0, "end")
        for page in self.project["pages"]:
            self.page_list.insert("end", "  " + page["name"])
        self.page_list.selection_set(self.page_index)
        self.profile_names.configure(text="\n\n".join(p["name"] for p in self.profiles.values()))
        self._refresh_cards()
        switch = self.selected_switch()
        self.switch_name.set(switch["label"])
        self.mode.set(next(label for label, value in MODES.items() if value == switch["mode"]))
        self.notebook.tab(1, state="normal" if switch["mode"] == "alternating" else "hidden")
        self.state_index = min(self.state_index, len(switch["states"]) - 1)
        self.notebook.select(self.tabs[self.state_index])
        self.refreshing = False
        self.refresh_state()

    def refresh_state(self):
        self.refreshing = True
        self.state_name.set(self.selected_state()["label"])
        self.actions.delete(*self.actions.get_children())
        for index, action in enumerate(self.selected_state()["actions"]):
            self.actions.insert("", "end", iid=str(index), values=description(action, self.profiles))
        self.refreshing = False
        self.refresh_output()

    def refresh_output(self):
        try:
            limit = sysex_limit(self.project)
            result = compile_state(self.selected_state(), self.profiles, limit)
            self.current_compiled = result
            bank = "A" if self.state_index == 0 else "B"
            mode_name = "Single step (switch 2 banks)" if self.selected_switch()["mode"] == "alternating" else "Single step (single bank)"
            if result.sysex_hex:
                self.output = result.sysex_hex
                self.output_title.set(f"Bank {bank} · SysEx · {result.byte_count}/{limit} bytes")
                help_text = f"MidiSuite: switch {self.switch_letter} → {mode_name} → Bank {bank} → SysEx (hex). Paste all content into one field."
            elif result.cc_rows:
                self.output = "\n".join(f"Channel {r['channel']} · Type CC · Data 1 {r['data1']} · Data 2 {r['data2']}" for r in result.cc_rows)
                self.output_title.set(f"Bank {bank} · {len(result.cc_rows)} CC messages")
                help_text = f"MidiSuite: switch {self.switch_letter} → {mode_name} → Bank {bank}. Add one CC row for each command."
            else:
                self.output = ""
                self.output_title.set(f"Bank {bank} · No messages")
                help_text = "Add an action to configure this state."
            prerequisites = []
            for action in self.selected_state()["actions"]:
                if action["type"] == "raw_midi":
                    continue
                profile = self.profiles[action["device_id"]]
                if profile["protocol"] == "tank_v09" and action.get("model_id"):
                    block = next(b for b in profile["blocks"] if b["id"] == action["block_id"])
                    model = next(m for m in block["models"] if m["id"] == action["model_id"])
                    prerequisite = f"{block['label']} = {model['label']}"
                    if prerequisite not in prerequisites:
                        prerequisites.append(prerequisite)
            if prerequisites:
                help_text += "\nPreset: " + "; ".join(prerequisites) + "."
            if any(action["type"] == "reload_current_preset" for action in self.selected_state()["actions"]):
                help_text += "\nExperimental reload: restores the whole saved preset, including all effects. Hardware test pending."
            if len(result.messages) > 1 and result.sysex_hex:
                help_text += " Multiple consecutive SysEx messages: needs testing on the Chocolate."
            self.guidance.set(help_text)
            self.copy_button.configure(state="normal" if self.output else "disabled")
            self.status.set(f"{len(self.profiles)} profiles · {len(self.project['pages'])} pages · {len(self.selected_state()['actions'])} actions in the selected state")
        except (ValueError, KeyError, StopIteration) as exc:
            self.current_compiled, self.output = None, ""
            self.output_title.set("Configuration needs correction")
            self.guidance.set(str(exc))
            self.copy_button.configure(state="disabled")
            self.status.set("Correct the actions before exporting.")
        self.hex_text.configure(state="normal")
        self.hex_text.delete("1.0", "end")
        self.hex_text.insert("1.0", self.output)
        self.hex_text.configure(state="disabled")

    def _page_selected(self, event=None):
        selection = self.page_list.curselection()
        if selection and not self.refreshing and selection[0] != self.page_index:
            self.page_index, self.state_index = selection[0], 0
            self.refresh()

    def select_switch(self, letter):
        self.switch_letter, self.state_index = letter, 0
        self.refresh()

    def _state_selected(self, event=None):
        if not self.refreshing:
            index = self.notebook.index(self.notebook.select())
            if index < len(self.selected_switch()["states"]) and index != self.state_index:
                self.state_index = index
                self.refresh_state()

    def _mode_changed(self, event=None):
        switch = self.selected_switch()
        mode = MODES[self.mode.get()]
        if mode == "single" and len(switch["states"]) == 2:
            if switch["states"][1]["actions"] and not messagebox.askyesno("Remove the second state?", "Switching to one sequence will remove this switch's second state.", parent=self.root):
                self.refresh()
                return
            switch["states"] = switch["states"][:1]
        elif mode == "alternating" and len(switch["states"]) == 1:
            switch["states"].append({"label": "State 2", "actions": []})
        switch["mode"] = mode
        self.state_index = 0
        self.mark_dirty()
        self.refresh()

    def selected_action_index(self):
        selected = self.actions.selection()
        return int(selected[0]) if selected else None

    def add_action(self):
        dialog = ActionDialog(self.root, self.profiles)
        self.root.wait_window(dialog)
        if dialog.result:
            self.selected_state()["actions"].append(dialog.result)
            self.mark_dirty()
            self.refresh_state()

    def edit_action(self):
        index = self.selected_action_index()
        if index is None:
            return
        action = self.selected_state()["actions"][index]
        if action["type"] == "model":
            messagebox.showinfo("Model change", "This version changes states and parameters. Select the model in the device's preset.", parent=self.root)
            return
        dialog = (RawMidiDialog(self.root, self.profiles, action) if action["type"] == "raw_midi"
                  else ActionDialog(self.root, self.profiles, action))
        self.root.wait_window(dialog)
        if dialog.result:
            self.selected_state()["actions"][index] = dialog.result
            self.mark_dirty()
            self.refresh_state()
            self.actions.selection_set(str(index))

    def remove_action(self):
        index = self.selected_action_index()
        if index is not None:
            del self.selected_state()["actions"][index]
            self.mark_dirty()
            self.refresh_state()

    def move_action(self, direction):
        index = self.selected_action_index()
        if index is None:
            return
        actions = self.selected_state()["actions"]
        target = index + direction
        if 0 <= target < len(actions):
            actions[index], actions[target] = actions[target], actions[index]
            self.mark_dirty()
            self.refresh_state()
            self.actions.selection_set(str(target))

    def add_page(self):
        if len(self.project["pages"]) >= MAX_UI_PAGES:
            messagebox.showinfo("Pages", f"This version supports up to {MAX_UI_PAGES} pages for Chocolate Plus.", parent=self.root)
            return
        self.project["pages"].append(empty_page(len(self.project["pages"]) + 1))
        self.page_index, self.state_index = len(self.project["pages"]) - 1, 0
        self.mark_dirty()
        self.refresh()

    def rename_page(self):
        value = messagebox.askstring("Page name", "Page name:", initialvalue=self.project["pages"][self.page_index]["name"], parent=self.root)
        if value and value.strip():
            self.project["pages"][self.page_index]["name"] = value.strip()
            self.mark_dirty()
            self.refresh()

    def remove_page(self):
        if len(self.project["pages"]) == 1:
            messagebox.showinfo("Pages", "Keep at least one page in the project.", parent=self.root)
            return
        if messagebox.askyesno("Remove page?", "Remove this page and its assignments?", parent=self.root):
            del self.project["pages"][self.page_index]
            self.page_index, self.state_index = max(0, self.page_index - 1), 0
            self.mark_dirty()
            self.refresh()

    def copy_output(self):
        if self.output:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.output)
            self.status.set("Messages copied. Paste them into the specified bank in MidiSuite.")

    def save_project(self):
        if self.startup_frame is not None:
            return False
        try:
            validate_config(self.project, self.profiles)
            target = self.path or filedialog.asksaveasfilename(parent=self.root, title="Save Stompilot project", defaultextension=".json", filetypes=[("Stompilot project", "*.json")], initialfile="stompilot.json")
            if not target:
                return False
            save_config(target, self.project, self.profiles)
            self.path, self.dirty = Path(target), False
            self.root.title("Stompilot")
            self._refresh_source()
            self.status.set(f"Project saved: {self.path.name}")
            return True
        except (ValueError, OSError) as exc:
            messagebox.showerror("Save", str(exc), parent=self.root)
            return False

    def can_discard(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("Project changes", "Save changes before continuing?", parent=self.root)
        return self.save_project() if answer is True else answer is False

    def new(self):
        if self.can_discard():
            self._replace_project(new_project(example=False), origin="New empty project")

    def _replace_project(self, project, path=None, *, origin=None):
        validate_config(project, self.profiles)
        project = normalize_project(project)
        self.project, self.path, self.dirty = project, path, False
        self.origin = origin or (f"Saved project · {Path(path).name}" if path else "Local project")
        self.page_index, self.switch_letter, self.state_index = 0, "A", 0
        self.refreshing = True
        self.project_name.set(project["name"])
        self.refreshing = False
        self.root.title("Stompilot")
        self._refresh_source()
        self._show_editor()
        self.refresh()

    def open_project(self):
        if not self.can_discard():
            return
        path = filedialog.askopenfilename(parent=self.root, title="Open Stompilot project", filetypes=[("JSON projects", "*.json")])
        if path:
            try:
                self._replace_project(load_config(path, self.profiles), Path(path))
            except (ValueError, OSError) as exc:
                messagebox.showerror("Invalid project", str(exc), parent=self.root)

    def export_text(self):
        validate_config(self.project, self.profiles)
        lines = [f"STOMPILOT · {self.project['name']}", "Controller: Chocolate Plus / MidiSuite", "",
                 f"Set Max Group Count = {len(self.project['pages'])}.",
                 "States are Bank A/B of the same switch, not separate pages.", ""]
        for page_number, page in enumerate(self.project["pages"], 1):
            lines.append(f"PAGE {page_number}: {page['name']}")
            for letter, switch in page["switches"].items():
                mode_name = "Single step (switch 2 banks)" if switch["mode"] == "alternating" else "Single step (single bank)"
                lines.append(f"\nSWITCH {letter} · {switch['label']} · {mode_name}")
                for index, state in enumerate(switch["states"]):
                    result = compile_state(state, self.profiles, sysex_limit(self.project))
                    lines.append(f"\nBank {'A' if index == 0 else 'B'}: {state['label']}")
                    lines.extend(result.summaries)
                    if result.sysex_hex:
                        lines.extend(["Type: SysEx (hex). Paste all lines into the same field:", result.sysex_hex])
                    elif result.cc_rows:
                        lines.extend(f"Channel {r['channel']} / Type CC / Data 1 {r['data1']} / Data 2 {r['data2']}" for r in result.cc_rows)
                    else:
                        lines.append("No messages.")
                    lines.extend("Note: " + warning for warning in result.warnings)
            lines.append("\n")
        return "\n".join(lines)

    def export_messages(self):
        try:
            output = self.export_text()
            path = filedialog.asksaveasfilename(parent=self.root, title="Export messages for MidiSuite", defaultextension=".txt", filetypes=[("MIDI message summary", "*.txt")], initialfile="stompilot-midi.txt")
            if path:
                Path(path).write_text(output, encoding="utf-8")
                self.status.set("Messages exported. The file is a summary to copy into MidiSuite.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Export", str(exc), parent=self.root)

    def manage_profiles(self):
        window = tk.Toplevel(self.root)
        window.title("Device profiles · Stompilot")
        window.configure(bg=BG)
        window.geometry("760x590")
        window.transient(self.root)
        self.zoom.toolbar(window).pack(fill="x", padx=22, pady=(10, 0))
        profile_scroll = ScrollableFrame(window, horizontal=True)
        profile_scroll.pack(fill="both", expand=True)
        frame = ttk.Frame(profile_scroll.content, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Device profiles", style="Section.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Browse device information, import new JSON profiles, or export a profile to create a variant.",
                  style="Muted.TLabel", wraplength=710).pack(anchor="w", pady=(6, 16))
        selected = tk.StringVar(value=next(iter(self.profiles.values()))["name"])
        box = ttk.Combobox(frame, textvariable=selected, state="readonly", values=[p["name"] for p in self.profiles.values()])
        box.pack(fill="x", pady=(0, 12))
        text = tk.Text(frame, bg=PANEL, fg=FG, wrap="word", relief="flat", padx=14, pady=12, font=self.zoom.font("body"))
        text.pack(fill="both", expand=True)
        def display(event=None):
            profile = next(p for p in self.profiles.values() if p["name"] == selected.get())
            evidence = profile.get("evidence", {})
            evidence_note = evidence.get("note", "") if isinstance(evidence, dict) else str(evidence)
            content = [profile["name"], "", f"Protocol: {profile['protocol']}",
                       f"Blocks: {len(profile['blocks'])}", "", evidence_note]
            notes = profile.get("notes", [])
            content.extend(["", *(notes if isinstance(notes, list) else [str(notes)])])
            content.append("\nProfile sources:")
            for source in profile.get("sources", []):
                if isinstance(source, dict):
                    content.extend([source.get("title", ""), source.get("url", ""), ""])
                else:
                    content.append(str(source))
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", "\n".join(content))
            text.configure(state="disabled")
        box.bind("<<ComboboxSelected>>", display)
        display()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 16), before=box)
        def import_profile():
            path = filedialog.askopenfilename(parent=window, title="Import device profile", filetypes=[("JSON profile", "*.json")])
            if not path:
                return
            try:
                profile = validate_profile(json.loads(Path(path).read_text(encoding="utf-8-sig")))
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile["id"]):
                    raise ValidationError("The profile ID may contain only letters, numbers, hyphens, and underscores.")
                if profile["id"] in self.profiles or any(p["name"] == profile["name"] for p in self.profiles.values()):
                    raise ValidationError("A profile with this ID or name already exists. Use a different ID and name for a variant.")
                target = PROFILE_DIR / f"{profile['id']}.json"
                if target.exists():
                    raise ValidationError("A profile file with this name already exists.")
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                self.profiles = load_device_profiles()
                window.destroy()
                self.refresh()
                self.status.set(f"Profile imported: {profile['name']}")
            except (ValueError, OSError) as exc:
                messagebox.showerror("Invalid profile", str(exc), parent=window)
        def export_profile():
            profile = next(p for p in self.profiles.values() if p["name"] == selected.get())
            path = filedialog.asksaveasfilename(parent=window, title="Export profile", defaultextension=".json", initialfile=profile["id"] + ".json", filetypes=[("JSON profile", "*.json")])
            if path:
                try:
                    Path(path).write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                except OSError as exc:
                    messagebox.showerror("Export", str(exc), parent=window)
        ttk.Button(buttons, text="Import JSON profile", style="Accent.TButton", command=import_profile).pack(side="left")
        ttk.Button(buttons, text="Export profile", command=export_profile).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        self.zoom.listen(window, lambda percent: window.geometry(
            f"{min(self.zoom.px(760), window.winfo_screenwidth() - 60)}x{min(self.zoom.px(670), window.winfo_screenheight() - 100)}"))

    def configure_controller(self):
        if self.controller_dialog is not None and self.controller_dialog.winfo_exists():
            self.controller_dialog.lift()
            return
        try:
            validate_config(self.project, self.profiles)
            if __package__:
                from .controller_ui import ControllerDialog
            else:
                from controller_ui import ControllerDialog
            self.controller_dialog = ControllerDialog(self.root, self.project, self.profiles)
        except (ImportError, ValueError, OSError) as exc:
            messagebox.showerror("Controller connection", str(exc), parent=self.root)

    def close(self):
        if self.controller_dialog is not None and self.controller_dialog.winfo_exists():
            if not self.controller_dialog.close():
                return
        if self.can_discard():
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Stompilot, a local visual MIDI configurator")
    parser.add_argument("--check", action="store_true", help="check profiles and the example project without opening windows")
    parser.add_argument("--smoke-test", action="store_true", help="check the interface and close the window")
    parser.add_argument("--screenshot", type=Path, help="save a screenshot of the app and close the window")
    args = parser.parse_args()
    profiles = load_device_profiles()
    project = new_project()
    validate_config(project, profiles)
    if args.check:
        boost = compile_state(project["pages"][0]["switches"]["B"]["states"][0], profiles)
        print(json.dumps({"profiles": list(profiles), "pages": 1, "boost_on_bytes": boost.byte_count,
                          "boost_on_hex": boost.sysex_hex}, indent=2))
        return
    root = tk.Tk()
    if args.smoke_test or args.screenshot:
        ZoomManager(root, persist=False)
    if args.smoke_test:
        root.withdraw()
    app = StompilotApp(root, project if args.smoke_test else None, start_screen=not args.smoke_test)
    if args.smoke_test:
        root.update()
        assert app.current_compiled.byte_count == 43
        assert app.state_name.get() == "Boost ON / Mix 10"
        app.state_index = 1
        app.refresh_state()
        assert app.current_compiled.byte_count == 43
        assert "05 00 78 04 F7" in app.output
        app.select_switch("C")
        assert app.output == ""
        app.select_switch("A")
        assert app.current_compiled.byte_count == 21
        app.add_page()
        assert len(app.project["pages"]) == 2 and app.output == ""
        assert "PAGE 2" in app.export_text()
        dialog = ActionDialog(root, profiles, project["pages"][0]["switches"]["B"]["states"][0]["actions"][1])
        root.update()
        assert dialog.get_action()["value"] == 10
        assert dialog.get_action()["param_id"] == "mix"
        unchanged_project = json.dumps(app.project, sort_keys=True)
        unchanged_action = dialog.get_action()
        unchanged_output = app.output
        app.zoom.set_percent(200)
        root.update_idletasks()
        assert dialog.zoom is app.zoom
        assert dialog.get_action() == unchanged_action
        assert json.dumps(app.project, sort_keys=True) == unchanged_project
        assert app.output == unchanged_output
        assert app.zoom.font("title").cget("size") == 42
        assert app.zoom.font("body").cget("size") == 20
        app.zoom.reset()
        dialog.destroy()
        reload_action = {"device_id": "tank_mini_v09", "type": "reload_current_preset"}
        dialog = ActionDialog(root, profiles, reload_action)
        assert dialog.get_action() == reload_action
        assert dialog.block_box.winfo_manager() == ""
        assert compile_action(dialog.get_action(), profiles).hex == (
            "F0 00 32 09 49 00 00 00 02 01 00 00 00 12 00 00 00 7F 35 03 F7")
        dialog.destroy()
        minimal = {"schema_version": 1, "name": "Minimal import", "pages": [
            {"name": "Page 1", "switches": {"A": {"mode": "single", "states": [{}]}}}]}
        app._replace_project(minimal)
        assert set(app.project["pages"][0]["switches"]) == set("ABCD")
        assert app.project["controller"]["max_sysex_bytes"] == 127
        assert app.state_name.get() == "State 1" and app.output == ""
        assert "SWITCH D" in app.export_text()
        app.show_startup()
        assert app.startup_frame is not None and app.winfo_manager() == ""
        app.new()
        assert app.startup_frame is None and "New empty project" in app.source_text.get()
        if __package__:
            from .chocolate import ADVANCED_MODE, BANK_A_ROWS, CONFIG_SIZE, switch_offset, plan_project
        else:
            from chocolate import ADVANCED_MODE, BANK_A_ROWS, CONFIG_SIZE, switch_offset, plan_project
        image = bytearray(CONFIG_SIZE)
        image[0] = ADVANCED_MODE
        offset = switch_offset(0, "A") + BANK_A_ROWS
        image[offset:offset + 5] = bytes([1, 0, 1, 2, 0])
        app._load_controller(bytes(image), SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16))
        assert "Chocolate Plus" in app.source_text.get() and "Local working copy" in app.source_text.get()
        assert app.selected_state()["actions"] == [{"type": "raw_midi", "message_hex": "B0 02 00"}]
        assert plan_project(app.project, profiles, bytes(image)).changed_bytes == 0
        dialog = RawMidiDialog(root, profiles, app.selected_state()["actions"][0])
        assert dialog.get_action()["message_hex"] == "B0 02 00"
        app.zoom.set_percent(150)
        root.update_idletasks()
        assert dialog.zoom is app.zoom and dialog.get_action()["message_hex"] == "B0 02 00"
        assert app.zoom.font("mono").cget("size") == 15
        app.zoom.reset()
        dialog.value.set("10")
        assert dialog.get_action()["message_hex"] == "B0 02 0A"
        dialog.destroy()
        if getattr(sys, "frozen", False):
            from chocolate_transport import list_usb_devices
            list_usb_devices()
            from controller_ui import BACKUP_DIR
            assert BACKUP_DIR.parent == Path(sys.executable).resolve().parent
            assert PROFILE_DIR.parent == Path(sys.executable).resolve().parent
        root.destroy()
        print("GUI smoke test OK: global zoom, startup, source banner, controller import, raw MIDI editing, states, pages, export, reload action.")
        return
    if args.screenshot:
        from PIL import ImageGrab
        def capture_window():
            try:
                ImageGrab.grab(window=root.winfo_id()).save(args.screenshot)
            finally:
                root.destroy()
        root.after(500, capture_window)
        root.mainloop()
        print(args.screenshot)
        return
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if any(arg in sys.argv for arg in ("--check", "--smoke-test", "--screenshot")):
            raise
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror("Stompilot", f"Unable to start the configurator:\n{exc}", parent=error_root)
        error_root.destroy()
        raise SystemExit(1)
