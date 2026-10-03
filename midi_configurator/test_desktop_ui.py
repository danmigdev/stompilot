"""Compiled Svelte + real desktop service integration, using owned fixtures.

These tests use Chromium when the optional Playwright installation and a built
frontend are available. All USB requests use DocumentationTransport, and every
file picker and preference write stays in PreviewServer's temporary directory.
"""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit
import unittest

from .chocolate import (BANK_A_ROWS, BANK_A_SYSEX, import_controller_project,
                        plan_project, switch_offset)
from .core import compile_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DesktopUiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (PROJECT_ROOT / "frontend" / "build" / "index.html").is_file():
            raise unittest.SkipTest("Build frontend/build to run compiled desktop UI integration tests.")
        try:
            from playwright.sync_api import Error, expect, sync_playwright
        except ImportError:
            raise unittest.SkipTest("Optional Playwright is not installed.")
        from docs.preview_server import BRIDGE_SCRIPT, PreviewServer
        cls.expect = staticmethod(expect)
        cls.PreviewServer, cls.BRIDGE_SCRIPT = PreviewServer, BRIDGE_SCRIPT
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Error as error:
            cls.playwright.stop()
            raise unittest.SkipTest("Playwright Chromium is unavailable: " + str(error).splitlines()[0])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.server = self.PreviewServer().__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1050})
        self.addCleanup(self.context.close)
        self.context.add_init_script(self.BRIDGE_SCRIPT)

        def local_only(route):
            parsed = urlsplit(route.request.url)
            if parsed.scheme in ("http", "https") and f"{parsed.scheme}://{parsed.netloc}" == self.server.url:
                route.continue_()
            else:
                route.abort()

        self.context.route("**/*", local_only)
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(self.server.url, wait_until="networkidle")
        self.expect(self.page.get_by_test_id("startup-open")).to_be_visible()

    def tearDown(self):
        self.assertEqual(self.page_errors, [], "The compiled frontend raised a JavaScript error")
        self.assertEqual(self.server.write_attempts, 0, "UI tests must never request a simulated USB write")

    def act(self, test_id, method):
        """Wait for the actual service response instead of sleeping for the UI."""
        with self.page.expect_response(lambda response: response.url.endswith("/test-api")
                                      and response.request.post_data_json.get("method") == method) as response:
            self.page.get_by_test_id(test_id).click()
        result = response.value.json()
        self.assertTrue(result["ok"], result)
        return result["data"]

    def assert_hex(self, expected_messages):
        self.expect(self.page.get_by_test_id("hex-preview").locator("code")).to_have_text(expected_messages)

    def test_startup_cancelled_open_and_explicit_empty_project(self):
        state = self.server.service.snapshot()
        self.assertIsNone(state["project"])
        self.assertIsNone(state["compiled"])
        self.assertEqual(state["controller"]["devices"], [])
        self.assertFalse(state["controller"]["connected"])
        self.expect(self.page.get_by_test_id("project-name")).to_have_count(0)
        self.expect(self.page.get_by_test_id("save-project")).to_be_disabled()
        self.server.cancel_pickers = True
        self.assertIsNone(self.act("startup-open", "open_project")["project"])
        self.expect(self.page.get_by_test_id("startup-open")).to_be_visible()
        self.assertEqual(self.server.service.snapshot(), state)
        self.server.cancel_pickers = False
        created = self.act("startup-new", "new_project")
        self.expect(self.page.get_by_test_id("project-name")).to_have_value("New project")
        self.assertFalse(created["dirty"])
        self.assertEqual(created["origin"], "New empty project")
        for switch in created["project"]["pages"][0]["switches"].values():
            self.assertEqual(switch["mode"], "single")
            self.assertEqual(switch["states"][0]["actions"], [])
        self.expect(self.page.get_by_test_id("hex-preview")).to_contain_text("Messages appear here")
        self.assertEqual(created["compiled"], compile_config(created["project"], self.server.service.profiles))
        self.assertEqual(created["controller"]["devices"], [])

    def test_open_edit_hall_mix_exact_compilation_cancel_save_as_then_save_and_export(self):
        source_bytes = self.server.project_path.read_bytes()
        opened = self.act("startup-open", "open_project")
        self.assertEqual(opened["project"], self.server.example_project)
        self.expect(self.page.get_by_test_id("project-name")).to_have_value(self.server.example_project["name"])
        self.page.get_by_test_id("switch-B").click()
        expected_project = deepcopy(self.server.example_project)
        original_compiled = compile_config(expected_project, self.server.service.profiles)
        self.assert_hex(original_compiled["pages"][0]["switches"]["B"]["states"][0]["messages_hex"])
        self.page.get_by_test_id("action-edit-1").click()
        self.expect(self.page.get_by_test_id("action-model")).to_have_value("1")
        self.expect(self.page.get_by_test_id("action-parameter")).to_have_value("mix")
        self.expect(self.page.get_by_test_id("parameter-value")).to_have_value("20")
        self.page.get_by_test_id("parameter-value").fill("21")
        edited = self.act("action-save", "set_project")
        self.expect(self.page.get_by_test_id("action-editor")).to_be_hidden()
        expected_project["pages"][0]["switches"]["B"]["states"][0]["actions"][1]["value"] = 21
        expected_compiled = compile_config(expected_project, self.server.service.profiles)
        self.assertEqual(edited["project"], expected_project)
        self.assertEqual(edited["compiled"], expected_compiled)
        messages = expected_compiled["pages"][0]["switches"]["B"]["states"][0]["messages_hex"]
        self.assertNotEqual(messages[1], original_compiled["pages"][0]["switches"]["B"]["states"][0]["messages_hex"][1])
        self.assert_hex(messages)
        self.assertTrue(edited["dirty"])
        self.expect(self.page.locator(".topbar-project")).to_contain_text("Unsaved changes")
        self.server.cancel_pickers = True
        cancelled = self.act("save-as", "save_project")
        self.assertEqual(cancelled["project"], expected_project)
        self.assertTrue(cancelled["dirty"])
        self.assertEqual(json.loads(self.server.open_project_path.read_text(encoding="utf-8")), self.server.example_project)
        self.server.cancel_pickers = False
        saved = self.act("save-project", "save_project")
        self.expect(self.page.locator(".topbar-project")).to_contain_text("Saved project")
        self.assertFalse(saved["dirty"])
        self.assertEqual(json.loads(self.server.open_project_path.read_text(encoding="utf-8")), expected_project)
        self.assertEqual(self.server.project_path.read_bytes(), source_bytes, "Saving must only affect the owned example copy")
        self.assert_hex(messages)
        exported = self.act("export-messages", "export_messages")
        self.assertEqual(exported["project"], expected_project)
        output_path = self.server.data_directory / "exports" / "stompilot-midi.txt"
        output = output_path.read_text(encoding="utf-8")
        self.assertEqual(output, self.server.service.export_text())
        self.assertIn(messages[1], output)
        self.assertIn("Bank B: Reload saved preset", output)

    def test_controller_read_import_keeps_raw_cc_sysex_native_banks_and_preview_has_zero_changes(self):
        image = bytearray(self.server.cc_fixture_image())
        offset = switch_offset(0, "C")
        # Disabled row data and unused SysEx storage must survive a native CC import.
        image[offset + BANK_A_ROWS + 5:offset + BANK_A_ROWS + 10] = b"\x00\xFE\xFD\xFC\xFB"
        image[offset + BANK_A_SYSEX:offset + BANK_A_SYSEX + 128] = bytes([0xA6]) * 128
        image = bytes(image)
        self.server.set_fixture_image(image)
        scanned = self.act("startup-read", "controller_scan")
        self.expect(self.page.get_by_test_id("controller-device")).to_have_value(self.server.device.name)
        self.assertIsNone(scanned["project"])
        read = self.act("controller-read", "controller_read")
        self.expect(self.page.get_by_test_id("controller-import")).to_be_enabled()
        self.expect(self.page.get_by_test_id("controller-summary")).to_contain_text("CC 2 = 0")
        self.assertTrue(read["controller"]["can_import"])
        self.assertFalse(read["controller"]["can_write"])
        self.assertIsNone(read["project"])
        imported = self.act("controller-import", "controller_import")
        expected_project = import_controller_project(image, self.server.service.profiles)
        self.expect(self.page.get_by_test_id("project-name")).to_have_value("Chocolate Plus configuration")
        self.assertEqual(imported["project"], expected_project)
        self.assertFalse(imported["dirty"])
        self.assertIn("read at", imported["origin"])
        expected_compiled = compile_config(expected_project, self.server.service.profiles)
        self.page.get_by_test_id("switch-B").click()
        sysex = expected_compiled["pages"][0]["switches"]["B"]["states"][0]["messages_hex"]
        self.assert_hex(sysex)
        self.page.get_by_test_id("action-edit-1").click()
        self.expect(self.page.get_by_test_id("raw-hex")).to_have_value(sysex[1])
        self.page.get_by_role("button", name="Close action editor").click()
        self.page.get_by_test_id("switch-C").click()
        self.assert_hex(["B0 02 00"])
        self.page.get_by_test_id("action-edit-0").click()
        self.expect(self.page.get_by_test_id("raw-cc")).to_have_value("2")
        self.expect(self.page.get_by_test_id("raw-value")).to_have_value("0")
        self.page.get_by_role("button", name="Close action editor").click()
        self.assertEqual(self.server.service.snapshot()["project"], expected_project)
        saved = self.act("save-project", "save_project")
        saved_path = self.server.data_directory / "exports" / "stompilot.json"
        self.assertEqual(json.loads(saved_path.read_text(encoding="utf-8")), expected_project)
        self.assertFalse(saved["dirty"])
        self.page.get_by_test_id("nav-controller").click()
        reviewed = self.act("controller-preview", "controller_preview")
        self.expect(self.page.get_by_test_id("write-preview")).to_be_visible()
        self.expect(self.page.locator(".write-panel")).to_contain_text("0 changed bytes")
        self.assertEqual(reviewed["controller"]["preview"]["changed_bytes"], 0)
        self.assertEqual(plan_project(expected_project, self.server.service.profiles, image).image, image)
        self.assertEqual(self.server.fixture_image, image)
        self.assertEqual(self.server.write_attempts, 0)
        self.assertFalse((self.server.data_directory / ".backups").exists())

    def test_profile_import_selection_and_export_use_the_real_portable_profile_schema(self):
        profile = json.loads((PROJECT_ROOT / "examples" / "generic_cc_profile.json").read_text(encoding="utf-8"))
        profile.update(id="ui_variant", name="UI integration variant")
        (self.server.data_directory / "profile-to-import.json").write_text(json.dumps(profile), encoding="utf-8")
        self.page.get_by_test_id("nav-profiles").click()
        imported = self.act("profile-import", "import_profile")
        self.assertIn(profile, imported["profiles"])
        self.page.get_by_test_id("profile-ui_variant").click()
        self.expect(self.page.get_by_test_id("profile-detail")).to_contain_text(profile["name"])
        self.act("profile-export", "export_profile")
        custom_path = self.server.data_directory / "profiles" / "ui_variant.json"
        exported_path = self.server.data_directory / "exports" / "ui_variant.json"
        self.assertEqual(json.loads(custom_path.read_text(encoding="utf-8")), profile)
        self.assertEqual(json.loads(exported_path.read_text(encoding="utf-8")), profile)
        self.assertIsNone(self.server.service.snapshot()["project"])


if __name__ == "__main__":
    unittest.main()
