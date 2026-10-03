"""USB controller editor. Tk stays on the main thread; MIDI work is serialized."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import tkinter as tk
from tkinter import ttk

if __package__:
    from .chocolate import config_summary, plan_project
    from .chocolate_transport import UsbChocolate, list_usb_devices
    from .runtime_paths import data_directory
    from .ui_scale import ScrollableFrame, get_zoom
    from . import ui_dialogs as messagebox
else:
    from chocolate import config_summary, plan_project
    from chocolate_transport import UsbChocolate, list_usb_devices
    from runtime_paths import data_directory
    from ui_scale import ScrollableFrame, get_zoom
    import ui_dialogs as messagebox


BACKUP_DIR = data_directory() / ".backups"


def save_backup(image, identity, directory=BACKUP_DIR):
    """Persist the exact image before any write, including its integrity hash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"chocolate-plus-{stamp}.json"
    content = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device_name": identity.name,
        "firmware_version": str(identity.firmware_version),
        "image_size": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
        "image_hex": image.hex(),
    }
    with path.open("x", encoding="utf-8") as stream:
        json.dump(content, stream, indent=2)
        stream.write("\n")
    return path


class ControllerDialog(tk.Toplevel):
    def __init__(self, parent, project=None, profiles=None, *, on_load=None,
                 transport_factory=UsbChocolate, discovery=list_usb_devices,
                 backup_directory=BACKUP_DIR):
        if on_load is None and project is None:
            raise ValueError("A project is required for controller programming.")
        super().__init__(parent)
        self.zoom = get_zoom(parent)
        self.on_load, self.import_mode = on_load, on_load is not None
        self.import_ready = False
        self.title("Chocolate Plus · Read controller configuration" if self.import_mode else
                   "Chocolate Plus · Direct USB configuration")
        self.geometry("1000x740")
        self.minsize(800, 620)
        self.configure(bg="#111721")
        self.transient(parent)
        self.grab_set()
        self.project, self.profiles = deepcopy(project), deepcopy(profiles or {})
        self.transport_factory, self.discovery = transport_factory, discovery
        self.backup_directory = backup_directory
        self.transport = self.identity = self.image = self.plan = None
        self.devices, self.busy, self.alive = [], False, True
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stompilot-usb")
        self.events = Queue()
        self.device_name, self.connection_status = tk.StringVar(), tk.StringVar(value="Disconnected")
        self.info = tk.StringVar(value="Connect Chocolate Plus by USB and close MidiSuite before connecting.")
        self.status = tk.StringVar(value="Choose the controller, then read its configuration.")
        self._build()
        self.zoom.listen(self, self._zoom_changed)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = self.after(80, self._drain)
        self.scan()

    def _build(self):
        self.container = ttk.Frame(self, padding=self.zoom.px(22))
        self.container.pack(fill="both", expand=True)
        self.zoom.toolbar(self.container).pack(anchor="e", pady=(0, 12))
        self.scroll = ScrollableFrame(self.container, horizontal=True)
        self.scroll.pack(fill="both", expand=True)
        frame = self.scroll.content
        ttk.Label(frame, text="Read Chocolate Plus" if self.import_mode else
                  "Configure Chocolate Plus", style="Section.TLabel").pack(anchor="w")
        self.info_label = ttk.Label(frame, textvariable=self.info, style="Muted.TLabel", wraplength=940)
        self.info_label.pack(anchor="w", pady=(6, 14))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.device_box = ttk.Combobox(row, textvariable=self.device_name, state="readonly", width=37)
        self.device_box.pack(side="left", fill="x", expand=True)
        self.scan_button = ttk.Button(row, text="Refresh devices", command=self.scan)
        self.scan_button.pack(side="left", padx=8)
        self.read_button = ttk.Button(row, text="Connect and read", command=self.read)
        self.read_button.pack(side="left")
        ttk.Label(frame, textvariable=self.connection_status, style="Muted.TLabel").pack(anchor="w", pady=(10, 12))
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill="both", expand=True)
        self.preview_text = None
        if not self.import_mode:
            preview = ttk.Frame(self.tabs)
            self.tabs.add(preview, text="Project to write")
            self.preview_text = self._text(preview)
            self._set_text(self.preview_text, self._project_overview())
        current = ttk.Frame(self.tabs)
        self.tabs.add(current, text="Current controller configuration")
        self.current_text = self._text(current)
        self._set_text(self.current_text, "Read the controller to inspect its saved configuration.")
        self.status_label = ttk.Label(frame, textvariable=self.status, style="Muted.TLabel", wraplength=930)
        self.status_label.pack(anchor="w", pady=(14, 8))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))
        footer = ttk.Frame(frame)
        footer.pack(fill="x")
        self.disconnect_button = ttk.Button(footer, text="Disconnect", command=self.disconnect)
        self.disconnect_button.pack(side="left")
        self.close_button = ttk.Button(footer, text="Close", command=self.close)
        self.close_button.pack(side="right")
        self.write_button = self.load_button = None
        if self.import_mode:
            self.load_button = ttk.Button(footer, text="Load into editor", style="Accent.TButton",
                                          command=self.load_into_editor)
            self.load_button.pack(side="right", padx=10)
        else:
            self.write_button = ttk.Button(footer, text="Write project to controller", style="Accent.TButton", command=self.write)
            self.write_button.pack(side="right", padx=10)
        self._controls()
        self.scroll.canvas.bind("<Configure>", self._wrap_text, add="+")

    def _zoom_changed(self, _percent):
        self.container.configure(padding=self.zoom.px(22))
        self.zoom.fit_window(self, 1000, 740)
        self._wrap_text()

    def _wrap_text(self, _event=None):
        width = max(160, self.scroll.canvas.winfo_width() - self.zoom.px(24))
        self.info_label.configure(wraplength=width)
        self.status_label.configure(wraplength=width)

    @staticmethod
    def _text(parent):
        area = tk.Text(parent, bg="#1b2432", fg="#edf4ff", wrap="word", relief="flat",
                       font=get_zoom(parent).font("mono"), padx=16, pady=14, state="disabled")
        scroll = ttk.Scrollbar(parent, command=area.yview)
        scroll.pack(side="right", fill="y")
        area.pack(side="left", fill="both", expand=True)
        area.configure(yscrollcommand=scroll.set)
        return area

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _project_overview(self):
        lines = [self.project["name"], ""]
        for number, page in enumerate(self.project["pages"], 1):
            lines.append(f"Page {number}: {page['name']}")
            for letter, switch in page["switches"].items():
                lines.append(f"  Switch {letter}: {switch['label']}")
                for bank, state in zip("AB", switch["states"]):
                    lines.append(f"    Bank {bank}: {state['label']} ({len(state['actions'])} actions)")
            lines.append("")
        lines.append("Read the controller to check compatibility and preview the exact assignments.")
        return "\n".join(lines)

    def _controls(self):
        idle = not self.busy
        connected = self.transport is not None
        self.device_box.configure(state="readonly" if idle and not connected else "disabled")
        self.scan_button.configure(state="normal" if idle and not connected else "disabled")
        self.read_button.configure(state="normal" if idle and (connected or self.devices) else "disabled",
                                   text="Read again" if connected else "Connect and read")
        self.disconnect_button.configure(state="normal" if idle and connected else "disabled")
        if self.write_button is not None:
            self.write_button.configure(state="normal" if idle and self.plan is not None else "disabled")
        if self.load_button is not None:
            self.load_button.configure(state="normal" if idle and self.import_ready else "disabled")
        self.close_button.configure(state="normal" if idle else "disabled")

    def _submit(self, operation, done):
        if self.busy:
            return
        self.busy = True
        self._controls()
        self.progress.start(15)
        future = self.executor.submit(operation)
        future.add_done_callback(lambda completed: self.events.put(("done", completed, done)))

    def _progress(self, *parts):
        if len(parts) == 2 and all(isinstance(part, int) for part in parts):
            message = f"Transferring configuration: {parts[0]:,} / {parts[1]:,} bytes"
        else:
            message = " ".join(str(part) for part in parts)
        self.events.put(("status", message))

    def _drain(self):
        if not self.alive:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "status":
                    self.status.set(event[1])
                else:
                    self.busy = False
                    self.progress.stop()
                    try:
                        event[2](event[1].result())
                    except Exception as exc:
                        self.plan = None
                        self.import_ready = False
                        self.status.set(str(exc))
                        messagebox.showerror("Controller operation failed", str(exc), parent=self)
                    self._controls()
        except Empty:
            pass
        self.poll_id = self.after(80, self._drain)

    def scan(self):
        self.status.set("Looking for USB MIDI controllers…")
        def done(devices):
            self.devices = devices
            self.device_box.configure(values=[device.name for device in devices])
            self.device_name.set(devices[0].name if devices else "")
            self.status.set("Choose the controller, then connect." if devices else
                            "No Chocolate Plus found. Connect its USB cable and refresh devices.")
        self._submit(self.discovery, done)

    def read(self):
        self.import_ready = False
        self.status.set("Reading controller configuration…")
        selected = next((device for device in self.devices if device.name == self.device_name.get()), None)
        def operation():
            if self.transport is None:
                if selected is None:
                    raise ValueError("Select a Chocolate Plus controller first.")
                transport = self.transport_factory(selected.input_name, selected.output_name)
                try:
                    transport.open()
                    identity = transport.query()
                    image = transport.read_config(progress_callback=self._progress)
                except Exception:
                    transport.close()
                    raise
                self.transport, self.identity = transport, identity
            else:
                image = self.transport.read_config(progress_callback=self._progress)
            return image
        self._submit(operation, self._read_done)

    def _read_done(self, image):
        summary = config_summary(image)
        self.image = image
        self.connection_status.set(f"{self.identity.name} · Firmware {self.identity.firmware_version} · USB connected")
        self._set_text(self.current_text, summary)
        self.plan = None
        if self.import_mode:
            self.import_ready = True
            self.status.set("Configuration read. Review its assignments, then choose Load into editor.")
            return
        if self.identity.firmware_version != 16:
            message = (
                f"Direct writing is supported for Chocolate Plus firmware 16. "
                f"This controller reports firmware {self.identity.firmware_version}. "
                "Its configuration can be inspected, and you can still copy messages into MidiSuite."
            )
            self._set_text(self.preview_text, self._project_overview() + "\n\n" + message)
            self.status.set(message)
            return
        self.plan = plan_project(self.project, self.profiles, image)
        self._set_text(self.preview_text, "\n".join(self.plan.summaries) +
                       "\n\n" + "\n".join(self.plan.warnings))
        self.status.set("Configuration read. Review the project before writing. A backup will be saved automatically.")

    def write(self):
        if self.import_mode or self.plan is None or self.transport is None or self.busy:
            return
        self.status.set("Reading a fresh backup before writing…")
        reviewed_image = self.image
        def operation():
            before = self.transport.read_config(progress_callback=self._progress)
            if before != reviewed_image:
                raise ValueError(
                    "The controller configuration changed since the preview. "
                    "Choose Read again to review its current assignments before writing."
                )
            plan = plan_project(self.project, self.profiles, before)
            backup = save_backup(before, self.identity, self.backup_directory)
            self._progress(f"Backup saved: {backup.name}. Writing project…")
            verified = self.transport.write_config(before, plan.image, progress_callback=self._progress)
            return verified, backup
        def done(result):
            image, backup = result
            self._read_done(image)
            self.status.set(f"Project written and readback verified. Backup: {backup.name}")
        self._submit(operation, done)

    def load_into_editor(self):
        if not self.import_mode or not self.import_ready or self.busy or not self.alive:
            return
        image, identity, callback = self.image, self.identity, self.on_load
        try:
            callback(image, identity)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Unable to load controller configuration", str(exc), parent=self)
            return
        self.close()

    def disconnect(self):
        def operation():
            self.transport.close()
        def done(result):
            self.transport = self.identity = self.image = self.plan = None
            self.import_ready = False
            self.connection_status.set("Disconnected")
            self.status.set("Disconnected. Read a controller to load its configuration." if self.import_mode else
                            "Disconnected. The project remains available in Stompilot.")
        if self.transport is not None:
            self._submit(operation, done)

    def close(self):
        if self.busy:
            messagebox.showinfo("Controller operation in progress", "Wait for the controller operation to finish before closing.", parent=self)
            return False
        if self.transport is not None:
            self.transport.close()
        self.alive = False
        self.after_cancel(self.poll_id)
        self.executor.shutdown(wait=False)
        self.destroy()
        return True
