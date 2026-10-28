"""Loopback-only documentation preview of the compiled Svelte desktop UI.

All files and preferences live in an owned temporary directory. USB discovery
and transport are fixtures; no hardware MIDI ports are opened. The real desktop
service supplies the same snapshots and validation used by the application.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from midi_configurator.chocolate import CONFIG_SIZE, plan_project
from midi_configurator.chocolate_transport import UsbDevice
from midi_configurator.desktop_service import DesktopService


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DocumentationTransport:
    """A simulated read-only Chocolate Plus; writes always fail."""

    def __init__(self, owner):
        self.owner = owner

    def open(self):
        return self

    def close(self):
        pass

    def query(self):
        return SimpleNamespace(name="FootCtrlPlus_016", firmware_version=16)

    def read_config(self, progress_callback=None):
        image = bytes(self.owner.fixture_image)
        if progress_callback:
            progress_callback(len(image), len(image))
        return image

    def write_config(self, *_args, **_kwargs):
        self.owner.write_attempts += 1
        raise RuntimeError("Documentation simulation is read-only. USB writing is prohibited.")


class PreviewServer(AbstractContextManager):
    """Serve frontend/build and an isolated DesktopService on 127.0.0.1.

    ``service`` remains available for integration-test assertions. File pickers
    return only owned fixture paths, so tests cannot overwrite source examples.
    ``cancel_pickers`` makes every picker return None without changing state.
    """

    def __init__(self, frontend_directory=None, project_path=None):
        self.frontend_directory = Path(frontend_directory or PROJECT_ROOT / "frontend" / "build").resolve()
        if not (self.frontend_directory / "index.html").is_file():
            raise FileNotFoundError("Build the Svelte frontend before starting the documentation preview.")
        self.project_path = Path(project_path or PROJECT_ROOT / "examples" / "tank_mini_live_boost.json")
        self.example_project = json.loads(self.project_path.read_text(encoding="utf-8"))
        self.temporary = tempfile.TemporaryDirectory(prefix="stompilot-docs-")
        self.data_directory = Path(self.temporary.name).resolve()
        # Validate the owned cleanup target before TemporaryDirectory removes it.
        if not self.data_directory.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not self.data_directory.name.startswith("stompilot-docs-"):
            raise RuntimeError("Unexpected documentation temporary directory.")
        self.cancel_pickers = False
        self.write_attempts = 0
        self.open_project_path = self.data_directory / self.project_path.name
        self.open_project_path.write_text(json.dumps(self.example_project, indent=2) + "\n", encoding="utf-8")
        self.device = UsbDevice("Chocolate Plus (documentation fixture)", "Documentation input", "Documentation output")
        self.service = DesktopService(
            profiles=None, data_directory=self.data_directory,
            discovery=lambda: [self.device],
            transport_factory=lambda *_: DocumentationTransport(self),
            dialog=self.pick_file,
        )
        self.fixture_image = plan_project(self.example_project, self.service.profiles, bytes(CONFIG_SIZE)).image
        self.original_image = self.fixture_image
        self.httpd = self.thread = None

    def pick_file(self, kind, suggested_name="", file_types=()):
        if self.cancel_pickers:
            return None
        if kind == "open_project":
            return self.open_project_path
        if kind == "import_profile":
            # The fixture picker is cancelled unless a test supplies a file.
            candidate = self.data_directory / "profile-to-import.json"
            return candidate if candidate.is_file() else None
        names = {"save_project": "stompilot.json", "export_messages": "stompilot-midi.txt",
                 "export_profile": "tank_mini_v09.json"}
        if kind not in names:
            raise ValueError("Unknown documentation picker.")
        name = Path(suggested_name).name if suggested_name else names[kind]
        return self.data_directory / "exports" / name

    def set_fixture_image(self, image):
        if self.service.is_busy:
            raise RuntimeError("Wait for the fixture operation to finish before replacing its image.")
        image = bytes(image)
        if len(image) != CONFIG_SIZE:
            raise ValueError("The documentation fixture requires a complete controller image.")
        self.fixture_image = image

    def raw_fixture_image(self):
        """Supplement the real use case with one CC and one SysEx that no profile sends."""
        project = deepcopy(self.example_project)
        project["pages"][0]["switches"]["C"]["states"][0]["actions"] = [
            {"type": "raw_midi", "message_hex": "B0 02 00"}
        ]
        project["pages"][0]["switches"]["D"]["states"][0]["actions"] = [
            {"type": "raw_midi", "message_hex": "F0 7D 01 02 03 F7"}
        ]
        return plan_project(project, self.service.profiles, bytes(CONFIG_SIZE)).image

    @property
    def url(self):
        if self.httpd is None:
            raise RuntimeError("The documentation server has not started.")
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self):
        owner = self

        class Handler(SimpleHTTPRequestHandler):
            extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                              ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(owner.frontend_directory), **kwargs)

            def log_message(self, *_):
                pass

            def _local_request(self):
                return self.headers.get("Host") == f"127.0.0.1:{owner.httpd.server_port}"

            def do_GET(self):
                if not self._local_request():
                    self.send_error(403, "Loopback documentation requests only.")
                    return
                path = unquote(urlsplit(self.path).path)
                target = (owner.frontend_directory / path.lstrip("/")).resolve()
                if not target.is_relative_to(owner.frontend_directory):
                    self.send_error(404)
                    return
                super().do_GET()

            def do_POST(self):
                if not self._local_request() or urlsplit(self.path).path != "/test-api":
                    self.send_error(403, "Loopback documentation requests only.")
                    return
                if self.headers.get("Origin") not in (None, owner.url):
                    self.send_error(403, "Same-origin documentation requests only.")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2_000_000:
                        raise ValueError("Invalid documentation request size.")
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict):
                        raise ValueError("The documentation request must be an object.")
                    result = owner.service.request(request.get("method"), request.get("payload", {}))
                except (ValueError, TypeError) as error:
                    result = {"ok": False, "error": {"message": str(error)}}
                content = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="stompilot-documentation-preview", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=3)
        if not self.service.close():
            raise RuntimeError("The documentation service still has an operation in progress.")
        self.temporary.cleanup()
        return False


BRIDGE_SCRIPT = """
window.__STOMPILOT_REQUEST__ = async (method, payload = {}) => {
    const response = await fetch('/test-api', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({method, payload})
    });
    return response.json();
};
"""
