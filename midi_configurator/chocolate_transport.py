"""Chocolate Plus configuration over the native USB MIDI SysEx protocol.

The message framing matches MidiSuite's USB transport. The audited stock V13
uses verified configuration-sector writes and requires a final restart check.
No firmware, bootloader, or arbitrary memory operation is exposed publicly.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Callable

try:
    from .core import ValidationError, pack7, parse_packet
except ImportError:  # The desktop launcher executes desktop.py directly.
    from core import ValidationError, pack7, parse_packet


CONFIG_SIZE = 23646
MEMORY_TYPE = 4
# MidiSuite's nominal limits are 1009/1024 bytes. The connected V16 unit
# answers 512-byte reads but does not answer 896-byte or larger reads, so use
# conservative packets below the observed USB message-size limit.
READ_CHUNK = 512
WRITE_CHUNK = 512
GLOBAL_OFFSETS = (23639, 23640, 0)  # Activate the selected operating mode last.
ProgressCallback = Callable[[int, int], None]


class ChocolateTransportError(RuntimeError):
    """A readable connection or controller protocol error."""


@dataclass(frozen=True)
class UsbDevice:
    name: str
    input_name: str
    output_name: str


@dataclass(frozen=True)
class ChocolateIdentity:
    name: str
    firmware_version: int | None
    raw_payload: bytes


_BACKEND = None


def use_backend(module) -> None:
    """Use another mido-shaped MIDI backend, such as the Android app's; None restores mido."""
    global _BACKEND
    _BACKEND = module


def _mido():
    if _BACKEND is not None:
        return _BACKEND
    try:
        import mido
        return mido
    except ImportError as error:
        raise ChocolateTransportError(
            "USB support requires mido and python-rtmidi. Run Setup Stompilot.cmd."
        ) from error


def _port_name(name: str) -> str:
    return re.sub(r"\s+\d+$", "", name).casefold()


