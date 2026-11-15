"""Suspended fixed-sector erase/program experiment. Default is an OFFLINE plan.

The first --execute request erased sector 0x3C000..0x3CFFF but disconnected USB
before readback/programming. Both --execute and the erase-based --recover are
now disabled. restore_erased_sector.py provides guarded program-only recovery.
All device functions are stock V13 RAM routines; no executable payload exists.
An O/USB-disconnect/U restart is required afterward to restore the RAM pointer.
This is a configuration-sector diagnostic, not a firmware installer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

from capture_persistence import (HERE, RAM_BASE, FLASH_BASE, FLASH_VIEW,
                                 RESERVATION_SIZE, UsbChocolate, list_usb_devices,
                                 capture, audit, read_audited, require, sha)
from read_spi import POINTER, SLOT, DISABLED_FLASH_BASE, SPI_READ, ram_read, write_command, spi_read
from retention_probe import RAM_SHA, FLASH_SHA
from cycle_probe import OFFSET, LENGTH
from midi_configurator.chocolate import PAGE_COUNT_OFFSET
from midi_configurator.chocolate_transport import ChocolateTransportError

SECTOR = 0x3C000
SECTOR_SIZE, PAGE_SIZE = 4096, 256
RELATIVE = SECTOR - FLASH_BASE
SPI_PROGRAM, SPI_ERASE = 0x1D1B8, 0x1D114
# Page 7 D bank A padding, outside the sector being erased. This remains
# available for recovery even after a restart with the target sector incomplete.
SCRATCH_OFFSET = 93 + 27 * 417 + 164
SCRATCH = RAM_BASE + SCRATCH_OFFSET
TABLE = struct.pack("<4I", 0, SPI_READ, SPI_PROGRAM, SPI_ERASE)
BASELINE = HERE / "artifacts/spi-probes/20261027T175717.404580Z/baseline"
ROOT = HERE / "artifacts/sector-erase-probes"


def baseline():
    ram = (BASELINE / "ram-before.bin").read_bytes()
    flash = (BASELINE / "usrflash-first.bin").read_bytes()
    require(sha(ram) == RAM_SHA and sha(flash) == FLASH_SHA, "Pinned baseline mismatch")
    return ram, flash


def pattern_image(original):
    result = bytearray(original)
    result[OFFSET - RELATIVE:OFFSET - RELATIVE + LENGTH] = b"\xff" * LENGTH
    return bytes(result)


def program_image(backend, image):
    require(len(image) == SECTOR_SIZE, "Wrong sector size")
    # All-FF pages remain erased; other pages receive one aligned program each.
    for pos in range(0, SECTOR_SIZE, PAGE_SIZE):
        data = image[pos:pos + PAGE_SIZE]
        if data != b"\xff" * PAGE_SIZE:
            backend.program(SECTOR + pos, data)


def erase_verified(backend):
    backend.erase(SECTOR)
    first, second = backend.read(SECTOR, SECTOR_SIZE), backend.read(SECTOR, SECTOR_SIZE)
    require(first == second == b"\xff" * SECTOR_SIZE, "Sector did not verify completely erased")


def restore_sector(backend, original, full_backup):
    erase_verified(backend)
    program_image(backend, original)
    first = backend.read(FLASH_BASE, RESERVATION_SIZE)
    second = backend.read(FLASH_BASE, RESERVATION_SIZE)
    require(first == second == full_backup, "Complete physical USRFLASH restoration differs from backup")


def run_experiment(backend, full_backup, record):
    """Always restore after an erase request, including an uncertain failure."""
    original = full_backup[RELATIVE:RELATIVE + SECTOR_SIZE]
    desired = pattern_image(original)
    require(backend.read(FLASH_BASE, RESERVATION_SIZE) == full_backup and
            backend.read(FLASH_BASE, RESERVATION_SIZE) == full_backup,
            "Physical baseline mismatch; no erase requested")
    try:
        record("test_erase_armed")
        erase_verified(backend)
        record("test_sector_erased")
        program_image(backend, desired)
        first, second = backend.read(FLASH_BASE, RESERVATION_SIZE), backend.read(FLASH_BASE, RESERVATION_SIZE)
        expected = full_backup[:RELATIVE] + desired + full_backup[RELATIVE + SECTOR_SIZE:]
        record("pattern_measured", spi_first_sha256=sha(first), spi_second_sha256=sha(second),
               first_pattern=first[OFFSET:OFFSET + LENGTH].hex(), second_pattern=second[OFFSET:OFFSET + LENGTH].hex(),
               stable=first == second, complete_image_matches=first == second == expected)
        require(first == second == expected, "Pattern still differs after an explicitly verified erase")
    finally:
        record("restoration_armed")
        restore_sector(backend, original, full_backup)
        record("physical_backup_restored", sha256=sha(full_backup))


class NativeBackend:
    def __init__(self, device, record, full_backup, capture_dir=None):
        self.device, self.record = device, record
        self.original = full_backup[RELATIVE:RELATIVE + SECTOR_SIZE]
        self.pattern = pattern_image(self.original)
        self.capture_dir, self.read_count = capture_dir, 0

    def read(self, address, size):
        data = spi_read(self.device, address, size)
        if self.capture_dir is not None:
            self.read_count += 1
            name = f"spi-{self.read_count:03d}-{address:06x}-{size}.bin"
            (self.capture_dir / name).write_bytes(data)
            self.record("spi_capture", file=name, sha256=sha(data))
        return data

    def erase(self, address):
        require(address == SECTOR, "Only the single fixed configuration sector may be erased")
        require(ram_read(self.device, SLOT, 4) == struct.pack("<I", SCRATCH) and
                ram_read(self.device, SCRATCH, 16) == TABLE, "Native callback table differs")
        # Type-5 operation 0x24 calls slot +12 with two little-endian uint32s.
        # Stock SPI erase ABI: r0=200 (4-KiB erase), r1=physical sector address.
        prefix = b"\x05" + struct.pack("<II", 200, SECTOR)
        self.record("erase_requested", physical_address=hex(SECTOR), bytes=SECTOR_SIZE)
        try:
            response = self.device._request(0x24, prefix, 0x24, prefix)["payload"]
        except ChocolateTransportError as error:
            # A missing/differently framed ACK must not prevent recovery after
            # an erase that already happened. Do not resend it here. Both callers
            # always perform two full raw blank checks before any programming.
            self.record("erase_ack_unconfirmed", error=str(error))
        else:
            confirmed = len(response) == 11 and response[9:] == bytes(2)
            self.record("erase_acknowledged" if confirmed else "erase_ack_unconfirmed",
                        payload=response.hex())

    def program(self, address, data):
        require(address % PAGE_SIZE == 0 and SECTOR <= address < SECTOR + SECTOR_SIZE and
                len(data) == PAGE_SIZE, "Program outside the fixed sector/page")
        pos = address - SECTOR
        require(data in (self.original[pos:pos + PAGE_SIZE], self.pattern[pos:pos + PAGE_SIZE]),
                "Page differs from the original or single permitted pattern")
        self.record("page_program_requested", physical_address=hex(address), sha256=sha(data))
        write_command(self.device, 5, address, data)
        self.record("page_program_acknowledged", physical_address=hex(address))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true")
    action.add_argument("--recover", type=Path)
    args = parser.parse_args()
    if args.execute or args.recover:
        parser.error("Native erase execution is suspended after the first 0x24 erase disconnected USB. "
                     "Do not repeat it for recovery. After a physical restart, use "
                     "restore_erased_sector.py --execute SESSION for the guarded program-only recovery.")
    ram, flash = baseline()
    require(SECTOR == FLASH_BASE + 3 * SECTOR_SIZE and
            SECTOR + SECTOR_SIZE <= FLASH_BASE + RESERVATION_SIZE, "Sector map mismatch")
    require(SCRATCH_OFFSET + 16 <= RELATIVE and
            OFFSET >= RELATIVE and OFFSET + LENGTH <= RELATIVE + SECTOR_SIZE, "Scratch/probe map mismatch")
    require(all(0x1BD4C <= f < 0x1E3AC for f in (SPI_READ, SPI_PROGRAM, SPI_ERASE)), "Function outside stock execution range")
    if not args.execute and not args.recover:
        print(json.dumps(dict(mode="suspended_offline_plan", device_io=False, execution_enabled=False,
                              physical_sector=hex(SECTOR),
                              sector_bytes=SECTOR_SIZE, expected_erase_count=2, pattern_offset=OFFSET,
                              pattern_bytes=LENGTH, baseline_sha256=FLASH_SHA,
                              executable_payload_bytes=0, restart_required_after_execution=True,
                              recovery="restore_erased_sector.py --execute SESSION, then physical restart and verify_recovery.py"), indent=2))
        return
    if args.recover:
        folder = args.recover.resolve()
        require(folder.parent == ROOT.resolve(), "Wrong recovery session")
        report = json.loads((folder / "report.json").read_text())
        require(report["baseline_sha256"] == FLASH_SHA and report["physical_sector"] == SECTOR,
                "Session does not describe the fixed sector")
        require((folder / "usrflash-backup.bin").read_bytes() == flash, "Session backup mismatch")
        report["recovering"] = True
    else:
        require(not any(json.loads(p.read_text()).get("restoration_pending") for p in ROOT.glob("*/report.json")),
                "A previous erase session requires recovery")
        folder = ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        folder.mkdir(parents=True)
        (folder / "ram-backup.bin").write_bytes(ram)
        (folder / "usrflash-backup.bin").write_bytes(flash)
        report = dict(status="preflight", physical_sector=SECTOR, baseline_sha256=FLASH_SHA,
                      firmware_writes=0, executable_payload_bytes=0, restoration_pending=False,
                      restart_required=False, events=[])

    def record(name, **details):
        if name in ("test_erase_armed", "restoration_armed"):
            report["restoration_pending"] = True
        if name == "physical_backup_restored":
            report["restoration_pending"] = False
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), event=name, **details))
        tmp = folder / "report.tmp"
        tmp.write_text(json.dumps(report, indent=2) + "\n")
        tmp.replace(folder / "report.json")
        print(name, json.dumps(details), flush=True)

    print(f"Session: {folder}", flush=True)
    record("started")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        audit(device)
        current_ram = device.read_config()
        current_flash = read_audited(device, FLASH_VIEW, RESERVATION_SIZE)
        for actual, original in ((current_ram, ram), (current_flash, flash)):
            require(all(a == b for i, (a, b) in enumerate(zip(actual, original))
                        if not args.recover or not RELATIVE <= i < RELATIVE + SECTOR_SIZE),
                    "Unexpected state outside the recovery sector")
        require(current_ram[PAGE_COUNT_OFFSET] < 6 and current_ram[0] == 3 and current_ram[23640] == 0,
                "Require advanced mode, page 1, with pages 7 and 8 disabled")
        require(current_ram[93 + 27 * 417 + 161] == 0 and
                current_ram[SCRATCH_OFFSET:SCRATCH_OFFSET + 16] == bytes(16), "Scratch bank is not empty")
        require(ram_read(device, SLOT, 4) == bytes(4), "Type 5 already registered")
        require(ram_read(device, 0x193B3, 1) == b"\x03", "Unsupported panel context")
        # audit() already pins the entire stock driver, including read/program/erase.
        from capture_persistence import APP_BASE, RELEASES
        ref = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
        require(sha(ref) == RELEASES["013"]["app_sha256"], "Wrong application")
        require(read_audited(device, 0x1E13C4E, 0x1C6) == ref[0x1E13C4E - APP_BASE:0x1E13E14 - APP_BASE],
                "Native command dispatch differs from the reviewed 0x24 ABI")
        for address, size in ((0x1E0CD20, 0x7C), (0x1E13B0E, 0xEA), (0x1E13A24, 0x44)):
            require(ram_read(device, address, size) == ref[address - APP_BASE:address - APP_BASE + size],
                    "RAM-install suppression path differs")

        def ram_write(address, data, initial=False):
            control = (address, data) in ((POINTER, struct.pack("<I", DISABLED_FLASH_BASE)),
                       (SCRATCH, TABLE), (SCRATCH, bytes(16)), (SLOT, struct.pack("<I", SCRATCH)), (SLOT, bytes(4)))
            relative = address - RAM_BASE
            recovering_ram = (args.recover and RELATIVE <= relative < relative + len(data) <= RELATIVE + SECTOR_SIZE
                              and 1 <= len(data) <= 512 and data == ram[relative:relative + len(data)])
            require(control or recovering_ram, "RAM write outside the fixed plan")
            expected_pointer = FLASH_BASE if initial else DISABLED_FLASH_BASE
            require(ram_read(device, POINTER, 4) == struct.pack("<I", expected_pointer), "Unexpected pointer")
            require(DISABLED_FLASH_BASE + relative + len(data) > 0x40000 and relative >= 0,
                    "Write would not be suppressed")
            record("ram_write_requested", address=hex(address), bytes=len(data))
            write_command(device, 4, relative, data)
            require(ram_read(device, address, len(data)) == data, "RAM verification failed")
            record("ram_write_verified", address=hex(address))

        try:
            report["restart_required"] = True
            record("restart_armed")
            ram_write(POINTER, struct.pack("<I", DISABLED_FLASH_BASE), initial=True)
            ram_write(SCRATCH, TABLE)
            ram_write(SLOT, struct.pack("<I", SCRATCH))
            record("native_table_installed")
            captures = folder / ("captures-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
            captures.mkdir()
            backend = NativeBackend(device, record, flash, captures)
            if args.recover:
                record("restoration_armed")
                restore_sector(backend, flash[RELATIVE:RELATIVE + SECTOR_SIZE], flash)
                record("physical_backup_restored", sha256=sha(flash))
                for pos in range(RELATIVE, RELATIVE + SECTOR_SIZE, 512):
                    ram_write(RAM_BASE + pos, ram[pos:pos + 512])
            else:
                run_experiment(backend, flash, record)
            report["status"] = "physical_backup_verified"
        except BaseException as error:
            report.update(status="interrupted", error=f"{type(error).__name__}: {error}")
            record("interrupted")
            raise
        finally:
            if ram_read(device, POINTER, 4) == struct.pack("<I", DISABLED_FLASH_BASE):
                ram_write(SLOT, bytes(4))
                ram_write(SCRATCH, bytes(16))
                record("table_cleared_restart_required")
            else:
                require(ram_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE), "Unknown pointer")
                require(ram_read(device, SLOT, 4) == bytes(4), "Unexpected registered callback")
                report["restart_required"] = False
                record("original_pointer_unchanged")
    print("Restart through O with USB disconnected for 10 seconds, then U; run verify_recovery.py.", flush=True)


if __name__ == "__main__":
    main()
