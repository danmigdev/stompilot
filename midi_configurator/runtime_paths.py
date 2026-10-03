"""Keep bundled resources separate from persistent portable application data."""
from pathlib import Path
import sys


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Locate immutable profiles inside the source tree or extracted bundle."""
    if frozen():
        return Path(sys._MEIPASS) / "midi_configurator"
    return Path(__file__).resolve().parent


def data_directory() -> Path:
    """Locate durable data beside the EXE, or at the source project's root."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def profile_directory() -> Path:
    """Locate custom profiles; frozen defaults remain in resource_dir()."""
    if frozen():
        return data_directory() / "profiles"
    return resource_dir() / "profiles"
