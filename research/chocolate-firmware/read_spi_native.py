"""Read USRFLASH through the original SPI routine; never execute injected code.

Default: local plan only. --execute installs a 16-byte DATA-ONLY callback table,
with only the stock SPI read function populated. Configuration persistence is
temporarily suppressed by the audited stock capacity guard. The table and slot
are then cleared. A physical O/USB-disconnect/U restart is REQUIRED to restore
the USRFLASH pointer; verify_recovery.py must confirm the complete backup.
--ff-byte additionally writes FF through the original configuration protocol at
the already tested inactive byte. It compares the direct and mapped reads, then
requires --restore-after-restart SESSION after the physical restart. The direct
SPI callback remains read-only. No erase, firmware update or executable payload.
--full-flash reads the complete 256-KiB physical flash twice through that same
read-only callback, including boot/calibration/VM data. It is mutually exclusive
with --ff-byte. It still requires a physical restart to restore the RAM pointer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

from capture_persistence import (HERE, RAM_BASE, APP_BASE, FLASH_BASE, FLASH_VIEW,
                                 RESERVATION_SIZE, CONFIG_SIZE, UsbChocolate,
                                 list_usb_devices, capture, read_audited, require, sha, RELEASES)
from read_spi import (POINTER, SLOT, SCRATCH, DISABLED_FLASH_BASE, SPI_READ,
                      ram_read, write_command, spi_read)
from retention_probe import RAM_SHA, FLASH_SHA
from midi_configurator.chocolate import PAGE_COUNT_OFFSET
from retention_probe import require_inactive
from probe_restore_bit import OFFSET

TABLE = struct.pack("<4I", 0, SPI_READ, 0, 0)
PHYSICAL_FLASH_SIZE = 0x40000


def read_full_flash(device, output, progress):
    """Fixed physical extent, 512-byte read packets, no general address interface."""
    out = bytearray()
    while len(out) < PHYSICAL_FLASH_SIZE:
        address, size = len(out), 512
        prefix = b"\x05" + address.to_bytes(4, "little") + size.to_bytes(3, "little")
        data = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
        require(len(data) == size, "Incomplete full physical flash response")
        output.write(data)
        out.extend(data)
        if len(out) % 32768 == 0:
            progress(len(out))
    return bytes(out)


def restore_after_restart(folder):
    folder = folder.resolve()
    require(folder.parent == (HERE / "artifacts/native-spi-probes").resolve(), "Wrong session directory")
    report = json.loads((folder / "report.json").read_text())
    require(report.get("ff_byte") and report.get("pattern_restore_pending"), "No pending FF byte")
    ram = (folder / "baseline/ram-before.bin").read_bytes()
    flash = (folder / "baseline/usrflash-first.bin").read_bytes()
    require(sha(ram) == RAM_SHA and sha(flash) == FLASH_SHA, "Wrong backup")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        from capture_persistence import audit
        audit(device)  # Includes the restored flash pointer and stock code.
        require(ram_read(device, SLOT, 4) == bytes(4) and
                ram_read(device, SCRATCH, 16) == bytes(16), "Temporary table remains")
        current = device.read_config()
        require_inactive(current, empty=False)
        report["after_restart_ram_byte"] = current[OFFSET:OFFSET + 1].hex()
        report["after_restart_mapped_byte"] = read_audited(device, FLASH_VIEW + OFFSET, 1).hex()
        (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        device._write(OFFSET, b"\0")
        final_ram, final_flash, _ = capture(device, folder / ("restored-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")))
        require(final_ram == ram and final_flash == flash, "Full restoration differs from backup")
        report.update(status="recovered_verified", restart_required=False,
                      pattern_restore_pending=False, restored_ram_matches_backup=True,
                      restored_flash_matches_backup=True)
        (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ff-byte", action="store_true")
    parser.add_argument("--full-flash", action="store_true")
    parser.add_argument("--restore-after-restart", type=Path)
    args = parser.parse_args()
    require(not (args.ff_byte and args.full_flash), "Full backup and FF-byte test are separate operations")
    if args.restore_after_restart:
        require(not args.execute and not args.ff_byte and not args.full_flash, "Restoration is a separate action")
        restore_after_restart(args.restore_after_restart)
        return
    ref = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(ref) == RELEASES["013"]["app_sha256"], "Reference mismatch")
    require(0x1BD4C <= SPI_READ < 0x1E3AC, "Reader outside the stock RAM execution range")
    require(TABLE == bytes.fromhex("0000000066cf01000000000000000000"), "Unexpected table")
    # Every native type-4 installation/cleanup write is above physical capacity.
    for address, size in ((POINTER, 4), (SCRATCH, 16), (SLOT, 4), (RAM_BASE + OFFSET, 1)):
        require(0x40000 < DISABLED_FLASH_BASE + address - RAM_BASE < 0xFFFFFFFF - size,
                "Installation is not rejected by the capacity guard")
    if not args.execute:
        print("Plan checked: 16 data bytes, stock SPI read callback only, no executable payload.")
        print("No device I/O. Execution requires a physical restart afterward to restore the flash pointer.")
        if args.ff_byte:
            print("FF-byte plan: one native configuration-byte write; physical restoration after restart.")
        if args.full_flash:
            print("Full backup plan: two 262144-byte physical reads, no flash changes.")
        return
    folder = HERE / "artifacts/native-spi-probes" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True)
    report = dict(status="preflight", table=TABLE.hex(), executable_payload_bytes=0,
                  firmware_writes=0, direct_spi_write_commands=0, restart_required=False,
                  ff_byte=args.ff_byte, full_flash=args.full_flash, pattern_restore_pending=False, events=[])

    def event(name, **extra):
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), event=name, **extra))
        pending = folder / "report.tmp"
        pending.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        pending.replace(folder / "report.json")
        print(name, json.dumps(extra), flush=True)

    print(f"Saved: {folder}", flush=True)
    event("created")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        ram, flash, _ = capture(device, folder / "baseline")
        require(sha(ram) == RAM_SHA and sha(flash) == FLASH_SHA, "Wrong baseline; no mutation")
        require(ram[PAGE_COUNT_OFFSET] < 7 and ram[93 + 31 * 417 + 161] == 0 and
                ram[SCRATCH - RAM_BASE:SCRATCH - RAM_BASE + 16] == bytes(16), "Scratch is not inactive padding")
        require(ram_read(device, SLOT, 4) == bytes(4), "Type 5 already in use")
        require(ram_read(device, 0x193B3, 1) in (b"\xff", b"\x03") and
                ram[23640] <= ram[23639], "Context/page could trigger another save")
        for address, size in ((0x1E0CD20, 0x7C), (0x1E13B0E, 0xEA), (0x1E13A24, 0x44)):
            require(ram_read(device, address, size) == ref[address - APP_BASE:address - APP_BASE + size],
                    "Write path differs from the audited stock guard")
        event("preflight_verified")

        def suppressed_write(address, data, initial=False):
            allowed = ((POINTER, struct.pack("<I", DISABLED_FLASH_BASE)),
                       (SCRATCH, TABLE), (SCRATCH, bytes(16)),
                       (SLOT, struct.pack("<I", SCRATCH)), (SLOT, bytes(4)),
                       (RAM_BASE + OFFSET, b"\0"))
            require((address, data) in allowed, "Write is outside the fixed RAM plan")
            expected_pointer = FLASH_BASE if initial else DISABLED_FLASH_BASE
            require(ram_read(device, POINTER, 4) == struct.pack("<I", expected_pointer), "Unexpected pointer")
            event("ram_write_requested", address=hex(address), data=data.hex())
            write_command(device, 4, address - RAM_BASE, data)
            require(ram_read(device, address, len(data)) == data, "RAM write did not verify")
            event("ram_write_verified", address=hex(address))

        try:
            if args.ff_byte:
                report["pattern_restore_pending"] = True
                event("native_ff_byte_requested", offset=OFFSET)
                device._write(OFFSET, b"\xff")
                event("native_ff_byte_acknowledged", ram=device._read(OFFSET, 1).hex(),
                      mapped=read_audited(device, FLASH_VIEW + OFFSET, 1).hex())
            report.update(status="installing_data_table", restart_required=True)
            event("restart_armed")
            suppressed_write(POINTER, struct.pack("<I", DISABLED_FLASH_BASE), initial=True)
            suppressed_write(SCRATCH, TABLE)
            suppressed_write(SLOT, struct.pack("<I", SCRATCH))
            event("data_table_installed")
            # First read is just the known marker; stop before a full capture if
            # the response is implausible. This is diagnostic, not an erase guard.
            marker = spi_read(device, FLASH_BASE + 23652, 8)
            (folder / "spi-marker.bin").write_bytes(marker)
            event("direct_marker", hex=marker.hex(), matches_plain_marker=marker == b"usrdata\0")
            require(marker == b"usrdata\0", "Raw marker differs; preserve result and stop")
            first = spi_read(device, FLASH_BASE, RESERVATION_SIZE)
            (folder / "spi-first.bin").write_bytes(first)
            event("direct_first", sha256=sha(first), matches_baseline=first == flash)
            second = spi_read(device, FLASH_BASE, RESERVATION_SIZE)
            (folder / "spi-second.bin").write_bytes(second)
            report.update(spi_stable=first == second, spi_matches_baseline=first == second == flash,
                          spi_matches_mapping=first == second == flash)
            event("direct_second", sha256=sha(second), matches_first=first == second)
            if args.full_flash:
                require(first == second == flash, "USRFLASH preflight differs; no full backup attempted")
                physical_reads = []
                for label in ("first", "second"):
                    with (folder / f"flash-physical-{label}.bin").open("xb") as output:
                        physical = read_full_flash(device, output,
                            lambda count: print(f"Physical flash {label}: {count}/{PHYSICAL_FLASH_SIZE}", flush=True))
                    require(physical[FLASH_BASE:FLASH_BASE + RESERVATION_SIZE] == flash,
                            "Physical backup configuration differs from baseline")
                    physical_reads.append(physical)
                    event("physical_flash_read", copy=label, bytes=len(physical), sha256=sha(physical))
                require(physical_reads[0] == physical_reads[1], "Full physical snapshots differ; preserve both")
                report.update(full_flash_stable=True, full_flash_sha256=sha(physical_reads[0]),
                              full_flash_bytes=PHYSICAL_FLASH_SIZE)
            if args.ff_byte:
                mapped = read_audited(device, FLASH_VIEW, RESERVATION_SIZE)
                (folder / "mapped-with-pattern.bin").write_bytes(mapped)
                report["spi_matches_mapping"] = first == second == mapped
                report["pattern_comparison"] = dict(
                    requested="ff", direct_first=first[OFFSET:OFFSET + 1].hex(),
                    direct_second=second[OFFSET:OFFSET + 1].hex(), mapped=mapped[OFFSET:OFFSET + 1].hex(),
                    direct_changes_outside_target=sum(a != b for i, (a, b) in enumerate(zip(first, flash)) if i != OFFSET))
                event("pattern_comparison", **report["pattern_comparison"])
        except BaseException as error:
            report["error"] = f"{type(error).__name__}: {error}"
            event("interrupted")
            raise
        finally:
            # No custom cleanup callback. Original type-4 writes remain usable
            # while the pointer suppresses physical writes. Restart restores it.
            pointer = ram_read(device, POINTER, 4)
            if pointer == struct.pack("<I", FLASH_BASE):
                # Failure before installing the data table: normal persistence
                # is still available, so restore the pattern immediately.
                if report["pattern_restore_pending"]:
                    device._write(OFFSET, b"\0")
                    report["pattern_restore_pending"] = False
                require(ram_read(device, SLOT, 4) == bytes(4) and
                        ram_read(device, SCRATCH, 16) == bytes(16), "Unexpected temporary RAM")
                report["restart_required"] = False
            else:
                require(pointer == struct.pack("<I", DISABLED_FLASH_BASE), "Unknown pointer; restart required")
                suppressed_write(SLOT, bytes(4))
                suppressed_write(SCRATCH, bytes(16))
                if args.ff_byte:
                    suppressed_write(RAM_BASE + OFFSET, b"\0")
            restored_ram = read_audited(device, RAM_BASE, CONFIG_SIZE)
            mapped = read_audited(device, FLASH_VIEW, RESERVATION_SIZE)
            (folder / "ram-after-cleanup.bin").write_bytes(restored_ram)
            (folder / "mapped-after-cleanup.bin").write_bytes(mapped)
            differences = [i for i, (a, b) in enumerate(zip(mapped, flash)) if a != b]
            require(restored_ram == ram and (not differences or
                    (report["pattern_restore_pending"] and differences == [OFFSET])),
                    "Unexpected configuration changes; restart and verify backup")
            report.update(status="table_cleared_restart_required" if report["restart_required"] else "restored",
                          ram_restored=True, mapped_flash_matches_backup=mapped == flash)
            event("data_table_cleared", pointer_requires_physical_restart=report["restart_required"])
    next_step = (f"read_spi_native.py --restore-after-restart {folder}" if report["pattern_restore_pending"]
                 else "verify_recovery.py")
    print(f"Physical O, disconnect USB 10 seconds, then U/reconnect; run {next_step}.", flush=True)


if __name__ == "__main__":
    main()