def usb_packet(command: int, payload: bytes = b"") -> bytes:
    """MidiSuite uses the continuous 7-bit stream without a redundant group."""
    raw = b"\x00\x59" + bytes([command]) + len(payload).to_bytes(3, "little")
    raw += payload + bytes([(~sum(payload)) & 255])
    return b"\xF0" + pack7(raw)[:(len(raw) * 8 + 6) // 7] + b"\xF7"


def list_usb_devices() -> list[UsbDevice]:
    """Match Chocolate Plus input/output ports without choosing another device."""
    midi = _mido()
    try:
        inputs = midi.get_input_names()
        outputs = midi.get_output_names()
    except Exception as error:
        raise ChocolateTransportError(f"Cannot list USB MIDI ports: {error}") from error
    found = []
    for input_name in inputs:
        if "footctrlplus" not in input_name.casefold():
            continue
        matching = [name for name in outputs if _port_name(name) == _port_name(input_name)]
        if len(matching) == 1:
            found.append(UsbDevice(re.sub(r"\s+\d+$", "", input_name), input_name, matching[0]))
    return found


def changed_ranges(before: bytes, after: bytes) -> list[tuple[int, bytes]]:
    """Return only changed spans; unchanged settings are never sent for writing."""
    if len(before) != CONFIG_SIZE or len(after) != CONFIG_SIZE:
        raise ChocolateTransportError(f"Expected a {CONFIG_SIZE}-byte Chocolate Plus configuration.")
    ranges = []
    start = None
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new and start is None:
            start = index
        elif old == new and start is not None:
            ranges.append((start, after[start:index]))
            start = None
    if start is not None:
        ranges.append((start, after[start:]))
    return ranges


class UsbChocolate:
    """A serialized connection; call from a worker thread, never from the UI thread."""

    def __init__(self, input_name: str, output_name: str, *, timeout: float = 3.0):
        if "footctrlplus" not in input_name.casefold() or "footctrlplus" not in output_name.casefold():
            raise ChocolateTransportError("Choose the FootCtrlPlus USB MIDI input and output.")
        if _port_name(input_name) != _port_name(output_name):
            raise ChocolateTransportError("The USB MIDI input and output must belong to the same controller.")
        self.input_name = input_name
        self.output_name = output_name
        self.timeout = timeout
        self.input = None
        self.output = None
        self.identity = None
        self.last_query_packet = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self.input is not None and self.output is not None

    def open(self):
        with self._lock:
            if self.is_open:
                return self
            midi = _mido()
            try:
                self.input = midi.open_input(self.input_name)
                self.output = midi.open_output(self.output_name)
            except Exception as error:
                self.close()
                raise ChocolateTransportError(
                    f"Cannot open Chocolate Plus USB MIDI ports: {error}. Close MidiSuite and try again."
                ) from error
            return self

    def close(self):
        with self._lock:
            for port in (self.output, self.input):
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
            self.output = self.input = None
            self.identity = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    def _request(self, command: int, payload: bytes, wanted: int, prefix: bytes | None = None):
        with self._lock:
            if not self.is_open:
                raise ChocolateTransportError("Connect the Chocolate Plus first.")
            try:
                # Remove replies from an earlier request before sending this one.
                for _ in self.input.iter_pending():
                    pass
                self.output.send(_mido().Message.from_bytes(usb_packet(command, payload)))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    for message in self.input.iter_pending():
                        if message.type != "sysex":
                            continue
                        try:
                            response = parse_packet(bytes(message.bytes()))
                        except ValidationError:
                            # Other equipment's SysEx can arrive on a MIDI route.
                            continue
                        if response["opcode"] == wanted and (
                            prefix is None or response["payload"][:len(prefix)] == prefix
                        ):
                            return response
                    time.sleep(0.005)
            except ChocolateTransportError:
                raise
            except Exception as error:
                raise ChocolateTransportError(f"Chocolate Plus USB communication failed: {error}") from error
            raise ChocolateTransportError(
                f"Chocolate Plus did not answer command 0x{command:02X}. Check the USB cable and close MidiSuite."
            )

    def query(self) -> ChocolateIdentity:
        response = self._request(0x11, b"", 0x11)
        payload = response["payload"]
        try:
            name = payload[:24].split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise ChocolateTransportError("The connected device returned an invalid identity.") from error
        if not name.startswith("FootCtrlPlus_"):
            raise ChocolateTransportError(f"The connected device is {name!r}, expected FootCtrlPlus.")
        match = re.search(r"_(\d+)$", name)
        identity = ChocolateIdentity(name, int(match.group(1)) if match else None, payload)
        self.identity = identity
        self.last_query_packet = response["raw"]
        return identity

    def _read(self, address: int, length: int) -> bytes:
        if not 0 <= address < CONFIG_SIZE or not 1 <= length <= READ_CHUNK or address + length > CONFIG_SIZE:
            raise ChocolateTransportError("Read exceeds the Chocolate Plus configuration region.")
        prefix = bytes([MEMORY_TYPE]) + address.to_bytes(4, "little") + length.to_bytes(3, "little")
        response = self._request(0x23, prefix, 0x23, prefix)
        data = response["payload"][8:]
        if len(data) != length:
            raise ChocolateTransportError(f"Incomplete controller configuration: {len(data)}/{length} bytes.")
        return data

    def read_config(self, progress_callback: ProgressCallback | None = None) -> bytes:
        with self._lock:
            if self.identity is None:
                self.query()
            image = bytearray()
            while len(image) < CONFIG_SIZE:
                image.extend(self._read(len(image), min(READ_CHUNK, CONFIG_SIZE - len(image))))
                if progress_callback:
                    progress_callback(len(image), CONFIG_SIZE)
            return bytes(image)

    def _write(self, address: int, data: bytes):
        if not 0 <= address < CONFIG_SIZE or not 1 <= len(data) <= WRITE_CHUNK or address + len(data) > CONFIG_SIZE:
            raise ChocolateTransportError("Write exceeds the Chocolate Plus configuration region.")
        prefix = bytes([MEMORY_TYPE]) + address.to_bytes(4, "little") + len(data).to_bytes(3, "little")
        response = self._request(0x22, prefix + data, 0x00)
        status = response["payload"]
        if not status or status[0] != 0:
            raise ChocolateTransportError(
                "Chocolate Plus refused a configuration write" + (f" (status {status[0]})." if status else ".")
            )
        if self._read(address, len(data)) != data:
            raise ChocolateTransportError(
                f"Chocolate Plus did not retain the settings at byte {address}. Keep the backup."
            )

    def _persistence(self, backup_directory, progress_callback):
        if __package__:
            from .chocolate_persistence import VerifiedWriter
            from .runtime_paths import data_directory
        else:
            from chocolate_persistence import VerifiedWriter
            from runtime_paths import data_directory
        return VerifiedWriter(self, backup_directory or data_directory() / ".backups", progress_callback)

    def verify_config_restart(self, backup_directory, progress_callback=None) -> bytes:
        with self._lock:
            return self._persistence(backup_directory, progress_callback).verify_restart()

    def restore_config_backup(self, backup_directory, progress_callback=None) -> bytes:
        with self._lock:
            return self._persistence(backup_directory, progress_callback).restore()

    def write_config(self, before: bytes, after: bytes, progress_callback: ProgressCallback | None = None,
                     *, backup_directory=None) -> bytes:
        """Apply a reviewed patch after checking its snapshot, then verify every byte.

        The caller must save a backup before this operation. Changes made by
        another editor after that backup cause the operation to abort before
        writing. Writes are not automatically retried after a lost ACK.
        """
        with self._lock:
            before, after = bytes(before), bytes(after)
            ranges = changed_ranges(before, after)
            # V13's immediate partial writes lack an erase. Never fall back to
            # those writes when build checks or verified saving fail.
            if self.query().name == "FootCtrlPlus_013":
                return self._persistence(backup_directory, progress_callback).write(before, after)
            settings, globals_ = [], []
            for address, data in ranges:
                end = address + len(data)
                cursor = address
                for global_offset in sorted(GLOBAL_OFFSETS):
                    if address <= global_offset < end:
                        if cursor < global_offset:
                            settings.append((cursor, after[cursor:global_offset]))
                        globals_.append((global_offset, after[global_offset:global_offset + 1]))
                        cursor = global_offset + 1
                if cursor < end:
                    settings.append((cursor, after[cursor:end]))
            globals_.sort(key=lambda item: GLOBAL_OFFSETS.index(item[0]))
            ranges = settings + globals_
            if self.read_config() != before:
                raise ChocolateTransportError(
                    "The controller configuration changed since it was read. Read it again before writing."
                )
            total = sum(len(data) for _, data in ranges)
            done = 0
            for address, data in ranges:
                for offset in range(0, len(data), WRITE_CHUNK):
                    chunk = data[offset:offset + WRITE_CHUNK]
                    self._write(address + offset, chunk)
                    done += len(chunk)
                    if progress_callback:
                        progress_callback(done, total)
            verified = self.read_config()
            if verified != after:
                differing = next(i for i, (expected, actual) in enumerate(zip(after, verified)) if expected != actual)
                raise ChocolateTransportError(
                    f"The controller did not retain the complete configuration (byte {differing}). Keep the backup."
                )
            return verified
