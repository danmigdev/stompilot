r"""Capture real Stompilot windows with reproducible, read-only USB fixtures.

Run on Windows with Pillow installed:
    .venv\Scripts\python.exe docs/capture_screenshots.py

No hardware MIDI ports are opened. USB screenshots show a simulated controller
containing the exact commands from examples/tank_mini_live_boost.json. Captures
are limited to this process's application windows, including its native notes
messagebox; the desktop is never captured.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from PIL import ImageGrab


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from midi_configurator.app import ActionDialog, RawMidiDialog, StompilotApp
from midi_configurator.chocolate import CONFIG_SIZE, plan_project
from midi_configurator.chocolate_transport import UsbDevice
from midi_configurator.core import load_config
from midi_configurator.controller_ui import ControllerDialog


class WindowCapture:
    def __init__(self, root, directory):
        self.root, self.directory = root, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.results = []
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL

    def settle(self, window):
        window.lift()
        window.focus_force()
        for _ in range(6):
            self.root.update()
            time.sleep(0.04)

    def save_handle(self, handle, filename):
        screenshot = ImageGrab.grab(window=handle)
        screenshot.save(self.directory / filename)
        self.results.append({"file": filename, "width": screenshot.width, "height": screenshot.height})

    def save(self, window, filename):
        self.settle(window)
        handle = self.user32.GetAncestor(window.winfo_id(), 2)
        self.save_handle(handle, filename)

    def notes(self, app):
        """Capture the application's actual native Setup notes messagebox."""
        owner = self.user32.GetAncestor(self.root.winfo_id(), 2)
        errors = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL

        def find_messagebox():
            matches = []
            @callback_type
            def visit(handle, _):
                pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value != os.getpid() or self.user32.GetWindow(handle, 4) != owner:
                    return True
                title = ctypes.create_unicode_buffer(256)
                class_name = ctypes.create_unicode_buffer(256)
                self.user32.GetWindowTextW(handle, title, len(title))
                self.user32.GetClassNameW(handle, class_name, len(class_name))
                if title.value == "Setup notes" and class_name.value == "#32770":
                    matches.append(handle)
                return True
            self.user32.EnumWindows(visit, 0)
            return matches[0] if matches else None

        def capture_native():
            handle = None
            try:
                deadline = time.monotonic() + 10
                while handle is None and time.monotonic() < deadline:
                    handle = find_messagebox()
                    time.sleep(0.05)
                if handle is None:
                    raise RuntimeError("The native Setup notes dialog did not appear.")
                time.sleep(0.25)
                self.save_handle(handle, "setup-notes.png")
            except Exception as error:
                errors.append(error)
            finally:
                if handle is not None:
                    self.user32.PostMessageW(handle, 0x0010, 0, 0)

        thread = threading.Thread(target=capture_native, daemon=True)
        thread.start()
        app.show_notes()
        thread.join(timeout=11)
        if errors:
            raise errors[0]
        if thread.is_alive():
            raise RuntimeError("The native Setup notes capture did not finish.")


def wait_idle(root, dialog):
    deadline = time.monotonic() + 5
    while dialog.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    root.update()
    if dialog.busy:
        raise RuntimeError("The simulated USB read did not finish.")


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def reader_factory(image):
    class ReadOnlyController:
        def __init__(self, *_):
            self.image = image
        def open(self):
            return self
        def close(self):
            pass
        def query(self):
            return SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16)
        def read_config(self, progress_callback=None):
            if progress_callback:
                progress_callback(len(self.image), len(self.image))
            return self.image
        def write_config(self, *_args, **_kwargs):
            raise RuntimeError("Screenshot fixtures are read-only; writing is prohibited.")
    return ReadOnlyController


def capture_all(directory, project_path):
    if os.name != "nt":
        raise RuntimeError("Application-window capture requires Windows.")
    root = tk.Tk()
    try:
        app = StompilotApp(root, start_screen=True)
        root.geometry("1280x900+20+20")
        capture = WindowCapture(root, directory)
        capture.save(root, "startup.png")
        project = load_config(project_path, app.profiles)
        app._replace_project(project, project_path)
        app.select_switch("B")
        capture.save(root, "project-editor.png")
        app.state_index = 1
        app.refresh()
        capture.save(root, "reload-state.png")
        capture.notes(app)
        states = project["pages"][0]["switches"]["B"]["states"]
        for filename, action in (
            ("effect-toggle-editor.png", states[0]["actions"][0]),
            ("parameter-editor.png", states[0]["actions"][1]),
            ("reload-action-editor.png", states[1]["actions"][0]),
        ):
            dialog = ActionDialog(root, app.profiles, action)
            capture.save(dialog, filename)
            dialog.destroy()
        before = set(root.winfo_children())
        app.manage_profiles()
        profile_window = next(window for window in root.winfo_children()
                              if window not in before and isinstance(window, tk.Toplevel))
        profile_window.geometry("760x650+160+80")
        box = next(widget for widget in descendants(profile_window) if isinstance(widget, ttk.Combobox))
        box.set(app.profiles["tank_mini_v09"]["name"])
        box.event_generate("<<ComboboxSelected>>")
        capture.save(profile_window, "device-profiles.png")
        profile_window.destroy()

        image = plan_project(project, app.profiles, bytes(CONFIG_SIZE)).image
        device = UsbDevice("FootCtrlPlus", "FootCtrlPlus 0", "FootCtrlPlus 1")
        usb_options = {"transport_factory": reader_factory(image), "discovery": lambda: [device]}
        read_dialog = ControllerDialog(root, on_load=app._load_controller, **usb_options)
        read_dialog.geometry("1000x800+120+40")
        wait_idle(root, read_dialog)
        read_dialog.read()
        wait_idle(root, read_dialog)
        capture.save(read_dialog, "controller-read.png")
        read_dialog.load_into_editor()
        app.select_switch("B")
        capture.save(root, "imported-editor.png")
        raw_sysex = app.project["pages"][0]["switches"]["B"]["states"][0]["actions"][0]
        raw_dialog = RawMidiDialog(root, app.profiles, raw_sysex)
        capture.save(raw_dialog, "imported-sysex-editor.png")
        raw_dialog.destroy()
        raw_dialog = RawMidiDialog(root, app.profiles, {"type": "raw_midi", "message_hex": "B0 02 00"})
        capture.save(raw_dialog, "imported-cc-editor.png")
        raw_dialog.destroy()

        app._replace_project(project, project_path)
        app.select_switch("B")
        write_dialog = ControllerDialog(root, project, app.profiles, **usb_options)
        write_dialog.geometry("1000x900+120+20")
        wait_idle(root, write_dialog)
        write_dialog.read()
        wait_idle(root, write_dialog)
        capture.save(write_dialog, "controller-write-preview.png")
        write_dialog.tabs.select(1)
        capture.save(write_dialog, "controller-current.png")
        write_dialog.close()
        return sorted(capture.results, key=lambda item: item["file"])
    finally:
        for child in root.winfo_children():
            if isinstance(child, ControllerDialog) and child.winfo_exists():
                wait_idle(root, child)
                child.close()
        root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs" / "screenshots")
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT / "examples" / "tank_mini_live_boost.json")
    args = parser.parse_args()
    print(json.dumps(capture_all(args.output, args.project), indent=2))


if __name__ == "__main__":
    main()
