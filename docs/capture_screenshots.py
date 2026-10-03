r"""Capture the compiled Svelte UI with isolated, read-only documentation fixtures.

Install requirements-docs.txt and Playwright Chromium, then run:
    .venv\Scripts\python.exe docs/capture_screenshots.py

Only a 127.0.0.1 preview server is used. MIDI discovery and transport are simulated;
USB writes always fail. File pickers and zoom preferences use an owned temporary
directory. Screenshots come from the actual compiled frontend, never mockups.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import sys
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import expect, sync_playwright
from docs.preview_server import BRIDGE_SCRIPT, PreviewServer


class BrowserCapture:
    def __init__(self, page, directory):
        self.page, self.directory = page, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.results = []

    def save(self, filename, locator=None):
        target = self.page if locator is None else locator
        image = target.screenshot(path=str(self.directory / filename), animations="disabled", caret="hide")
        width, height = struct.unpack(">II", image[16:24])
        self.results.append({"file": filename, "width": width, "height": height})

    def editor(self, filename):
        modal = self.page.get_by_test_id("action-editor")
        expect(modal).to_be_visible()
        self.save(filename, modal)
        self.page.get_by_role("button", name="Close action editor").click()
        expect(modal).not_to_be_visible()


def capture_all(directory, project_path=None, frontend_directory=None):
    source_path = Path(project_path or PROJECT_ROOT / "examples" / "tank_mini_live_boost.json")
    original_file = source_path.read_bytes()
    with PreviewServer(frontend_directory, source_path) as preview, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
        context.add_init_script(BRIDGE_SCRIPT)
        # Documentation rendering cannot make requests to external sites.
        def local_only(route):
            parsed = urlsplit(route.request.url)
            if parsed.scheme in ("http", "https") and f"{parsed.scheme}://{parsed.netloc}" == preview.url:
                route.continue_()
            else:
                route.abort()
        context.route("**/*", local_only)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(preview.url, wait_until="networkidle")
        expect(page.get_by_test_id("startup-open")).to_be_visible()
        expect(page.get_by_test_id("browser-preview")).to_contain_text("test fixture")
        capture = BrowserCapture(page, directory)
        capture.save("startup.png")

        page.get_by_test_id("startup-open").click()
        expect(page.get_by_test_id("project-name")).to_have_value(preview.example_project["name"])
        page.get_by_test_id("switch-B").click()
        expect(page.get_by_test_id("state-name")).to_have_value("Boost ON / Reverb 20 / Delay 20")
        page.locator(".switch-grid").evaluate("element => element.scrollIntoView({block: 'start'})")
        capture.save("project-editor.png")
        page.get_by_test_id("state-1").click()
        expect(page.get_by_test_id("state-name")).to_have_value("Reload saved preset")
        capture.save("reload-state.png")
        page.get_by_test_id("action-edit-0").click()
        capture.editor("reload-action-editor.png")
        page.get_by_test_id("state-0").click()
        page.get_by_test_id("action-edit-0").click()
        capture.editor("effect-toggle-editor.png")
        page.get_by_test_id("action-edit-1").click()
        expect(page.get_by_test_id("parameter-value")).to_have_value("20")
        capture.editor("parameter-editor.png")

        page.get_by_test_id("nav-profiles").click()
        expect(page.get_by_test_id("profile-detail")).to_be_visible()
        page.locator(".main-content").evaluate("element => element.scrollTop = 0")
        capture.save("device-profiles.png")
        page.get_by_test_id("nav-controller").click()
        page.get_by_test_id("controller-scan").click()
        expect(page.get_by_test_id("controller-device")).to_have_value(preview.device.name)
        page.get_by_test_id("controller-read").click()
        expect(page.get_by_test_id("controller-import")).to_be_enabled()
        page.locator(".main-content").evaluate("element => element.scrollTop = 0")
        capture.save("controller-read.png")
        page.get_by_test_id("controller-import").click()
        expect(page.get_by_test_id("switch-B")).to_be_visible()
        page.get_by_test_id("switch-B").click()
        page.locator(".main-content").evaluate("element => element.scrollTop = 0")
        capture.save("imported-editor.png")
        page.get_by_test_id("action-edit-0").click()
        expect(page.get_by_test_id("raw-hex")).to_be_visible()
        capture.editor("imported-sysex-editor.png")

        # The original use case contains SysEx only. A second simulated read
        # supplements it with one CC in C to show that editor without guessing
        # any device or effect mapping.
        preview.set_fixture_image(preview.cc_fixture_image())
        page.get_by_test_id("nav-controller").click()
        page.get_by_test_id("controller-read").click()
        expect(page.get_by_test_id("controller-import")).to_be_enabled()
        page.get_by_test_id("controller-import").click()
        expect(page.get_by_test_id("switch-C")).to_be_visible()
        page.get_by_test_id("switch-C").click()
        page.get_by_test_id("action-edit-0").click()
        expect(page.get_by_test_id("raw-cc")).to_have_value("2")
        capture.editor("imported-cc-editor.png")

        # Restore the exact real use case before capturing write previews.
        preview.set_fixture_image(preview.original_image)
        page.get_by_test_id("open-project").click()
        expect(page.get_by_test_id("project-name")).to_have_value(preview.example_project["name"])
        page.get_by_test_id("switch-B").click()
        page.get_by_test_id("nav-controller").click()
        page.get_by_test_id("controller-read").click()
        expect(page.get_by_test_id("controller-import")).to_be_enabled()
        page.get_by_test_id("controller-preview").click()
        expect(page.get_by_test_id("write-preview")).to_be_visible()
        page.locator(".main-content").evaluate("element => element.scrollTop = 0")
        capture.save("controller-write-preview.png")
        current = page.locator("section.panel").filter(has=page.get_by_test_id("controller-summary"))
        capture.save("controller-current.png", current)

        page.get_by_test_id("nav-editor").click()
        page.get_by_test_id("switch-B").click()
        page.set_viewport_size({"width": 1600, "height": 1250})
        page.get_by_test_id("zoom-in").click()
        page.get_by_test_id("zoom-in").click()
        expect(page.get_by_test_id("zoom-value")).to_have_text("150%")
        page.locator(".editor-columns").evaluate("element => element.scrollIntoView({block: 'start'})")
        capture.save("zoomed-interface.png")
        page.get_by_test_id("zoom-value").click()
        expect(page.get_by_test_id("zoom-value")).to_have_text("100%")
        assert preview.write_attempts == 0, "The screenshot flow attempted a USB write."
        assert source_path.read_bytes() == original_file, "The source example changed during capture."
        assert not errors, "Frontend errors during capture: " + "; ".join(errors)
        context.close()
        browser.close()
        return sorted(capture.results, key=lambda item: item["file"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs" / "screenshots")
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT / "examples" / "tank_mini_live_boost.json")
    parser.add_argument("--frontend", type=Path, default=PROJECT_ROOT / "frontend" / "build")
    args = parser.parse_args()
    print(json.dumps(capture_all(args.output, args.project, args.frontend), indent=2))


if __name__ == "__main__":
    main()
