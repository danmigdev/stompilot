"""Portable Svelte desktop host; USB and project logic live in DesktopService."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time

if __package__:
    from .desktop_service import DesktopService
    from .core import compile_config
    from .project import new_project
    from .runtime_paths import resource_dir
else:
    from desktop_service import DesktopService
    from core import compile_config
    from project import new_project
    from runtime_paths import resource_dir


# The smoke test starts from these saved preferences and checks that they come back.
SMOKE_SETTINGS = {"ui_zoom_percent": 150,
                  "window": {"x": 40, "y": 40, "width": 900, "height": 640, "maximized": False}}
SMOKE_BOUNDS = tuple(SMOKE_SETTINGS["window"][key] for key in ("x", "y", "width", "height"))


def frontend_directory():
    return resource_dir().parent / "frontend" / "build"


def window_placement(saved, screens, default_size, min_size):
    """Reuse saved bounds only when their title bar lands on a connected screen.

    Coordinates are logical pixels, as pywebview reports them. Otherwise the
    window opens centered at its default size.
    """
    placement = {"x": None, "y": None, "width": default_size[0], "height": default_size[1], "maximized": False}
    if saved is None:
        return placement
    for screen in screens:
        overlap = min(saved["x"] + saved["width"], screen.x + screen.width) - max(saved["x"], screen.x)
        # Windows draws invisible resize borders, so a window snapped to the top can start slightly above it.
        if overlap >= 100 and screen.y - 16 <= saved["y"] <= screen.y + screen.height - 40:
            return {"x": saved["x"], "y": saved["y"], "maximized": saved["maximized"],
                    "width": min(max(saved["width"], min_size[0]), screen.width),
                    "height": min(max(saved["height"], min_size[1]), screen.height)}
    return placement


class DesktopBridge:
    """Expose only the message API to the embedded application."""
    def __init__(self, service):
        self._service = service
        self._window = None
        self._allow_close = False

    def request(self, method, payload=None):
        payload = {} if payload is None else payload
        if method == "close_application":
            if not isinstance(payload, dict):
                return {"ok": False, "error": {"message": "Request payload must be an object."}}
            if self._service.is_busy:
                return {"ok": False, "error": {"message": "Wait for the current operation to finish before closing."}}
            snapshot = self._service.snapshot()
            if snapshot["dirty"] and payload.get("discard") is not True:
                return {"ok": False, "error": {"message": "Save or discard the project changes before closing."}}
            if not self._service.close():
                return {"ok": False, "error": {"message": "Wait for the current operation to finish before closing."}}
            self._allow_close = True
            if self._window is not None:
                threading.Timer(0.15, self._window.destroy).start()
            return {"ok": True, "data": snapshot}
        result = self._service.request(method, payload)
        if result.get("ok") and self._window is not None:
            self._window.set_title("Stompilot *" if result["data"]["dirty"] else "Stompilot")
        return result

    def _dispatch(self, name, detail=None):
        script = "window.dispatchEvent(new CustomEvent(" + json.dumps(name)
        script += ",{detail:" + json.dumps(detail or {}) + "}));"
        def deliver():
            try:
                self._window.run_js(script)
            except Exception:
                pass
        threading.Thread(target=deliver, daemon=True).start()

    def _remember_window(self):
        """Save the normal bounds, also when maximized; pywebview calls closing on the UI thread."""
        try:
            from System.Windows.Forms import FormWindowState
            form = self._window.native
            state = form.WindowState
            bounds = form.Bounds if state == FormWindowState.Normal else form.RestoreBounds
            scale = form._scale
            self._service.save_window_geometry({
                "x": round(bounds.X / scale), "y": round(bounds.Y / scale),
                "width": round(bounds.Width / scale), "height": round(bounds.Height / scale),
                "maximized": state == FormWindowState.Maximized})
        except Exception:
            # Closing never depends on remembering the window.
            pass

    def _on_closing(self):
        if self._allow_close:
            self._remember_window()
            return True
        if self._service.is_busy:
            self._dispatch("stompilot-notice", {"message": "Wait for the controller operation to finish before closing."})
            return False
        if self._service.snapshot()["dirty"]:
            self._dispatch("stompilot-close-request")
            return False
        closed = self._service.close()
        if closed:
            self._remember_window()
        return closed

    def _pick_file(self, kind, suggested_name="", file_types=()):
        import webview
        save = kind in ("save_project", "export_messages", "export_profile")
        filters = tuple(f"{label} ({pattern})" for label, pattern in file_types)
        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE if save else webview.FileDialog.OPEN,
            allow_multiple=False, save_filename=suggested_name, file_types=filters)
        return selected[0] if selected else None


def _wait_js(window, expression, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if window.evaluate_js(expression):
            return
        time.sleep(0.05)
    raise RuntimeError("The desktop interface did not finish the requested operation.")


def _capture_window(window, target):
    if os.name != "nt":
        raise RuntimeError("Application-window capture requires Windows.")
    from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
    from System import Action, Func, Object
    from System.IO import MemoryStream
    from System.Threading.Tasks import Task
    complete = threading.Event()
    errors = []
    stream = MemoryStream()
    target.parent.mkdir(parents=True, exist_ok=True)
    def finished(task):
        try:
            task.GetAwaiter().GetResult()
            target.write_bytes(bytes(stream.ToArray()))
        except Exception as error:
            errors.append(error)
        finally:
            complete.set()
    def begin():
        task = window.native.browser.webview.CoreWebView2.CapturePreviewAsync(
            CoreWebView2CapturePreviewImageFormat.Png, stream)
        task.ContinueWith(Action[Task](finished))
        return None
    try:
        window.native.Invoke(Func[Object](begin))
        if not complete.wait(10):
            raise RuntimeError("The application's renderer did not finish the screenshot.")
        if errors:
            raise errors[0]
    finally:
        stream.Close()


def _smoke(window, bridge):
    """Exercise the compiled Svelte UI and actual Python bridge without MIDI."""
    _wait_js(window, "!!document.querySelector('[data-testid=\"startup-new\"]')")
    initial = bridge._service.snapshot()
    assert initial["project"] is None and not initial["controller"]["connected"]
    _wait_js(window, "document.querySelector('[data-testid=\"zoom-value\"]').textContent.includes('150%')")
    bounds = (window.x, window.y, window.width, window.height)
    # Display scaling rounds logical pixels.
    assert all(abs(actual - saved) <= 2 for actual, saved in zip(bounds, SMOKE_BOUNDS)), \
        f"The saved window bounds were not restored: {bounds}"
    window.evaluate_js("document.querySelector('[data-testid=\"startup-new\"]').click()")
    _wait_js(window, "!!document.querySelector('[data-testid=\"add-action\"]')")
    baseline = bridge._service.snapshot()
    assert baseline["project"]["name"] == "New project" and not baseline["dirty"]
    font_before = window.evaluate_js("parseFloat(getComputedStyle(document.querySelector('[data-testid=\"zoom-value\"]')).fontSize)")
    window.evaluate_js("document.querySelector('[data-testid=\"zoom-in\"]').click()")
    _wait_js(window, "document.querySelector('[data-testid=\"zoom-value\"]').textContent.includes('175%')")
    deadline = time.monotonic() + 5
    while bridge._service.snapshot()["zoom_percent"] != 175 and time.monotonic() < deadline:
        time.sleep(0.05)
    after = bridge._service.snapshot()
    assert after["zoom_percent"] == 175 and after["project"] == baseline["project"] and not after["dirty"]
    font_after = window.evaluate_js("parseFloat(getComputedStyle(document.querySelector('[data-testid=\"zoom-value\"]')).fontSize)")
    assert font_after > font_before
    window.evaluate_js("document.querySelector('[data-testid=\"add-action\"]').click()")
    _wait_js(window, "!!document.querySelector('[data-testid=\"action-editor\"]')")
    assert bridge._service.snapshot()["project"] == baseline["project"]
    return {"ok": True, "checks": ["Svelte assets", "Python bridge", "explicit startup", "saved zoom",
                                    "saved window bounds", "empty project", "global text zoom",
                                    "action editor", "no MIDI I/O"]}


def main():
    parser = argparse.ArgumentParser(description="Stompilot Svelte desktop MIDI configurator")
    parser.add_argument("--check", action="store_true", help="validate bundled profiles and UI assets without opening MIDI")
    parser.add_argument("--smoke-test", action="store_true", help="check the desktop interface without MIDI access")
    parser.add_argument("--screenshot", type=Path, help="capture the application's startup window and exit")
    parser.add_argument("--report", type=Path, help="write automation results as JSON")
    args = parser.parse_args()
    temporary = TemporaryDirectory() if args.smoke_test or args.screenshot else None
    outcome = {"ok": True}
    try:
        if args.smoke_test:
            (Path(temporary.name) / "settings.json").write_text(json.dumps(SMOKE_SETTINGS), encoding="utf-8")
        service = (DesktopService(data_directory=temporary.name, discovery=lambda: [])
                   if temporary else DesktopService())
        entry = frontend_directory() / "index.html"
        if not entry.is_file():
            raise RuntimeError("The Svelte interface is missing. Run Build Stompilot.cmd or build the frontend from source.")
        if args.check:
            compiled = compile_config(new_project(), service.profiles)
            print(json.dumps({"profiles": list(service.profiles), "frontend_ready": True,
                              "example_pages": len(compiled["pages"])}))
            return
        import webview
        bridge = DesktopBridge(service)
        if not temporary:
            service.dialog = bridge._pick_file
        screens = webview.screens
        width, height = min(1280, max(320, screens[0].width - 60)), min(860, max(240, screens[0].height - 100))
        min_size = (min(620, width), min(440, height))
        placement = window_placement(service.window_geometry(), screens, (width, height), min_size)
        window = webview.create_window(
            "Stompilot", str(entry), js_api=bridge, min_size=min_size,
            background_color="#10161f", text_select=True, **placement)
        bridge._window = window
        window.events.closing += bridge._on_closing
        def automate():
            if not (args.smoke_test or args.screenshot):
                return
            try:
                if not window.events.loaded.wait(20):
                    raise RuntimeError("The desktop interface did not load.")
                _wait_js(window, "!!document.querySelector('[data-testid=\"startup-new\"]')")
                if args.smoke_test:
                    outcome.update(_smoke(window, bridge))
                if args.screenshot:
                    time.sleep(0.25)
                    _capture_window(window, args.screenshot)
            except Exception as error:
                outcome.update({"ok": False, "error": str(error)})
            finally:
                bridge._allow_close = True
                window.destroy()
        webview.start(automate, gui="edgechromium" if os.name == "nt" else None,
                      private_mode=True, http_server=True)
        service.close()
        if args.smoke_test and outcome["ok"]:
            saved = service.window_geometry()
            if saved is None or any(abs(saved[key] - value) > 2 for key, value in
                                    zip(("x", "y", "width", "height"), SMOKE_BOUNDS)):
                outcome.update({"ok": False, "error": f"The window bounds were not saved on close: {saved}"})
            else:
                outcome["checks"].append("window bounds saved on close")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
        if not outcome["ok"]:
            raise RuntimeError(outcome["error"])
        if args.smoke_test:
            print("Svelte desktop smoke test passed.")
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if any(arg in sys.argv for arg in ("--check", "--smoke-test", "--screenshot")):
            raise
        message = ("Unable to start Stompilot.\n\n" + str(error) +
                   "\n\nOn Windows, install Microsoft Edge WebView2 Runtime and try again.")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "Stompilot", 0x10)
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1)
