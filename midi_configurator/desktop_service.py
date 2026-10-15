"""UI-independent desktop API; offline editing never opens MIDI ports.

Controller calls are synchronous and belong on the host's worker threads.
Snapshots use a separate short-lived lock, so polling remains responsive while
USB transfers run. All actual device transfers are serialized.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
import threading
from typing import Mapping

if __package__:
    from .core import (ValidationError, compile_config, compile_state,
                       load_profiles, sysex_limit, validate_config, validate_profile)
    from .project import new_project, normalize_project
    from .chocolate import (CONFIG_SIZE, _decode_bank, _native_bank, config_summary,
                            import_controller_project, legacy_sysex_banks, plan_project)
    from .chocolate_transport import UsbChocolate, list_usb_devices
    from .runtime_paths import (data_directory as default_data_directory,
                                profile_directory as default_profile_directory,
                                resource_dir)
else:
    from core import (ValidationError, compile_config, compile_state,
                      load_profiles, sysex_limit, validate_config, validate_profile)
    from project import new_project, normalize_project
    from chocolate import (CONFIG_SIZE, _decode_bank, _native_bank, config_summary,
                           import_controller_project, legacy_sysex_banks, plan_project)
    from chocolate_transport import UsbChocolate, list_usb_devices
    from runtime_paths import (data_directory as default_data_directory,
                               profile_directory as default_profile_directory,
                               resource_dir)


JSON_TYPES = (("JSON files", "*.json"),)
TEXT_TYPES = (("MIDI message summary", "*.txt"),)


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _fingerprint(project, profiles):
    content = json.dumps({"project": project, "profiles": profiles}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_text(path, text):
    """A failed save leaves the previous file intact."""
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".stompilot-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _exclusive_text(path, text):
    """Create a durable file without replacing an existing profile or backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def save_backup(image, identity, directory):
    """Persist and check the exact original image before permitting any write."""
    image = bytes(image)
    if len(image) != CONFIG_SIZE:
        raise ValidationError(f"A backup requires exactly {CONFIG_SIZE} bytes.")
    now = datetime.now(timezone.utc)
    path = Path(directory) / f"chocolate-plus-{now.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    digest = hashlib.sha256(image).hexdigest()
    content = {"schema_version": 1, "created_at": now.isoformat(),
               "device_name": identity.name, "firmware_version": str(identity.firmware_version),
               "image_size": len(image), "sha256": digest, "image_hex": image.hex()}
    _exclusive_text(path, _json_text(content))
    saved = json.loads(path.read_text(encoding="utf-8"))
    if bytes.fromhex(saved["image_hex"]) != image or saved["sha256"] != digest:
        raise OSError("The controller backup could not be verified. No configuration was written.")
    return path


