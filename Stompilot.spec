# PyInstaller one-file Windows GUI build. All paths follow this project folder.
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs

project_dir = Path(SPECPATH)
app_dir = project_dir / "midi_configurator"
data_files = [(str(path), "midi_configurator/profiles")
              for path in sorted((app_dir / "profiles").glob("*.json"))]
data_files += [(str(path), "examples")
               for path in sorted((project_dir / "examples").glob("*.json"))]

analysis = Analysis(
    [str(app_dir / "app.py")],
    pathex=[str(app_dir), str(project_dir)],
    binaries=collect_dynamic_libs("rtmidi"),
    datas=data_files,
    hiddenimports=[
        "mido.backends.rtmidi", "rtmidi", "rtmidi._rtmidi",
        "core", "chocolate", "chocolate_transport", "controller_ui", "runtime_paths",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
