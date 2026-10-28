# Package the Svelte desktop interface and Python MIDI engine as one Windows EXE.
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs

project_dir = Path(SPECPATH)
app_dir = project_dir / "midi_configurator"
frontend_dir = project_dir / "frontend" / "build"
if not (frontend_dir / "index.html").is_file():
    raise RuntimeError("Build the Svelte interface first: npm.cmd --prefix frontend run build")
data_files = [(str(path), "midi_configurator/profiles")
              for path in sorted((app_dir / "profiles").glob("*.json"))]
data_files += [(str(path), "examples")
               for path in sorted((project_dir / "examples").glob("*.json"))]
data_files += [(str(path), str(Path("frontend") / "build" / path.relative_to(frontend_dir).parent))
               for path in sorted(frontend_dir.rglob("*")) if path.is_file()]

analysis = Analysis(
    [str(app_dir / "desktop.py")],
    pathex=[str(app_dir), str(project_dir)],
    binaries=collect_dynamic_libs("rtmidi"),
    datas=data_files,
    hiddenimports=[
        "mido.backends.rtmidi", "rtmidi", "rtmidi._rtmidi",
        "core", "chocolate", "chocolate_transport", "chocolate_persistence", "desktop_service", "runtime_paths",
        "webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "clr_loader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3"],
    noarchive=False,
    optimize=0,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Stompilot",
    icon=str(project_dir / "assets" / "stompilot.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
