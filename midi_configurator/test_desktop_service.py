"""Desktop API regression tests; all devices, pickers, and writes are simulated."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import desktop_service
from .chocolate import BANK_A_ROWS, CONFIG_SIZE, legacy_sysex_banks, plan_project, switch_offset
from .core import compile_config
from .project import new_project
from .test_chocolate import legacy_fixture, readable_fixture
from .test_core import cc_fixture, config_fixture, tank_fixture


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.profiles = {"tank": tank_fixture(), "generic": cc_fixture()}
        self.choices, self.picker_calls, self.usb_calls = [], [], []
        self.device = SimpleNamespace(name="FootCtrlPlus", input_name="FootCtrlPlus 0", output_name="FootCtrlPlus 1")
        self.identity = SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16)
        self.image = readable_fixture()
        self.transport = None
        owner = self

        class FakeTransport:
            def __init__(self, input_name, output_name):
                owner.usb_calls.append(("factory", input_name, output_name))
                self.image = owner.image
                self.identity = owner.identity
                self.fail_read = self.fail_write = self.fail_query = False
                self.wrong_verification = False
                self.read_started = self.release_read = None
                self.write_started = self.release_write = None
                owner.transport = self

            def open(self):
                owner.usb_calls.append("open")

            def close(self):
                owner.usb_calls.append("close")

            def query(self):
                owner.usb_calls.append("query")
                if self.fail_query:
                    raise RuntimeError("The connected device returned an invalid identity.")
                return self.identity

            def read_config(self, progress_callback=None):
                owner.usb_calls.append("read")
                if progress_callback:
                    progress_callback(512, CONFIG_SIZE)
                if self.read_started is not None:
                    self.read_started.set()
                    if not self.release_read.wait(3):
                        raise RuntimeError("The simulated read was not released.")
                if self.fail_read:
                    raise RuntimeError("Chocolate Plus did not answer command 0x23.")
                return self.image

            def write_config(self, before, after, progress_callback=None):
                backups = list((owner.directory / ".backups").glob("*.json"))
                owner.assertTrue(backups, "The exact durable backup must exist before writing")
                backup = json.loads(backups[-1].read_text(encoding="utf-8"))
                owner.assertEqual(bytes.fromhex(backup["image_hex"]), before)
                owner.assertEqual(backup["sha256"], hashlib.sha256(before).hexdigest())
                owner.assertEqual(backup["image_size"], CONFIG_SIZE)
                owner.usb_calls.append(("write", before, after))
                if self.write_started is not None:
                    self.write_started.set()
                    if not self.release_write.wait(3):
                        raise RuntimeError("The simulated write was not released.")
                if self.fail_write:
                    raise RuntimeError("Chocolate Plus refused a configuration write. Keep the backup.")
                self.image = after
                if progress_callback:
                    progress_callback(sum(item.length for item in desktop_service.plan_project(
                        owner.service.project, owner.service.profiles, before).ranges), 100)
                return before if self.wrong_verification else after

        def picker(kind, suggested_name="", file_types=()):
            self.picker_calls.append((kind, suggested_name, file_types))
            return self.choices.pop(0) if self.choices else None

        def discovery():
            self.usb_calls.append("scan")
            return [self.device]

        self.service = desktop_service.DesktopService(
            profiles=self.profiles, data_directory=self.directory,
            transport_factory=FakeTransport, discovery=discovery, dialog=picker)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def request(self, method, payload=None):
        result = self.service.request(method, payload)
        self.assertTrue(result["ok"], result)
        return result["data"]

    def error(self, method, payload=None, contains=None):
        result = self.service.request(method, payload)
        self.assertFalse(result["ok"], result)
        if contains is not None:
            self.assertIn(contains, result["error"]["message"])
        return result["error"]["message"]

    def install_project(self):
        self.request("new_project")
        return self.request("set_project", {"project": config_fixture()})

    def read(self):
        self.request("controller_scan")
        return self.request("controller_read", {"device_name": self.device.name})

    def preview(self):
        self.install_project()
        self.read()
        return self.request("controller_preview")

    def test_startup_is_empty_offline_and_snapshots_do_not_share_state(self):
        state = self.request("bootstrap")
        self.assertIsNone(state["project"])
        self.assertIsNone(state["compiled"])
        self.assertEqual(state["controller"]["devices"], [])
        self.assertFalse(state["controller"]["connected"])
        self.assertEqual(self.usb_calls, [])
        self.assertEqual(self.picker_calls, [])
        state["profiles"][0]["name"] = "Changed client copy"
        self.assertEqual(self.request("bootstrap")["profiles"][0]["name"], "TANK Mini V09")

    def test_startup_ignores_previous_project_files_and_zoom_fallback_is_safe(self):
        (self.directory / "stompilot.json").write_text(json.dumps(config_fixture()), encoding="utf-8")
        settings = self.directory / "settings.json"
        for content in ("{", "[]", '{"ui_zoom_percent":true}', '{"ui_zoom_percent":201}'):
            settings.write_text(content, encoding="utf-8")
            loaded = desktop_service.DesktopService(
                profiles=self.profiles, data_directory=self.directory,
                discovery=lambda: self.fail("Startup must not discover MIDI devices"),
                transport_factory=lambda *_: self.fail("Startup must not open MIDI ports"))
            self.assertIsNone(loaded.snapshot()["project"])
            self.assertEqual(loaded.snapshot()["zoom_percent"], 100)
            loaded.close()

    def test_import_is_tk_free_in_package_and_direct_modes(self):
        directory = Path(desktop_service.__file__).parent
        scripts = ["import midi_configurator.desktop_service", "import sys; sys.path.insert(0, " + repr(str(directory)) + "); import desktop_service"]
        for script in scripts:
            script += "; import sys; assert 'tkinter' not in sys.modules; assert 'mido' not in sys.modules"
            result = subprocess.run([sys.executable, "-c", script], cwd=directory.parent,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_project_is_clean_and_all_previews_use_core_compiler(self):
        state = self.request("new_project")
        self.assertEqual(state["project"], new_project(example=False))
        self.assertEqual(state["origin"], "New empty project")
        self.assertFalse(state["dirty"])
        self.assertEqual(state["compiled"], compile_config(state["project"], self.profiles))

    def test_noop_edit_preserves_preview_and_reverting_to_clean_clears_dirty(self):
        state = self.request("new_project")
        clean = state["project"]
        edited = deepcopy(clean)
        edited["name"] = "Renamed"
        state = self.request("set_project", {"project": edited})
        self.assertTrue(state["dirty"])
        self.assertEqual(state["origin"], "New empty project")
        self.read()
        reviewed = self.request("controller_preview")["controller"]["preview"]
        self.assertEqual(self.request("set_project", {"project": edited})["controller"]["preview"], reviewed)
        self.assertFalse(self.request("set_project", {"project": clean})["dirty"])

    def test_set_validates_entire_project_and_failed_action_preserves_valid_state(self):
        previous = self.install_project()
        invalid = deepcopy(previous["project"])
        invalid["pages"][0]["switches"]["D"]["states"][0]["actions"] = [{"type": "raw_midi", "message_hex": "C0 00"}]
        self.error("set_project", {"project": invalid}, "switch D")
        self.assertEqual(self.request("bootstrap"), previous)
        invalid = deepcopy(previous["project"])
        invalid["pages"][0]["switches"]["A"]["states"][0]["native_bank"] = {"rows_hex": "00", "sysex_hex": "00"}
        self.error("set_project", {"project": invalid}, "80 bytes")
        self.assertEqual(self.request("bootstrap"), previous)

    def test_cancelled_file_operations_leave_state_unchanged(self):
        previous = self.install_project()
        for method in ("open_project", "save_project", "export_messages", "import_profile"):
            self.assertEqual(self.request(method), previous)
        self.assertEqual(self.request("export_profile", {"profile_id": "generic"}), previous)

    def test_invalid_open_keeps_working_copy_and_file_errors_hide_absolute_paths(self):
        previous = self.install_project()
        path = self.directory / "invalid-project.json"
        path.write_text('{"schema_version": 99}', encoding="utf-8")
        self.choices.append(path)
        self.error("open_project", contains="only version 1")
        self.assertEqual(self.request("bootstrap"), previous)
        self.choices.append(self.directory / "missing-project.json")
        message = self.error("open_project")
        self.assertIn("missing-project.json", message)
        self.assertNotIn(str(self.directory), message)

    def test_open_normalizes_optional_fields_preserves_metadata_and_saves_same_schema(self):
        source = config_fixture()
        source["notes"] = {"owner": "A musician"}
        del source["controller"]
        del source["pages"][0]["switches"]["D"]
        path = self.directory / "loaded.json"
        path.write_text("\ufeff" + json.dumps(source), encoding="utf-8")
        self.choices.append(path)
        state = self.request("open_project")
        self.assertEqual(state["file_name"], "loaded.json")
        self.assertEqual(state["origin"], "Saved project · loaded.json")
        self.assertEqual(state["project"]["notes"], source["notes"])
        self.assertIn("D", state["project"]["pages"][0]["switches"])
        self.assertEqual(state["project"]["controller"]["max_sysex_bytes"], 127)
        state["project"]["name"] = "Changed"
        self.request("set_project", {"project": state["project"]})
        saved = self.request("save_project")
        self.assertFalse(saved["dirty"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), saved["project"])
        self.assertEqual(len(self.picker_calls), 1)

    def test_save_as_cancel_and_failed_atomic_replace_keep_previous_state_and_file(self):
        self.install_project()
        path = self.directory / "saved.json"
        self.choices.append(path)
        self.request("save_project")
        state = self.request("bootstrap")
        state["project"]["name"] = "Unsaved"
        before = self.request("set_project", {"project": state["project"]})
        self.assertEqual(self.request("save_project", {"save_as": True}), before)
        original_bytes = path.read_bytes()
        with patch.object(desktop_service.os, "replace", side_effect=OSError("Disk is full")):
            self.error("save_project", contains="Disk is full")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.request("bootstrap"), before)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_export_messages_preserves_banks_and_cc_sysex_constraints(self):
        self.install_project()
        path = self.directory / "messages.txt"
        self.choices.append(path)
        before = self.request("bootstrap")
        self.assertEqual(self.request("export_messages"), before)
        output = path.read_text(encoding="utf-8")
        self.assertIn("Set Max Group Count = 1.", output)
        self.assertIn("Bank B: OFF / Mix 5", output)
        self.assertIn("Paste all lines into the same field", output)
        project = before["project"]
        actions = project["pages"][0]["switches"]["A"]["states"][0]["actions"]
        actions.append({"type": "raw_midi", "message_hex": "B0 12 7F"})
        self.error("set_project", {"project": project}, "CC and SysEx")

    def test_profile_import_is_durable_reloaded_duplicate_safe_and_exports_same_schema(self):
        profile = cc_fixture()
        profile.update(id="custom_variant", name="Custom variant")
        source = self.directory / "variant.json"
        source.write_text(json.dumps(profile), encoding="utf-8")
        self.choices.append(source)
        state = self.request("import_profile")
        target = self.directory / "profiles" / "custom_variant.json"
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), profile)
        self.assertEqual(state["profiles"][-1], profile)
        loaded = desktop_service.DesktopService(profiles=self.profiles, data_directory=self.directory)
        self.assertEqual(loaded.snapshot()["profiles"][-1], profile)
        loaded.close()
        self.choices.append(source)
        self.error("import_profile", contains="already exists")
        self.assertEqual(self.request("bootstrap"), state)
        exported = self.directory / "exported.json"
        self.choices.append(exported)
        self.request("export_profile", {"profile_id": profile["id"]})
        self.assertEqual(json.loads(exported.read_text(encoding="utf-8")), profile)

    def test_default_source_paths_keep_imports_separate_from_bundled_defaults(self):
        source = self.directory / "variant.json"
        profile = cc_fixture()
        profile.update(id="portable_variant", name="Portable variant")
        source.write_text(json.dumps(profile), encoding="utf-8")
        self.choices.append(source)
        with patch.object(desktop_service, "default_data_directory", return_value=self.directory), \
                patch.object(desktop_service, "default_profile_directory", return_value=self.directory / "profiles"):
            loaded = desktop_service.DesktopService(dialog=self.service.dialog)
            result = loaded.request("import_profile")
            self.assertTrue(result["ok"], result)
            self.assertIn(profile, result["data"]["profiles"])
            self.assertTrue((self.directory / "profiles" / "portable_variant.json").is_file())
            self.assertFalse((loaded._bundled_directory / "portable_variant.json").exists())
            loaded.close()

    def test_profile_id_traversal_and_duplicate_name_are_rejected_without_files(self):
        path = self.directory / "unsafe.json"
        for profile_id, name, expected in [("../outside", "Variant", "profile ID"), ("other", "Generic CC", "already exists")]:
            profile = cc_fixture()
            profile.update(id=profile_id, name=name)
            path.write_text(json.dumps(profile), encoding="utf-8")
            self.choices.append(path)
            self.error("import_profile", contains=expected)
        self.assertFalse((self.directory / "profiles").exists())

    def test_bundled_profile_export_cannot_overwrite_defaults(self):
        bundled_file = next((Path(desktop_service.__file__).parent / "profiles").glob("*.json"))
        before = bundled_file.read_bytes()
        self.choices.append(bundled_file)
        self.error("export_profile", {"profile_id": "generic"}, "read-only")
        self.assertEqual(bundled_file.read_bytes(), before)

    def test_zoom_reads_existing_preferences_preserves_other_keys_and_does_not_dirty(self):
        settings = self.directory / "settings.json"
        settings.write_text(json.dumps({"ui_zoom_percent": 125, "other_setting": {"value": 3}}), encoding="utf-8")
        loaded = desktop_service.DesktopService(profiles=self.profiles, data_directory=self.directory)
        self.assertEqual(loaded.snapshot()["zoom_percent"], 125)
        loaded.close()
        self.request("new_project")
        self.assertFalse(self.request("set_zoom", {"percent": 175})["dirty"])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"ui_zoom_percent": 175, "other_setting": {"value": 3}})
        for value in (74, 201, True, 100.0, "150"):
            self.error("set_zoom", {"percent": value}, "75 to 200")
        with patch.object(desktop_service, "_atomic_text", side_effect=PermissionError("Read-only drive")):
            self.assertEqual(self.request("set_zoom", {"percent": 200})["zoom_percent"], 200)

    def test_read_is_nonmutating_and_import_preserves_exact_native_banks(self):
        state = self.read()
        self.assertTrue(state["controller"]["can_import"])
        self.assertFalse(state["controller"]["can_write"])
        self.assertIsNone(state["project"])
        self.assertIsNone(state["controller"]["preview"])
        self.assertIn("Advanced Custom", state["controller"]["summary"])
        self.assertEqual(self.transport.image, self.image)
        imported = self.request("controller_import")
        self.assertFalse(imported["dirty"])
        self.assertIn("read at", imported["origin"])
        self.assertIn("native_bank", imported["project"]["pages"][0]["switches"]["A"]["states"][0])
        self.assertEqual(plan_project(imported["project"], self.profiles, self.image).image, self.image)
        path = self.directory / "native.json"
        self.choices.append(path)
        self.request("save_project")
        self.choices.append(path)
        reopened = self.request("open_project")
        self.assertEqual(reopened["project"], imported["project"])
        self.assertEqual(plan_project(reopened["project"], self.profiles, self.image).image, self.image)
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "write" for call in self.usb_calls))
        self.assertFalse((self.directory / ".backups").exists())

    def test_read_flags_old_stompilot_sysex_and_import_preview_write_repairs_it(self):
        self.image = legacy_fixture()
        state = self.read()
        status = state["controller"]["status"]
        self.assertIn("3 SysEx bank(s) were written by an earlier Stompilot", status)
        self.assertIn("Page 1, switch A, bank B", status)
        self.assertIn("Import as project, then preview and write", status)
        self.assertIn("old Stompilot format", state["controller"]["summary"])
        self.request("controller_import")
        preview = self.request("controller_preview")["controller"]["preview"]
        self.assertEqual(sum("old Stompilot format" in line for line in preview["summaries"]), 3)
        self.assertGreater(preview["changed_bytes"], 0)
        result = self.request("controller_write")
        self.assertIn("readback verified", result["controller"]["status"])
        self.assertEqual(legacy_sysex_banks(self.transport.image), [])
        self.assertNotIn("earlier Stompilot", result["controller"]["summary"])

    def test_old_sysex_on_unsupported_firmware_is_reported_without_a_repair_path(self):
        self.identity = SimpleNamespace(name="FootCtrlPlus_013", firmware_version=13)
        self.image = legacy_fixture()
        status = self.read()["controller"]["status"]
        self.assertIn("written by an earlier Stompilot", status)
        self.assertNotIn("Import as project", status)
        self.assertIn("firmware 13", status)

    def test_unsupported_firmware_is_inspect_only(self):
        self.identity = SimpleNamespace(name="FootCtrlPlus_015", firmware_version=15)
        self.install_project()
        state = self.read()
        self.assertTrue(state["controller"]["connected"])
        self.assertFalse(state["controller"]["can_import"])
        self.assertFalse(state["controller"]["can_write"])
        self.assertIn("firmware 15", state["controller"]["status"])
        self.assertIn("Page 1", state["controller"]["summary"])
        previous = state["project"]
        self.error("controller_import", contains="firmware 16")
        self.error("controller_preview", contains="firmware 16")
        self.error("controller_write", contains="firmware 16")
        self.assertEqual(self.request("bootstrap")["project"], previous)

    def test_unsupported_native_assignment_is_inspect_only_for_import(self):
        image = bytearray(self.image)
        image[switch_offset(0, "A") + BANK_A_ROWS: switch_offset(0, "A") + BANK_A_ROWS + 5] = b"\x01\x00\x00\x20\x00"
        self.image = bytes(image)
        state = self.read()
        self.assertFalse(state["controller"]["can_import"])
        self.assertIn("cannot be imported", state["controller"]["status"])
        self.assertIn("Page 1", state["controller"]["summary"])
        self.error("controller_import", contains="inspection")
        self.assertIsNone(self.request("bootstrap")["project"])

    def test_project_edit_invalidates_review_and_read_requires_explicit_preview(self):
        state = self.preview()
        self.assertTrue(state["controller"]["can_write"])
        state["project"]["name"] = "Edited after preview"
        changed = self.request("set_project", {"project": state["project"]})
        self.assertIsNone(changed["controller"]["preview"])
        self.assertFalse(changed["controller"]["can_write"])
        self.error("controller_write", contains="Preview")
        self.assertTrue(self.request("controller_preview")["controller"]["can_write"])
        reread = self.request("controller_read")
        self.assertIsNone(reread["controller"]["preview"])

    def test_fresh_changed_image_or_identity_aborts_before_backup_or_write(self):
        self.preview()
        image = bytearray(self.image)
        image[2] ^= 1
        self.transport.image = bytes(image)
        self.error("controller_write", contains="changed since the preview")
        self.assertFalse((self.directory / ".backups").exists())
        self.assertFalse(self.request("bootstrap")["controller"]["can_write"])
        self.assertFalse(self.request("bootstrap")["controller"]["can_import"])
        self.request("controller_read")
        self.request("controller_preview")
        self.transport.identity = SimpleNamespace(name="FootCtrlPlus_017", firmware_version=17)
        self.error("controller_write", contains="identity changed")
        self.assertFalse((self.directory / ".backups").exists())

    def test_backup_failure_prevents_all_writes_and_keeps_project(self):
        before = self.preview()["project"]
        with patch.object(desktop_service, "save_backup", side_effect=OSError("Disk is full")):
            self.error("controller_write", contains="Disk is full")
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "write" for call in self.usb_calls))
        self.assertEqual(self.request("bootstrap")["project"], before)

    def test_backup_fsync_failure_prevents_write(self):
        self.preview()
        with patch.object(desktop_service.os, "fsync", side_effect=OSError("Cannot flush backup")):
            self.error("controller_write", contains="Cannot flush")
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "write" for call in self.usb_calls))
        self.assertEqual(list((self.directory / ".backups").glob("*.json")), [])

    def test_write_passes_exact_original_and_plan_to_sparse_verified_transport(self):
        state = self.preview()
        before, expected = self.image, plan_project(state["project"], self.profiles, self.image)
        self.assertEqual(state["controller"]["preview"]["changed_bytes"], expected.changed_bytes)
        result = self.request("controller_write")
        written = [call for call in self.usb_calls if isinstance(call, tuple) and call[0] == "write"]
        self.assertEqual(written, [("write", before, expected.image)])
        self.assertEqual(self.transport.image, expected.image)
        self.assertIn("readback verified", result["controller"]["status"])
        self.assertEqual(result["project"], state["project"])
        self.assertEqual(result["dirty"], state["dirty"])
        self.assertFalse(result["controller"]["can_write"])
        self.assertIsNone(result["controller"]["preview"])

    def test_transport_write_error_and_bad_readback_keep_backup_and_disable_stale_import(self):
        for failure in ("fail_write", "wrong_verification"):
            with self.subTest(failure=failure):
                self.preview()
                setattr(self.transport, failure, True)
                self.error("controller_write", contains="backup")
                self.assertTrue(list((self.directory / ".backups").glob("*.json")))
                state = self.request("bootstrap")
                self.assertFalse(state["controller"]["busy"])
                self.assertFalse(state["controller"]["can_import"])
                self.assertFalse(state["controller"]["can_write"])
                self.request("controller_disconnect")

    def test_failed_first_read_closes_ports_and_failed_reread_keeps_project(self):
        self.request("controller_scan")
        with patch.object(desktop_service, "config_summary", side_effect=ValueError("Invalid controller image")):
            self.error("controller_read", {"device_name": self.device.name}, "Invalid")
        self.assertIn("close", self.usb_calls)
        self.assertFalse(self.request("bootstrap")["controller"]["connected"])
        previous = self.install_project()["project"]
        self.request("controller_read", {"device_name": self.device.name})
        self.transport.fail_read = True
        self.error("controller_read", contains="did not answer")
        self.assertEqual(self.request("bootstrap")["project"], previous)
        self.assertFalse(self.request("bootstrap")["controller"]["can_import"])

    def test_busy_guard_and_progress_polling_do_not_wait_for_usb_io(self):
        self.preview()
        self.transport.read_started = threading.Event()
        self.transport.release_read = threading.Event()
        responses = []
        thread = threading.Thread(target=lambda: responses.append(self.service.request("controller_read")))
        thread.start()
        try:
            self.assertTrue(self.transport.read_started.wait(1))
            start = time.monotonic()
            state = self.request("bootstrap")
            self.assertLess(time.monotonic() - start, 0.25)
            self.assertTrue(state["controller"]["busy"])
            self.assertIn("512 / 23,646 bytes", state["controller"]["status"])
            self.assertFalse(state["controller"]["can_import"])
            self.assertFalse(state["controller"]["can_write"])
            self.assertTrue(self.service.is_busy)
            self.assertFalse(self.service.can_close)
            self.assertFalse(self.service.close())
            for method in ("controller_scan", "controller_write", "controller_disconnect", "new_project"):
                self.error(method, contains="in progress")
        finally:
            self.transport.release_read.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(responses[0]["ok"], responses)
        self.assertFalse(self.service.is_busy)
        self.assertTrue(self.service.can_close)

    def test_zoom_during_a_usb_transfer_changes_only_ui_preferences(self):
        before = self.preview()
        self.transport.read_started = threading.Event()
        self.transport.release_read = threading.Event()
        replies = []
        worker = threading.Thread(target=lambda: replies.append(self.service.request("controller_read")))
        worker.start()
        try:
            self.assertTrue(self.transport.read_started.wait(1))
            calls = list(self.usb_calls)
            zoomed = self.request("set_zoom", {"percent": 175})
            self.assertEqual(zoomed["zoom_percent"], 175)
            self.assertTrue(zoomed["controller"]["busy"])
            self.assertEqual(zoomed["project"], before["project"])
            self.assertEqual(zoomed["dirty"], before["dirty"])
            self.assertEqual(self.usb_calls, calls)
            self.assertEqual(json.loads((self.directory / "settings.json").read_text())["ui_zoom_percent"], 175)
        finally:
            self.transport.release_read.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(replies[0]["ok"], replies)

    def test_close_disconnects_without_midi_writes_and_malformed_requests_are_portable(self):
        self.read()
        self.assertTrue(self.service.close())
        self.assertIn("close", self.usb_calls)
        self.assertFalse(self.request("bootstrap")["controller"]["connected"])
        self.error("new_project", contains="closed")
        self.error("arbitrary_method", contains="Unknown")
        self.assertFalse(self.service.request([], {})["ok"])
        self.assertFalse(self.service.request("bootstrap", []) ["ok"])


if __name__ == "__main__":
    unittest.main()
