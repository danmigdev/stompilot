"""Verified configuration-sector writes for the exact audited stock V13 build.

Uses stock callbacks via a temporary DATA table in unused SysEx padding. Only
USRFLASH is erased/programmed; executable code, firmware and boot are untouched.
The normal save pointer stays disabled until a physical restart. The durable
journal blocks another write and supports explicit backup restoration after an
interruption. An ACK or a RAM read is never treated as persistent verification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import uuid

try:
    from .chocolate_transport import CONFIG_SIZE, ChocolateTransportError
except ImportError:
    from chocolate_transport import CONFIG_SIZE, ChocolateTransportError


APP_BASE, APP_SIZE = 0x1E00120, 155488
APP_SHA256 = "f535c717a1ce23ef1b0fe5251c0ab7c906f2be068d2c172ad8440be1e0edeadc"
RAM_BASE, POINTER, SLOT, ERASE_UNIT = 0x13490, 0x19100, 0x19774, 0x193DB
FLASH_BASE, FLASH_VIEW, FLASH_SIZE = 0x39000, 0x1E38000, 0x6000
DISABLED_POINTER, SECTOR_SIZE, PAGE_SIZE = 0x100000, 4096, 256
TABLE = struct.pack("<4I", 0x1E0CCF6, 0x1CF66, 0x1E0CD8E, 0)
DONE = {"complete", "restored", "cancelled"}
RESTART_MESSAGE = ("Flash verified. Set the pedal to O, unplug USB-C from the pedal and keep its display "
                   "off for 10 seconds. Return to U, reconnect USB-C, then select Verify after restart.")
RECOVERY_MESSAGE = ("The save was interrupted. Keep the pedal powered and connected in U. "
                    "Select Restore backup before restarting it; the complete backup is saved.")


def require(condition, message):
    if not condition:
        raise ChocolateTransportError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def durable(path, data, *, replace=False):
    """Flush backup/state to disk before issuing the associated mutation."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp") if replace else path
    with temporary.open("wb" if replace else "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(temporary, path)
    require(path.read_bytes() == data, "The complete configuration backup could not be verified; no further write was sent.")


class SaveJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.state = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
        require(isinstance(self.state, dict) and isinstance(self.state.get("hashes"), dict)
                and isinstance(self.state.get("events"), list)
                and self.state.get("schema_version") == 1 and self.state.get("firmware") == APP_SHA256
                and self.state.get("status") in DONE | {"prepared", "writing", "recovery_required", "restart_required"}
                and self.state.get("target") in {"desired", "original"}, "Invalid verified-save record. Keep the backup files.")
        self.original = self._load("original", FLASH_SIZE)
        self.desired = self._load("desired", FLASH_SIZE)
        self.ram = self._load("ram", CONFIG_SIZE)
        require(self.original[23652:23660] == b"usrdata\0"
                and self.original[CONFIG_SIZE:] == self.desired[CONFIG_SIZE:], "Backup marker or preserved configuration tail differs.")

    def _load(self, name, size):
        data = (self.directory / f"{name}.bin").read_bytes()
        require(len(data) == size and digest(data) == self.state.get("hashes", {}).get(name),
                f"The {name} configuration backup is damaged. No write was sent.")
        return data

    @classmethod
    def create(cls, directory, ram, original, desired):
        root = Path(directory) / "verified-saves"
        root.mkdir(parents=True, exist_ok=True)
        folder = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8])
        folder.mkdir()
        values = dict(ram=ram, original=original, desired=desired)
        for name, data in values.items():
            durable(folder / f"{name}.bin", data)
        state = dict(schema_version=1, firmware=APP_SHA256, status="prepared", target="desired",
                     hashes={name: digest(data) for name, data in values.items()}, events=[])
        durable(folder / "state.json", (json.dumps(state, indent=2) + "\n").encode())
        return cls(folder)

    def record(self, event, **fields):
        self.state.update(fields)
        self.state["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), event=event))
        durable(self.directory / "state.json", (json.dumps(self.state, indent=2) + "\n").encode(), replace=True)

    @property
    def target(self):
        return self.desired if self.state["target"] == "desired" else self.original

    def summary(self):
        return dict(status=self.state["status"], backup_name=self.directory.name,
                    restored=self.state["target"] == "original",
                    message=RESTART_MESSAGE if self.state["status"] == "restart_required" else RECOVERY_MESSAGE)


def pending_save(directory):
    pending = []
    for path in sorted((Path(directory) / "verified-saves").glob("*/state.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        require(isinstance(state, dict), "Invalid verified-save record. Keep the backup files.")
        if state.get("status") not in DONE:
            pending.append(SaveJournal(path.parent))
    require(len(pending) <= 1, "Several unfinished configuration saves exist. Keep their backups; writing is disabled.")
    return pending[0] if pending else None


def choose_scratch(image):
    """Use 16 aligned bytes strictly beyond an advanced bank's stored length.

    RAM settings stay unchanged during programming except for this unused tail.
    All original padding bytes are restored, including nonzero padding. Refuse
    configurations with no suitable padding instead of modifying a used bank.
    """
    require(len(image) == CONFIG_SIZE, "Invalid configuration size.")
    # Prefer the previously measured page-7 D bank A location when it is unused.
    candidates = [(93 + 27 * 417 + 161, 93 + 27 * 417 + 164)]
    for switch in reversed(range(32)):
        for bank in (161, 289):
            start = 93 + switch * 417 + bank
            candidates.append((start, ((RAM_BASE + start + 128 - 16) & ~3) - RAM_BASE))
    for start, offset in candidates:
        length = image[start]
        if length <= 127 and start + 1 + length <= offset and offset + 16 <= start + 128:
            return offset
    raise ChocolateTransportError("Verified saving needs 16 unused bytes in a SysEx bank. "
                                  "All banks are full or use an unsupported format; no settings were written.")


class VerifiedWriter:
    def __init__(self, device, directory, progress=None):
        self.device, self.directory = device, Path(directory)
        self.progress = progress or (lambda *args: None)
        self.scratch = None
        self.ram = None
        self.journal = None

    def read(self, address, size):
        ranges = ((APP_BASE, APP_SIZE), (RAM_BASE, CONFIG_SIZE), (FLASH_VIEW, FLASH_SIZE),
                  (POINTER, 4), (SLOT, 4), (ERASE_UNIT, 1), (0x197E8, 32), (0x1CF60, 0x3E4))
        require(size > 0 and any(start <= address < address + size <= start + length for start, length in ranges),
                "Read outside the audited V13 regions.")
        result = bytearray()
        while len(result) < size:
            count = min(512, size - len(result))
            prefix = b"\x04" + (address + len(result) - RAM_BASE).to_bytes(4, "little") + count.to_bytes(3, "little")
            data = self.device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
            require(len(data) == count, "Incomplete V13 memory response; keep the backup.")
            result.extend(data)
        return bytes(result)

    def audit(self):
        require(self.device.query().name == "FootCtrlPlus_013", "Verified sector saving supports the audited stock V13 only.")
        self.progress("Checking the installed firmware before configuration access…")
        app = self.read(APP_BASE, APP_SIZE)
        require(digest(app) == APP_SHA256, "This V13 build differs from the verified original. No configuration write was sent.")
        require(self.read(0x1CF60, 0x3E4) == app[0x25B54:0x25F38], "The running flash driver differs. No write was sent.")
        state = struct.unpack("<8I", self.read(0x197E8, 32))
        require(state[0] == 0x40000 and state[6:] == (0x1000, 0x1E00000), "Unexpected V13 flash capacity or mapping.")

    def normal_controls(self):
        require(self.read(POINTER, 4) == struct.pack("<I", FLASH_BASE)
                and self.read(SLOT, 4) == bytes(4) and self.read(ERASE_UNIT, 1) == b"\0",
                "A previous save still needs recovery or a physical restart. No new write was sent.")

    def stable_mapping(self):
        first = self.read(FLASH_VIEW, FLASH_SIZE)
        require(first == self.read(FLASH_VIEW, FLASH_SIZE), "The configuration flash reads are unstable. No erase was sent.")
        require(first[23652:23660] == b"usrdata\0", "Unexpected V13 configuration marker. No erase was sent.")
        return first

    def ram_write(self, address, data, *, initial=False):
        data = bytes(data)
        allowed = ((POINTER, struct.pack("<I", DISABLED_POINTER)), (SLOT, bytes(4)),
                   (SLOT, struct.pack("<I", RAM_BASE + self.scratch)),
                   (RAM_BASE + self.scratch, TABLE),
                   (RAM_BASE + self.scratch, self.ram[self.scratch:self.scratch + 16]),
                   (ERASE_UNIT, b"\0"), (ERASE_UNIT, b"\x10"))
        require((address, data) in allowed, "RAM write outside the fixed save controls.")
        require(self.read(POINTER, 4) == struct.pack("<I", FLASH_BASE if initial else DISABLED_POINTER),
                "The save controls changed. No further mutation was sent.")
        prefix = b"\x04" + (address - RAM_BASE).to_bytes(4, "little") + len(data).to_bytes(3, "little")
        self.ack(0x22, prefix + data)
        require(self.read(address, len(data)) == data, "Temporary save control did not verify; keep the backup.")

    def ack(self, command, payload):
        require(self.device._request(command, payload, 0)["payload"] == b"\0", "The controller refused a verified-save operation.")

    def controls(self):
        require(self.read(POINTER, 4) == struct.pack("<I", DISABLED_POINTER)
                and self.read(SLOT, 4) == struct.pack("<I", RAM_BASE + self.scratch)
                and self.read(RAM_BASE + self.scratch, 16) == TABLE,
                "Temporary save controls changed; no further erase/program was sent.")

    def spi_read(self, address, size):
        require(FLASH_BASE <= address < address + size <= FLASH_BASE + FLASH_SIZE, "SPI read outside configuration.")
        out = bytearray()
        while len(out) < size:
            count = min(512, size - len(out))
            prefix = b"\x05" + (address + len(out)).to_bytes(4, "little") + count.to_bytes(3, "little")
            data = self.device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
            require(len(data) == count, "Incomplete physical configuration read; keep the backup.")
            out.extend(data)
        return bytes(out)

    def physical_pair(self):
        self.controls()
        first = self.spi_read(FLASH_BASE, FLASH_SIZE)
        require(first == self.spi_read(FLASH_BASE, FLASH_SIZE), "Physical configuration reads differ; keep the backup.")
        return first

    def erase(self, address):
        require(FLASH_BASE <= address <= FLASH_BASE + FLASH_SIZE - SECTOR_SIZE and address % SECTOR_SIZE == 0,
                "Erase outside the configuration sectors.")
        self.controls()
        require(self.read(ERASE_UNIT, 1) == b"\0", "Unexpected erase unit; no erase was sent.")
        self.journal.record("erase_armed", sector=address, status="writing")
        try:
            self.ram_write(ERASE_UNIT, b"\x10")
            # Command 21 has the audited fixed ACK. Command 24 is never used.
            self.ack(0x21, b"\x05" + struct.pack("<I", address))
        finally:
            self.ram_write(ERASE_UNIT, b"\0")
        blank = bytes([255]) * SECTOR_SIZE
        require(self.spi_read(address, SECTOR_SIZE) == blank and self.spi_read(address, SECTOR_SIZE) == blank,
                "The entire configuration sector did not verify erased. Keep the backup.")

    def program(self, address, data):
        target = self.journal.target
        offset = address - FLASH_BASE
        require(address % PAGE_SIZE == 0 and 0 <= offset <= FLASH_SIZE - PAGE_SIZE
                and len(data) == PAGE_SIZE and data == target[offset:offset + PAGE_SIZE],
                "Program outside the backed-up configuration plan.")
        self.controls()
        require(self.read(ERASE_UNIT, 1) == b"\0", "Unexpected erase-unit state.")
        self.ack(0x22, b"\x05" + address.to_bytes(4, "little") + len(data).to_bytes(3, "little") + data)

    def cleanup(self):
        # Never re-enable saving by writing FLASH_BASE: the stock callback would
        # immediately persist the out-of-config control write. Restart does that.
        self.ram_write(ERASE_UNIT, b"\0")
        self.ram_write(SLOT, bytes(4))
        self.ram_write(RAM_BASE + self.scratch, self.ram[self.scratch:self.scratch + 16])
        require(self.device.read_config() == self.ram, "RAM changed during saving. Keep the backup and verify after restart.")

    def transfer(self, current):
        target = self.journal.target
        armed = False
        try:
            self.journal.record("controls_armed", status="writing", scratch=self.scratch,
                                cleanup_ram_sha256=digest(self.ram), cleanup_padding=self.ram[self.scratch:self.scratch + 16].hex())
            armed = True
            pointer = self.read(POINTER, 4)
            if pointer == struct.pack("<I", FLASH_BASE):
                self.ram_write(POINTER, struct.pack("<I", DISABLED_POINTER), initial=True)
            else:
                require(pointer == struct.pack("<I", DISABLED_POINTER), "Unexpected save pointer.")
            self.ram_write(RAM_BASE + self.scratch, TABLE)
            self.ram_write(SLOT, struct.pack("<I", RAM_BASE + self.scratch))
            self.controls()
            require(self.read(ERASE_UNIT, 1) == b"\0", "Unexpected erase unit.")
            self.ack(0x21, b"\x05" + struct.pack("<I", FLASH_BASE))  # flag 0: verified no-op
            require(self.physical_pair() == current, "Raw SPI differs from the initial flash capture. No erase was sent.")
            expected_ram = bytearray(self.ram)
            expected_ram[self.scratch:self.scratch + 16] = TABLE
            require(self.device.read_config() == bytes(expected_ram), "Settings changed while preparing the save. No erase was sent.")
            sectors = [pos for pos in range(0, FLASH_SIZE, SECTOR_SIZE)
                       if current[pos:pos + SECTOR_SIZE] != target[pos:pos + SECTOR_SIZE]]
            for index, pos in enumerate(sectors):
                self.progress(f"Writing and checking configuration sector {index + 1} of {len(sectors)}…")
                address = FLASH_BASE + pos
                self.erase(address)
                for offset in range(pos, pos + SECTOR_SIZE, PAGE_SIZE):
                    page = target[offset:offset + PAGE_SIZE]
                    if page != bytes([255]) * PAGE_SIZE:
                        self.program(FLASH_BASE + offset, page)
                require(self.spi_read(address, SECTOR_SIZE) == target[pos:pos + SECTOR_SIZE],
                        "Configuration sector readback failed. Keep the backup.")
            require(self.physical_pair() == target, "Complete physical configuration verification failed. Keep the backup.")
            self.cleanup()
            armed = False
            self.journal.record("physical_flash_verified", status="restart_required", verified_sha256=digest(target))
            return target[:CONFIG_SIZE]
        except BaseException as error:
            cleanup_error = None
            if armed:
                try:
                    self.cleanup()
                except BaseException as failure:
                    cleanup_error = str(failure)
            try:
                self.journal.record("interrupted", status="recovery_required", error=str(error), cleanup_error=cleanup_error)
            except OSError:
                pass  # The already-flushed prepared/writing state still blocks another write.
            raise ChocolateTransportError(f"{RECOVERY_MESSAGE} {error}") from error

    def write(self, before, after):
        require(pending_save(self.directory) is None, "Finish verification or restore the previous save before writing another project.")
        require(len(before) == len(after) == CONFIG_SIZE, "Invalid configuration size.")
        self.audit()
        self.normal_controls()
        self.ram = self.device.read_config()
        require(self.ram == bytes(before), "The controller configuration changed since the preview. Read it again before writing.")
        self.scratch = choose_scratch(self.ram)
        current = self.stable_mapping()
        target = bytes(after) + current[CONFIG_SIZE:]
        if current == target and self.ram == bytes(after):
            return bytes(after)  # Already persistent; no temporary control writes or restart needed.
        self.journal = SaveJournal.create(self.directory, self.ram, current, target)
        return self.transfer(current)

    def restore(self):
        self.journal = pending_save(self.directory)
        require(self.journal is not None, "There is no unfinished save to restore.")
        self.audit()
        pointer, slot, flag = self.read(POINTER, 4), self.read(SLOT, 4), self.read(ERASE_UNIT, 1)
        require(pointer in (struct.pack("<I", FLASH_BASE), struct.pack("<I", DISABLED_POINTER)), "Unknown save pointer; no recovery write was sent.")
        self.ram = self.device.read_config()
        if pointer == struct.pack("<I", FLASH_BASE):
            self.normal_controls()
        else:
            # Normalize only the exact controls/padding recorded before our interrupted operation.
            self.scratch = self.journal.state.get("scratch")
            require(isinstance(self.scratch, int) and 0 <= self.scratch <= CONFIG_SIZE - 16
                    and slot in (bytes(4), struct.pack("<I", RAM_BASE + self.scratch)) and flag in (b"\0", b"\x10"),
                    "Unrecognized temporary save state; no recovery write was sent.")
            padding = bytes.fromhex(self.journal.state.get("cleanup_padding", ""))
            require(len(padding) == 16 and self.ram[self.scratch:self.scratch + 16] in (padding, TABLE), "Unrecognized save padding.")
            original_ram = bytearray(self.ram)
            original_ram[self.scratch:self.scratch + 16] = padding
            require(digest(original_ram) == self.journal.state.get("cleanup_ram_sha256")
                    and choose_scratch(original_ram) == self.scratch, "RAM changed since the interrupted save; no recovery write was sent.")
            self.ram = bytes(original_ram)
            self.cleanup()
        self.scratch = choose_scratch(self.ram)
        # During recovery an interrupted marker may be blank; target still comes
        # solely from the durable, checked original 24-KiB backup.
        current = self.read(FLASH_VIEW, FLASH_SIZE)
        require(current == self.read(FLASH_VIEW, FLASH_SIZE), "Flash is unstable; recovery stopped before erase.")
        self.journal.record("restore_requested", target="original")
        return self.transfer(current)

    def verify_restart(self):
        journal = pending_save(self.directory)
        require(journal is not None, "There is no unfinished save to verify.")
        self.audit()
        self.normal_controls()
        target = journal.target
        require(self.device.read_config() == self.device.read_config() == target[:CONFIG_SIZE]
                and self.stable_mapping() == target,
                "The configuration after restart differs from the saved image. Keep the backup; Restore backup remains available.")
        journal.record("restart_verified", status="restored" if journal.state["target"] == "original" else "complete")
        return target[:CONFIG_SIZE]
