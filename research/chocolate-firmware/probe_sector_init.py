"""Fixed-sector test via stock one-argument erase and command 0x21.

Default is an offline plan. --prepare verifies a no-op command before erasing
only sector 0x3C000, then programs the pinned image with 32 FF padding bytes.
--restore SESSION erases/restores that same sector from the pinned backup.
Both actions remove their data table and require a physical restart to restore
the RAM persistence pointer. No executable payload, firmware update or 0x24.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

from capture_persistence import (HERE, APP_BASE, RAM_BASE, FLASH_BASE,
                                 RESERVATION_SIZE, UsbChocolate, list_usb_devices,
                                 capture, require, sha, RELEASES)
from read_spi import (POINTER, SLOT, DISABLED_FLASH_BASE, SPI_READ, ram_read,
                      spi_read, write_command)
from probe_sector_erase import (SECTOR, SECTOR_SIZE, PAGE_SIZE, RELATIVE,
                                SCRATCH, SCRATCH_OFFSET, OFFSET, LENGTH,
                                baseline, pattern_image, program_image)
from midi_configurator.chocolate import PAGE_COUNT_OFFSET

ROOT = HERE / "artifacts/sector-init-probes"
ERASE_UNIT = 0x193DB
ERASE_ONE = 0x1E0CCF6
PROGRAM = 0x1E0CD8E
TABLE = struct.pack("<4I", ERASE_ONE, SPI_READ, PROGRAM, 0)
CODE_RANGES = ((0x1E0CA70, 0x33C), (0x1E13C4E, 0x1C6), (0x1E18A28, 24),
               (0x1E13B0E, 0xEA), (0x1E13A24, 0x44))


def native_read(device, address, size):
    if address == ERASE_UNIT and size == 1 or any(a <= address < address + size <= a + n
                                               for a, n in CODE_RANGES):
        data = bytearray()
        while len(data) < size:
            length = min(512, size - len(data))
            prefix = b"\x04" + struct.pack("<I", address + len(data) - RAM_BASE) + length.to_bytes(3, "little")
            part = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
            require(len(part) == length, "Incomplete audited native read")
            data.extend(part)
        return bytes(data)
    return ram_read(device, address, size)


def reference():
    ref = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(ref) == RELEASES["013"]["app_sha256"], "Wrong V13 application")
    require(ref[ERASE_ONE - APP_BASE:ERASE_ONE - APP_BASE + 0x2A] == bytes.fromhex(
        "7404c1ffa093010050ee1b134221b1e81000422022ac44e00001b1e8010024164116bfea68ff40165404"),
        "One-argument wrapper differs from reviewed instructions")
    require(ref[0x1E13CFA - APP_BASE:0x1E13D0A - APP_BASE] == bytes.fromhex(
        "0484a1166216bfeaf2d8412880164482"), "Command-21 fixed ACK route differs")
    masks, kinds = struct.unpack("<3I", ref[0x1E18A28 - APP_BASE:0x1E18A34 - APP_BASE]), \
                   struct.unpack("<3I", ref[0x1E18A34 - APP_BASE:0x1E18A40 - APP_BASE])
    require(masks[1] == 0xFFFFF000 and kinds[1] == 200, "Stock 4-KiB erase map differs")
    require(TABLE[12:] == bytes(4), "Command-24 callback must remain absent")
    return ref


def expected_pattern(flash):
    return flash[:RELATIVE] + pattern_image(flash[RELATIVE:RELATIVE + SECTOR_SIZE]) + flash[RELATIVE + SECTOR_SIZE:]


def require_outside_matches(actual, original):
    require(len(actual) == len(original), "Wrong capture length")
    require(actual[:RELATIVE] == original[:RELATIVE] and
            actual[RELATIVE + SECTOR_SIZE:] == original[RELATIVE + SECTOR_SIZE:],
            "Changes outside the fixed sector; no erase authorized")


class InitBackend:
    def __init__(self, device, record, ram_write, flash, captures):
        self.device, self.record, self.ram_write = device, record, ram_write
        self.flash, self.captures, self.counter = flash, captures, 0
        self.dispatch_checked = False
        self.erased_blank = False

    def read(self, address, size):
        data = spi_read(self.device, address, size)
        self.counter += 1
        name = f"spi-{self.counter:03d}-{address:06x}-{size}.bin"
        (self.captures / name).write_bytes(data)
        self.record("spi_capture", file=name, sha256=sha(data))
        return data

    def verify_table(self):
        require(native_read(self.device, POINTER, 4) == struct.pack("<I", DISABLED_FLASH_BASE) and
                native_read(self.device, SLOT, 4) == struct.pack("<I", SCRATCH) and
                native_read(self.device, SCRATCH, 16) == TABLE, "Unexpected diagnostic controls")

    def command21(self):
        self.verify_table()
        status = self.device._request(0x21, b"\x05" + struct.pack("<I", SECTOR), 0)["payload"]
        require(status == b"\0", "Unexpected command-21 ACK")

    def check_dispatch(self, expected):
        require(not self.dispatch_checked, "Dispatch already checked")
        require(native_read(self.device, ERASE_UNIT, 1) == b"\0", "No-op requires zero erase-unit flag")
        self.record("noop_command21_requested", physical_address=hex(SECTOR), erase_unit=0)
        self.command21()
        first = self.read(FLASH_BASE, RESERVATION_SIZE)
        second = self.read(FLASH_BASE, RESERVATION_SIZE)
        require(first == second == expected, "No-op altered the physical configuration")
        require(native_read(self.device, ERASE_UNIT, 1) == b"\0", "No-op changed erase-unit flag")
        self.dispatch_checked = True
        self.record("noop_command21_verified", full_physical_image_unchanged=True)

    def erase(self, address):
        require(address == SECTOR and self.dispatch_checked, "Erase outside fixed plan or untested dispatch")
        self.verify_table()
        require(native_read(self.device, ERASE_UNIT, 1) == b"\0", "Original erase-unit flag differs")
        self.erased_blank = False
        try:
            self.ram_write(ERASE_UNIT, b"\x10")
            require(native_read(self.device, ERASE_UNIT, 1) == b"\x10", "4-KiB unit did not verify")
            self.record("erase_requested", physical_address=hex(SECTOR), bytes=SECTOR_SIZE, command="0x21")
            self.command21()  # No automatic retry after an uncertain acknowledgement.
            self.record("erase_acknowledged")
        finally:
            self.ram_write(ERASE_UNIT, b"\0")
        first = self.read(SECTOR, SECTOR_SIZE)
        second = self.read(SECTOR, SECTOR_SIZE)
        require(first == second == b"\xff" * SECTOR_SIZE, "Entire sector did not verify blank twice")
        self.erased_blank = True
        self.record("sector_blank_verified")

    def program(self, address, data):
        require(address % PAGE_SIZE == 0 and SECTOR <= address < SECTOR + SECTOR_SIZE and
                len(data) == PAGE_SIZE, "Program outside fixed sector/page")
        pos = address - FLASH_BASE
        allowed = (self.flash[pos:pos + PAGE_SIZE], expected_pattern(self.flash)[pos:pos + PAGE_SIZE])
        require(data in allowed, "Page differs from pinned backup/pattern")
        require(native_read(self.device, ERASE_UNIT, 1) == b"\0", "Erase-unit flag not restored")
        self.verify_table()
        self.record("page_program_requested", physical_address=hex(address), sha256=sha(data))
        write_command(self.device, 5, address, data)
        self.record("page_program_acknowledged", physical_address=hex(address))

    def verify_full(self, expected, event):
        first = self.read(FLASH_BASE, RESERVATION_SIZE)
        second = self.read(FLASH_BASE, RESERVATION_SIZE)
        self.record(event, first_sha256=sha(first), second_sha256=sha(second),
                    first_pattern=first[OFFSET:OFFSET + LENGTH].hex(),
                    second_pattern=second[OFFSET:OFFSET + LENGTH].hex(), matches=first == second == expected)
        require(first == second == expected, "Complete physical image differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--restore", type=Path)
    args = parser.parse_args()
    ref = reference()
    from trace_init_erase import trace
    noop_trace, erase_trace = trace(0), trace(16)
    require(noop_trace["events"] == [] and noop_trace["result"] == 0 and
            [e for e in erase_trace["events"] if e["operation"] == "erase"] ==
            [dict(operation="erase", kind=2, address=SECTOR)] and erase_trace["result"] == SECTOR_SIZE,
            "Offline wrapper trace differs from the fixed erase plan")
    ram, flash = baseline()
    desired = expected_pattern(flash)
    if not args.prepare and not args.restore:
        print(json.dumps(dict(device_io=False, sector=hex(SECTOR), bytes=SECTOR_SIZE, command="0x21",
                              original_erase_unit=0, active_erase_unit=16,
                              noop_verified_before_erase=True, executable_payload_bytes=0,
                              uses_command24=False, prepare_leaves_only_32_inactive_ff_bytes=True,
                              restore="--restore SESSION; both stages require a physical restart"), indent=2))
        return
    if args.restore:
        folder = args.restore.resolve()
        require(folder.parent == ROOT.resolve(), "Wrong restore directory")
        report = json.loads((folder / "report.json").read_text())
        require(report["baseline_sha256"] == sha(flash) and report["physical_sector"] == SECTOR and
                (folder / "usrflash-backup.bin").read_bytes() == flash and
                (folder / "ram-backup.bin").read_bytes() == ram, "Wrong session/backup")
    else:
        require(not any(json.loads(p.read_text()).get("restoration_pending") for p in ROOT.glob("*/report.json")),
                "A preceding session still needs restoration")
        folder = ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        folder.mkdir(parents=True)
        (folder / "ram-backup.bin").write_bytes(ram)
        (folder / "usrflash-backup.bin").write_bytes(flash)
        report = dict(status="preflight", baseline_sha256=sha(flash), physical_sector=SECTOR,
                      firmware_writes=0, executable_payload_bytes=0, command24_requests=0,
                      restoration_pending=False, restart_required=False, events=[])

    def record(event, **details):
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), event=event, **details))
        temp = folder / "report.tmp"
        temp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temp.replace(folder / "report.json")
        print(event, json.dumps(details), flush=True)

    print(f"Session: {folder}", flush=True)
    record("started", action="restore" if args.restore else "prepare")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        current_ram, current_flash, _ = capture(device, folder / ("before-" + stamp))
        if args.restore:
            require_outside_matches(current_ram, ram)
            require_outside_matches(current_flash, flash)
        else:
            require(current_ram == ram and current_flash == flash, "Baseline differs; no mutation")
        require(current_ram[0] == 3 and current_ram[PAGE_COUNT_OFFSET] < 6 and current_ram[23640] == 0 and
                current_ram[93 + 27 * 417 + 161] == 0 and
                current_ram[SCRATCH_OFFSET:SCRATCH_OFFSET + 16] == bytes(16), "Scratch/page/context mismatch")
        require(native_read(device, SLOT, 4) == bytes(4) and native_read(device, ERASE_UNIT, 1) == b"\0" and
                native_read(device, 0x193B3, 1) == b"\x03", "Unexpected control state")
        for address, size in CODE_RANGES:
            require(native_read(device, address, size) == ref[address - APP_BASE:address - APP_BASE + size],
                    "Stock erase/dispatch/program code differs")
        record("preflight_verified")

        def ram_write(address, data, initial=False):
            allowed = (address, data) in ((POINTER, struct.pack("<I", DISABLED_FLASH_BASE)),
                       (SLOT, struct.pack("<I", SCRATCH)), (SLOT, bytes(4)),
                       (SCRATCH, TABLE), (SCRATCH, bytes(16)), (ERASE_UNIT, b"\0"), (ERASE_UNIT, b"\x10"))
            relative = address - RAM_BASE
            padding = (relative == OFFSET and data in (bytes(LENGTH), b"\xff" * LENGTH))
            restoring = (RELATIVE <= relative < relative + len(data) <= RELATIVE + SECTOR_SIZE and
                         0 < len(data) <= 512 and data == ram[relative:relative + len(data)])
            require(allowed or padding or restoring, "RAM write outside fixed plan")
            require(native_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE if initial else DISABLED_FLASH_BASE),
                    "Unexpected persistence pointer")
            require(relative >= 0 and DISABLED_FLASH_BASE + relative + len(data) > 0x40000,
                    "Persistence guard would not reject this write")
            record("ram_write_requested", address=hex(address), bytes=len(data), data=data.hex() if len(data) <= 16 else None)
            write_command(device, 4, relative, data)
            require(native_read(device, address, len(data)) == data, "RAM readback differs")
            record("ram_write_verified", address=hex(address))

        backend = None
        mutated_flash = False
        try:
            report["restart_required"] = True
            record("restart_armed")
            ram_write(POINTER, struct.pack("<I", DISABLED_FLASH_BASE), initial=True)
            ram_write(SCRATCH, TABLE)
            ram_write(SLOT, struct.pack("<I", SCRATCH))
            captures = folder / ("spi-" + stamp)
            captures.mkdir()
            backend = InitBackend(device, record, ram_write, flash, captures)
            backend.check_dispatch(current_flash)
            report["restoration_pending"] = True
            mutated_flash = True
            record("erase_armed")
            backend.erase(SECTOR)
            image = flash if args.restore else desired
            program_image(backend, image[RELATIVE:RELATIVE + SECTOR_SIZE])
            backend.verify_full(image, "backup_verified" if args.restore else "pattern_verified")
            if args.restore:
                for pos in range(RELATIVE, RELATIVE + SECTOR_SIZE, 512):
                    ram_write(RAM_BASE + pos, ram[pos:pos + 512])
                report.update(restoration_pending=False, status="restored_pending_restart")
            else:
                ram_write(RAM_BASE + OFFSET, b"\xff" * LENGTH)
                report.update(status="pattern_verified_pending_restart", pattern_sha256=sha(desired))
            record("stage_complete")
        except BaseException as error:
            report.update(status="interrupted", error=f"{type(error).__name__}: {error}")
            record("interrupted")
            # Restore after a failed test only if the connection and audited
            # table still work. Never retry the failed command automatically.
            if mutated_flash and backend is not None:
                try:
                    backend.verify_table()
                    require(native_read(device, ERASE_UNIT, 1) == b"\0", "Erase-unit flag needs physical restart")
                    record("failure_recovery_armed")
                    if backend.erased_blank:
                        backend.erase(SECTOR)
                        program_image(backend, flash[RELATIVE:RELATIVE + SECTOR_SIZE])
                    else:
                        # An unacknowledged erase is never sent again here.
                        first = backend.read(FLASH_BASE, RESERVATION_SIZE)
                        second = backend.read(FLASH_BASE, RESERVATION_SIZE)
                        require(first == second, "Unstable failure capture")
                        require_outside_matches(first, flash)
                        if first != flash:
                            require(first[RELATIVE:RELATIVE + SECTOR_SIZE] == b"\xff" * SECTOR_SIZE,
                                    "Unconfirmed erase left a nonblank sector; preserved for recovery")
                            program_image(backend, flash[RELATIVE:RELATIVE + SECTOR_SIZE])
                    backend.verify_full(flash, "failure_backup_verified")
                    for pos in range(RELATIVE, RELATIVE + SECTOR_SIZE, 512):
                        ram_write(RAM_BASE + pos, ram[pos:pos + 512])
                    report.update(restoration_pending=False, status="test_failed_backup_restored_pending_restart")
                    record("failure_recovery_complete")
                except BaseException as recovery_error:
                    report["recovery_error"] = f"{type(recovery_error).__name__}: {recovery_error}"
                    record("failure_recovery_incomplete")
            raise
        finally:
            try:
                if native_read(device, POINTER, 4) == struct.pack("<I", DISABLED_FLASH_BASE):
                    ram_write(ERASE_UNIT, b"\0")
                    ram_write(SLOT, bytes(4))
                    ram_write(SCRATCH, bytes(16))
                    expected_ram = ram if not report["restoration_pending"] else desired[:len(ram)]
                    if report["status"] in ("restored_pending_restart", "pattern_verified_pending_restart",
                                             "test_failed_backup_restored_pending_restart"):
                        require(device.read_config() == expected_ram, "Final RAM differs from the intended stage")
                    record("temporary_table_cleared_restart_required")
            except BaseException as cleanup_error:
                report["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                record("cleanup_incomplete_restart_required")
                raise
    print("Stage complete. Physical O / unplug USB for 10 seconds / U / reconnect required.")


if __name__ == "__main__":
    main()
