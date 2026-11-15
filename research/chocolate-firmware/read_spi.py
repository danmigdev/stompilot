"""Suspended V13 direct-SPI diagnostic through a temporary RAM callback.

Default: inspect local files only. --execute temporarily registers memory type
5 using 96 zero bytes in disabled page 8's unused SysEx padding, then restores
all RAM. Installation writes are suppressed by the stock flash size guard.
--pattern also tests/restores the same 32 padding bytes as cycle_probe.py.
The first installation attempt lost USB before any SPI result was recorded.
Device execution is disabled pending an explanation and a validated recovery.
No erase, firmware programming, reset or bootloader operation is implemented.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import struct
import time

from capture_persistence import (HERE, RAM_BASE, APP_BASE, FLASH_BASE, FLASH_VIEW,
                                 RESERVATION_SIZE, UsbChocolate, list_usb_devices,
                                 capture, require, sha, RELEASES)
from midi_configurator.chocolate import PAGE_COUNT_OFFSET

SCRATCH, SCRATCH_SIZE = 0x16810, 96  # page 8 D bank A, length byte stays zero
SLOT = 0x19774                     # initially unregistered memory type 5
POINTER = 0x19100
DISABLED_FLASH_BASE = 0x100000      # no 32-bit wrap; greater than 256-KiB capacity
SPI_READ = 0x1CF66
PATTERN_OFFSET, PATTERN_LENGTH = 13405, 32
CONTROL_HEX = "60e0640bc3ff00910100b060014e4020c3ff74970100b060c0ff1068010041205a20c3ff807f1100d3008000"


def ram_read(device, address, size):
    regions = ((RAM_BASE, 23646), (POINTER, 4), (SLOT, 4), (0x193B3, 1),
               (0x1E0CD20, 0x7C), (0x1E13B0E, 0xEA), (0x1E13A24, 0x44),
               (FLASH_VIEW, RESERVATION_SIZE))
    require(size > 0 and any(a <= address < address + size <= a + n for a, n in regions),
            "Read outside fixed diagnostic regions")
    out = bytearray()
    while len(out) < size:
        n = min(512, size - len(out))
        prefix = b"\x04" + (address + len(out) - RAM_BASE).to_bytes(4, "little") + n.to_bytes(3, "little")
        data = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
        require(len(data) == n, "Incomplete RAM/mapped read")
        out.extend(data)
    return bytes(out)


def write_command(device, kind, address, data):
    prefix = bytes([kind]) + address.to_bytes(4, "little") + len(data).to_bytes(3, "little")
    status = device._request(0x22, prefix + data, 0x00)["payload"]
    require(bool(status) and status[0] == 0, "Device rejected diagnostic write")


def spi_read(device, address, size):
    require(FLASH_BASE <= address < address + size <= FLASH_BASE + RESERVATION_SIZE,
            "Direct SPI diagnostic is restricted to USRFLASH")
    out = bytearray()
    while len(out) < size:
        n = min(512, size - len(out))
        prefix = b"\x05" + (address + len(out)).to_bytes(4, "little") + n.to_bytes(3, "little")
        data = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
        require(len(data) == n, "Incomplete direct SPI response")
        out.extend(data)
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pattern", action="store_true")
    args = parser.parse_args()
    if args.execute:
        parser.error("Device execution is suspended: the 2026-10-27 RAM probe lost USB. "
                     "Use verify_recovery.py after a physical O -> U restart; "
                     "do not retry this installation.")
    require(not args.pattern or args.execute, "Pattern test requires explicit execution")
    code = (HERE / "artifacts/spi-probe-build/control.bin").read_bytes()
    require(code == bytes.fromhex(CONTROL_HEX), "RAM control code differs from reviewed instructions")
    table = struct.pack("<4I", 0, SPI_READ, SCRATCH + 16, 0)
    blob = (table + code).ljust(SCRATCH_SIZE, b"\0")
    require(len(blob) == SCRATCH_SIZE, "RAM control payload too large")
    ref = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(ref) == RELEASES["013"]["app_sha256"], "Wrong application reference")
    if not args.execute:
        print("Offline checks passed. Control code 44 bytes; scratch 96 bytes; no device I/O.")
        return
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    folder = HERE / "artifacts/spi-probes" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True)
    report = dict(status="preparing", pattern_test=args.pattern, events=[], firmware_writes=0,
                  install_persistence="Stock write guard rejects all installation addresses above capacity")
    def event(name, **details):
        report["events"].append(dict(event=name, **details))
        (folder / "spi-probe.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(name, json.dumps(details), flush=True)
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        baseline_ram, baseline_flash, _ = capture(device, folder / "baseline")
        require(baseline_ram == baseline_flash[:23646], "Baseline RAM/flash mismatch")
        require(baseline_ram[PAGE_COUNT_OFFSET] < 7, "Page 8 must be disabled")
        require(baseline_ram[SCRATCH - RAM_BASE:SCRATCH - RAM_BASE + SCRATCH_SIZE] == bytes(SCRATCH_SIZE),
                "Scratch padding is not empty")
        require(baseline_ram[93 + 31 * 417 + 161] == 0 and
                baseline_ram[PATTERN_OFFSET:PATTERN_OFFSET + PATTERN_LENGTH] == bytes(PATTERN_LENGTH),
                "The inactive SysEx length/pattern area is not zero")
        require(ram_read(device, SLOT, 4) == bytes(4), "Memory type 5 is already used")
        # Context 3 only clamps the current advanced page and refreshes its display.
        # With an already-valid page it does not alter the saved structure.
        require(ram_read(device, 0x193B3, 1) in (b"\xff", b"\x03") and
                baseline_ram[23640] <= baseline_ram[23639], "Unsupported controller context/page")
        for address, n in ((0x1E0CD20, 0x7C), (0x1E13B0E, 0xEA), (0x1E13A24, 0x44)):
            require(ram_read(device, address, n) == ref[address - APP_BASE:address - APP_BASE + n],
                    "Stock flash guard or write callback differs")

        def install_write(address, data):
            require((address, data) in ((POINTER, struct.pack("<I", DISABLED_FLASH_BASE)),
                                        (SCRATCH, blob), (SLOT, struct.pack("<I", SCRATCH))),
                    "Unexpected installation write")
            if address != POINTER:
                require(ram_read(device, POINTER, 4) == struct.pack("<I", DISABLED_FLASH_BASE),
                        "Installation flash suppression is not active")
            write_command(device, 4, address - RAM_BASE, data)
            require(ram_read(device, address, len(data)) == data, "RAM install verification failed")

        started = False
        pattern_attempted = False
        raw_baseline = None
        try:
            started = True
            install_write(POINTER, struct.pack("<I", DISABLED_FLASH_BASE))
            install_write(SCRATCH, blob)
            install_write(SLOT, struct.pack("<I", SCRATCH))
            # Fixed callback operation 0 restores normal persistence without removing the reader.
            write_command(device, 5, 0, b"\0")
            require(ram_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE), "Pointer not restored")
            event("ram_reader_installed")
            raw_baseline = spi_read(device, FLASH_BASE, RESERVATION_SIZE)
            (folder / "spi-baseline.bin").write_bytes(raw_baseline)
            event("direct_spi_baseline", sha256=sha(raw_baseline), matches_mapping=raw_baseline == baseline_flash,
                  marker=raw_baseline[23652:23660].hex())
            if args.pattern:
                require(raw_baseline == baseline_flash, "Raw SPI baseline differs; do not write the pattern")
                pattern_attempted = True
                device._write(PATTERN_OFFSET, b"\xff" * PATTERN_LENGTH)
                began = time.monotonic()
                for index, delay in enumerate((0, 0.01, 0.1, 0.5, 1.0)):
                    if delay:
                        time.sleep(delay)
                    raw = spi_read(device, FLASH_BASE + PATTERN_OFFSET, PATTERN_LENGTH)
                    mapped = ram_read(device, FLASH_VIEW + PATTERN_OFFSET, PATTERN_LENGTH)
                    working = device._read(PATTERN_OFFSET, PATTERN_LENGTH)
                    event("pattern_read", index=index, elapsed_seconds=round(time.monotonic() - began, 4),
                          spi=raw.hex(), mapped=mapped.hex(), ram=working.hex(),
                          cleared_spi_bits=sum(8 - b.bit_count() for b in raw))
        finally:
            if started:
                # If a prior ACK was lost, inspect state and finish installing the fixed cleanup callback.
                slot = ram_read(device, SLOT, 4)
                if slot == bytes(4):
                    require(ram_read(device, POINTER, 4) == struct.pack("<I", DISABLED_FLASH_BASE),
                            "Unexpected recovery state: power-cycle through O before further work")
                    install_write(SCRATCH, blob)
                    install_write(SLOT, struct.pack("<I", SCRATCH))
                require(ram_read(device, SLOT, 4) == struct.pack("<I", SCRATCH) and
                        ram_read(device, SCRATCH, SCRATCH_SIZE) == blob, "Cleanup callback is not intact")
                write_command(device, 5, 0, b"\0")
                if pattern_attempted:
                    device._write(PATTERN_OFFSET, bytes(PATTERN_LENGTH))
                    restored_raw = spi_read(device, FLASH_BASE, RESERVATION_SIZE)
                    (folder / "spi-restored.bin").write_bytes(restored_raw)
                    report["raw_spi_restored"] = restored_raw == raw_baseline
                    event("pattern_restored", entire_spi_matches_backup=report["raw_spi_restored"])
                # Operation 1 unregisters itself and tail-jumps into ROM memset to clear its own RAM.
                write_command(device, 5, 1, b"\0")
                require(ram_read(device, SLOT, 4) == bytes(4) and
                        ram_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE) and
                        ram_read(device, SCRATCH, SCRATCH_SIZE) == bytes(SCRATCH_SIZE), "RAM cleanup failed")
                final_ram, final_flash, _ = capture(device, folder / "restored")
                report.update(status="restored", ram_restored=final_ram == baseline_ram,
                              flash_mapping_restored=final_flash == baseline_flash)
                event("ram_reader_removed", ram_restored=report["ram_restored"],
                      flash_mapping_restored=report["flash_mapping_restored"])
                require(report["ram_restored"] and report["flash_mapping_restored"], "Final backup comparison differs")
                require(report.get("raw_spi_restored", True), "Direct SPI restoration differs from backup")
    print(f"Saved: {folder}", flush=True)


if __name__ == "__main__":
    main()