class DesktopService:
    """Own one editable working copy and one explicitly opened USB connection.

    ``dialog(kind, suggested_name='', file_types=...)`` is injected by the
    desktop host. A return value of None or an empty string means cancellation.
    The API deliberately accepts no browser-provided absolute file paths.
    """

    def __init__(self, *, profiles=None, data_directory=None, profile_directory=None,
                 backup_directory=None, transport_factory=UsbChocolate,
                 discovery=list_usb_devices, dialog=None):
        self._lock = threading.RLock()
        self._commands = threading.Lock()
        self._io = threading.Lock()
        self._settings = threading.Lock()
        self.data_directory = Path(data_directory) if data_directory is not None else default_data_directory()
        self.profile_directory = (Path(profile_directory) if profile_directory is not None else
                                  self.data_directory / "profiles" if data_directory is not None else
                                  default_profile_directory())
        self.backup_directory = (Path(backup_directory) if backup_directory is not None else
                                 self.data_directory / ".backups")
        self.settings_path = self.data_directory / "settings.json"
        self.transport_factory, self.discovery, self.dialog = transport_factory, discovery, dialog
        self._bundled_directory = resource_dir() / "profiles"
        if profiles is None:
            self._defaults = load_profiles(self._bundled_directory)
        elif isinstance(profiles, Mapping):
            self._defaults = deepcopy(dict(profiles))
        else:
            self._defaults = {profile["id"]: deepcopy(profile) for profile in profiles}
        for profile_id, profile in self._defaults.items():
            validate_profile(profile)
            if profile_id != profile["id"]:
                raise ValidationError("The profile dictionary key must match its profile ID.")
        self.profiles = self._load_profiles()
        self.project = self._clean_project = self.compiled = self.path = None
        self.origin, self.dirty = "", False
        self.zoom_percent = self._read_zoom()
        self.devices = []
        self.transport = self.identity = self.image = self.plan = None
        self._reviewed_hash = self._reviewed_image = None
        self._import_project = None
        self.busy = False
        self.summary = ""
        self.status = "Choose the controller, then read its configuration."
        self._closed = False

    def _load_profiles(self):
        profiles = deepcopy(self._defaults)
        if (self.profile_directory != self._bundled_directory and self.profile_directory.is_dir()
                and any(self.profile_directory.glob("*.json"))):
            for profile_id, profile in load_profiles(self.profile_directory).items():
                if profile_id in profiles:
                    raise ValidationError(f"Duplicate profile ID: {profile_id!r}")
                profiles[profile_id] = profile
        names = set()
        for profile in profiles.values():
            if profile["name"] in names:
                raise ValidationError(f"Duplicate profile name: {profile['name']!r}")
            names.add(profile["name"])
        return profiles

    def _read_zoom(self):
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8-sig")).get("ui_zoom_percent", 100)
            if isinstance(value, int) and not isinstance(value, bool) and 75 <= value <= 200:
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return 100

    def _portable_error(self, error):
        if isinstance(error, OSError) and error.filename:
            name = PureWindowsPath(str(error.filename)).name
            return f"{name}: {error.strerror or 'Unable to access this file.'}"
        message = str(error) or "The operation could not be completed."
        for path in (self.data_directory, self.profile_directory, self.backup_directory,
                     self.settings_path, self._bundled_directory, self.path):
            if path is not None:
                message = message.replace(str(path), Path(path).name)
        # Native Windows error messages may include a second path or a temporary filename.
        return re.sub(r"[A-Za-z]:[\\/][^\r\n'\"]+", lambda match:
                      PureWindowsPath(match.group(0)).name, message)

    def snapshot(self):
        with self._lock:
            preview = None if self.plan is None else {
                "summaries": list(self.plan.summaries), "warnings": list(self.plan.warnings),
                "changed_bytes": self.plan.changed_bytes}
            return deepcopy({"profiles": list(self.profiles.values()), "project": self.project,
                             "origin": self.origin, "dirty": self.dirty,
                             "file_name": self.path.name if self.path else None,
                             "zoom_percent": self.zoom_percent,
                             "controller": {"devices": [{"name": device.name,
                                 "input_name": device.input_name, "output_name": device.output_name}
                                 for device in self.devices], "connected": self.transport is not None,
                                 "busy": self.busy, "identity": None if self.identity is None else {
                                     "name": self.identity.name, "firmware_version": self.identity.firmware_version},
                                 "summary": self.summary, "preview": preview, "status": self.status,
                                 "can_import": not self.busy and self._import_project is not None,
                                 "can_write": not self.busy and self.plan is not None
                                     and self.transport is not None and self.identity is not None
                                     and self.identity.firmware_version == 16}, "compiled": self.compiled})

    @property
    def is_busy(self):
        with self._lock:
            return self.busy or self._commands.locked()

    @property
    def can_close(self):
        return not self.is_busy

    def request(self, method, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            return {"ok": False, "error": {"message": "Request payload must be an object."}}
        if not isinstance(method, str):
            return {"ok": False, "error": {"message": "Unknown desktop operation."}}
        if method == "bootstrap":
            return {"ok": True, "data": self.snapshot()}
        if method == "set_zoom":
            # Text preferences remain usable while USB or a file picker is busy.
            with self._settings:
                try:
                    if self._closed:
                        raise ValidationError("The desktop service is closed.")
                    self._set_zoom(payload)
                    return {"ok": True, "data": self.snapshot()}
                except Exception as error:
                    return {"ok": False, "error": {"message": self._portable_error(error)}}
        allowed = {"new_project", "open_project", "save_project", "set_project", "export_messages",
                   "import_profile", "export_profile", "set_zoom", "controller_scan", "controller_read",
                   "controller_preview", "controller_import", "controller_write", "controller_disconnect"}
        if method not in allowed:
            return {"ok": False, "error": {"message": "Unknown desktop operation."}}
        if not self._commands.acquire(blocking=False):
            return {"ok": False, "error": {"message": "Another desktop operation is in progress. Wait for it to finish."}}
        try:
            if self._closed:
                raise ValidationError("The desktop service is closed.")
            getattr(self, "_" + method)(payload)
            return {"ok": True, "data": self.snapshot()}
        except Exception as error:
            return {"ok": False, "error": {"message": self._portable_error(error)}}
        finally:
            self._commands.release()

    def _pick(self, kind, suggested_name="", file_types=JSON_TYPES):
        if self.dialog is None:
            raise ValidationError("This operation requires the desktop file picker.")
        value = self.dialog(kind, suggested_name=suggested_name, file_types=file_types)
        return Path(value) if value else None

    def _validated_project(self, project):
        # Preserve optional native bank bytes and all schema metadata verbatim.
        project = deepcopy(project)
        _json_text(project)
        validate_config(project, self.profiles)
        project = normalize_project(project)
        for page_index, page in enumerate(project["pages"], 1):
            for letter, switch in page["switches"].items():
                for state_index, state in enumerate(switch["states"], 1):
                    if "native_bank" in state:
                        path = f"Page {page_index}, switch {letter}, state {state_index}"
                        rows, sysex = _native_bank(state["native_bank"], path)
                        _decode_bank(rows, sysex, path)
        return project, compile_config(project, self.profiles)

    def _invalidate_preview(self):
        self.plan = self._reviewed_hash = self._reviewed_image = None

    def _replace_project(self, project, *, path=None, origin=""):
        project, compiled = self._validated_project(project)
        with self._lock:
            self.project, self.compiled, self.path = project, compiled, path
            self._clean_project = deepcopy(project)
            self.origin, self.dirty = origin, False
            self._invalidate_preview()

    def _require_project(self):
        if self.project is None:
            raise ValidationError("Create, open, or import a project first.")

    def _new_project(self, _payload):
        self._replace_project(new_project(example=False), origin="New empty project")

    def _open_project(self, _payload):
        path = self._pick("open_project")
        if path is None:
            return
        project = json.loads(path.read_text(encoding="utf-8-sig"))
        self._replace_project(project, path=path, origin=f"Saved project · {path.name}")

    def _set_project(self, payload):
        self._require_project()
        project, compiled = self._validated_project(payload.get("project"))
        with self._lock:
            if project != self.project:
                self.project, self.compiled = project, compiled
                self.dirty = project != self._clean_project
                self._invalidate_preview()

    def _save_project(self, payload):
        self._require_project()
        project, _ = self._validated_project(self.project)
        path = None if payload.get("save_as", False) else self.path
        if path is None:
            path = self._pick("save_project", self.path.name if self.path else "stompilot.json")
        if path is None:
            return
        _atomic_text(path, _json_text(project))
        with self._lock:
            self.path, self.dirty = path, False
            self._clean_project = deepcopy(project)

    def export_text(self):
        with self._lock:
            self._require_project()
            project, profiles = deepcopy(self.project), deepcopy(self.profiles)
        validate_config(project, profiles)
        lines = [f"STOMPILOT · {project['name']}", "Controller: Chocolate Plus / MidiSuite", "",
                 f"Set Max Group Count = {len(project['pages'])}.",
                 "States are Bank A/B of the same switch, not separate pages.", ""]
        for page_number, page in enumerate(project["pages"], 1):
            lines.append(f"PAGE {page_number}: {page['name']}")
            for letter, switch in page["switches"].items():
                mode = "Single step (switch 2 banks)" if switch["mode"] == "alternating" else "Single step (single bank)"
                lines.append(f"\nSWITCH {letter} · {switch['label']} · {mode}")
                for index, state in enumerate(switch["states"]):
                    result = compile_state(state, profiles, sysex_limit(project))
                    lines.append(f"\nBank {'A' if index == 0 else 'B'}: {state['label']}")
                    lines.extend(result.summaries)
                    if result.sysex_hex:
                        lines.extend(["Type: SysEx (hex). Paste all lines into the same field:", result.sysex_hex])
                    elif result.cc_rows:
                        lines.extend(f"Channel {row['channel']} / Type CC / Data 1 {row['data1']} / Data 2 {row['data2']}"
                                     for row in result.cc_rows)
                    else:
                        lines.append("No messages.")
                    lines.extend("Note: " + warning for warning in result.warnings)
            lines.append("\n")
        return "\n".join(lines)

    def _export_messages(self, _payload):
        output = self.export_text()
        path = self._pick("export_messages", "stompilot-midi.txt", TEXT_TYPES)
        if path is not None:
            _atomic_text(path, output)

    def _import_profile(self, _payload):
        path = self._pick("import_profile")
        if path is None:
            return
        profile = validate_profile(json.loads(path.read_text(encoding="utf-8-sig")))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile["id"]):
            raise ValidationError("The profile ID may contain only letters, numbers, hyphens, and underscores.")
        if profile["id"] in self.profiles or any(existing["name"] == profile["name"] for existing in self.profiles.values()):
            raise ValidationError("A profile with this ID or name already exists. Use a different ID and name for a variant.")
        target = self.profile_directory / f"{profile['id']}.json"
        _exclusive_text(target, _json_text(profile))
        try:
            profiles = self._load_profiles()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        with self._lock:
            self.profiles = profiles
            self._invalidate_preview()

    def _export_profile(self, payload):
        profile = self.profiles.get(payload.get("profile_id"))
        if profile is None:
            raise ValidationError("Choose a device profile to export.")
        path = self._pick("export_profile", profile["id"] + ".json")
        if path is not None:
            if path.resolve() in {file.resolve() for file in self._bundled_directory.glob("*.json")}:
                raise ValidationError("Bundled device profiles are read-only. Choose another filename.")
            _atomic_text(path, _json_text(profile))

    def _set_zoom(self, payload):
        percent = payload.get("percent")
        if not isinstance(percent, int) or isinstance(percent, bool) or not 75 <= percent <= 200:
            raise ValidationError("Zoom must be an integer from 75 to 200 percent.")
        with self._lock:
            self.zoom_percent = percent
        try:
            try:
                settings = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                settings = {}
            if not isinstance(settings, dict):
                settings = {}
            settings["ui_zoom_percent"] = percent
            _atomic_text(self.settings_path, _json_text(settings))
        except (OSError, ValueError):
            # Zoom remains useful even on a read-only portable drive.
            pass

    def _progress(self, *parts):
        message = (f"Transferring configuration: {parts[0]:,} / {parts[1]:,} bytes"
                   if len(parts) == 2 and all(isinstance(part, int) for part in parts)
                   else " ".join(str(part) for part in parts))
        with self._lock:
            self.status = message

    def _controller_operation(self, message, operation):
        with self._lock:
            if self.busy:
                raise ValidationError("A controller operation is in progress. Wait for it to finish.")
            self.busy, self.status = True, message
        try:
            with self._io:
                return operation()
        except Exception as error:
            with self._lock:
                self._invalidate_preview()
                self._import_project = None
                self.image = None
                self.status = self._portable_error(error)
            raise
        finally:
            with self._lock:
                self.busy = False

    def _controller_scan(self, _payload):
        if self.transport is not None:
            raise ValidationError("Disconnect the controller before refreshing devices.")
        def operation():
            devices = list(self.discovery())
            with self._lock:
                self.devices = devices
                self.status = ("Choose the controller, then connect." if devices else
                               "No Chocolate Plus found. Connect its USB cable and refresh devices.")
        self._controller_operation("Looking for USB MIDI controllers…", operation)

    def _inspect(self, image, identity):
        summary = config_summary(image)
        imported, note = None, ""
        if identity.firmware_version == 16:
            try:
                imported = import_controller_project(image, self.profiles)
            except ValidationError as error:
                note = str(error)
        else:
            note = (f"Editing and direct writing support Chocolate Plus firmware 16. This controller reports "
                    f"firmware {identity.firmware_version}. Its configuration is available for inspection.")
        legacy = legacy_sysex_banks(image)
        if legacy:
            warning = (f"{len(legacy)} SysEx bank(s) were written by an earlier Stompilot in a format the "
                       f"controller does not send: {'; '.join(legacy)}.")
            if imported is not None:
                warning += " Import as project, then preview and write it to rewrite them in the correct format."
            elif identity.firmware_version == 16:
                warning += " Writing a project that assigns these switches rewrites them in the correct format."
            note = f"{warning} {note}".strip()
        return summary, imported, note

    def _controller_read(self, payload):
        selected = next((device for device in self.devices if device.name == payload.get("device_name")), None)
        if self.transport is None and selected is None:
            raise ValidationError("Select a Chocolate Plus controller first.")
        def operation():
            transport = self.transport
            created = transport is None
            if created:
                transport = self.transport_factory(selected.input_name, selected.output_name)
            try:
                if created:
                    transport.open()
                identity = transport.query()
                image = bytes(transport.read_config(progress_callback=self._progress))
                summary, imported, note = self._inspect(image, identity)
            except Exception:
                if created:
                    transport.close()
                with self._lock:
                    self._import_project = None
                raise
            with self._lock:
                self.transport, self.identity, self.image = transport, identity, image
                self.summary, self._import_project = summary, imported
                self._invalidate_preview()
                self.status = note or "Configuration read. Review its assignments before importing or writing."
        self._controller_operation("Reading controller configuration…", operation)

    def _require_controller(self):
        if self.transport is None or self.image is None or self.identity is None:
            raise ValidationError("Connect and read the controller first.")
        if self.identity.firmware_version != 16:
            raise ValidationError("Editing and direct writing support Chocolate Plus firmware 16. This configuration is available for inspection.")

    def _controller_preview(self, _payload):
        self._require_project()
        self._require_controller()
        plan = plan_project(self.project, self.profiles, self.image)
        fingerprint = _fingerprint(self.project, self.profiles)
        with self._lock:
            self.plan, self._reviewed_hash, self._reviewed_image = plan, fingerprint, self.image
            self.status = "Review the exact assignments before writing. An exact backup will be saved automatically."

    def _controller_import(self, _payload):
        self._require_controller()
        if self._import_project is None:
            raise ValidationError("This configuration is available for inspection but contains assignments that cannot be imported for editing.")
        stamp = datetime.now().strftime("%H:%M:%S")
        self._replace_project(self._import_project,
                              origin=f"Chocolate Plus · {self.identity.name} · read at {stamp}")
        with self._lock:
            self.status = "Controller assignments loaded into a local working copy. Reading did not change the controller."

    def _controller_write(self, _payload):
        self._require_project()
        self._require_controller()
        if self.plan is None or self._reviewed_image is None or self._reviewed_hash is None:
            raise ValidationError("Preview the current project before writing to the controller.")
        reviewed_image, reviewed_plan, reviewed_hash = self._reviewed_image, self.plan, self._reviewed_hash
        def operation():
            if _fingerprint(self.project, self.profiles) != reviewed_hash:
                raise ValidationError("The project changed since the preview. Preview its current assignments before writing.")
            identity = self.transport.query()
            if identity.firmware_version != 16 or identity.name != self.identity.name:
                raise ValidationError("The connected controller identity changed. Read it again before writing; firmware 16 is required.")
            before = bytes(self.transport.read_config(progress_callback=self._progress))
            if before != reviewed_image:
                with self._lock:
                    self._import_project = None
                raise ValidationError("The controller configuration changed since the preview. Read it again to review its current assignments before writing.")
            plan = plan_project(self.project, self.profiles, before)
            if plan.image != reviewed_plan.image:
                raise ValidationError("The write plan changed since the preview. Preview it again before writing.")
            backup = save_backup(before, identity, self.backup_directory)
            self._progress(f"Backup saved: {backup.name}. Writing project…")
            verified = bytes(self.transport.write_config(before, plan.image, progress_callback=self._progress))
            if verified != plan.image:
                with self._lock:
                    self._import_project = None
                raise ValidationError("The controller did not retain the complete configuration. Keep the backup and read it again.")
            summary, imported, _ = self._inspect(verified, identity)
            with self._lock:
                self.image, self.identity, self.summary, self._import_project = verified, identity, summary, imported
                self._invalidate_preview()
                self.status = f"Project written and readback verified. Backup: {backup.name}"
        self._controller_operation("Reading a fresh backup before writing…", operation)

    def _controller_disconnect(self, _payload):
        def operation():
            if self.transport is not None:
                self.transport.close()
            with self._lock:
                self.transport = self.identity = self.image = self._import_project = None
                self.summary = ""
                self._invalidate_preview()
                self.status = "Disconnected. The project remains available in Stompilot."
        self._controller_operation("Disconnecting controller…", operation)

    def close(self):
        """Refuse to close during work, including an active transfer or picker."""
        if not self._commands.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self.busy:
                    return False
            self._controller_disconnect({})
            self._closed = True
            return True
        finally:
            self._commands.release()
