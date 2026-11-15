"""Restore the interrupted fixed sector using normal configuration writes only.

No erase, type-5 callback, RAM-control write or firmware programming is sent.
Only pages that are completely FF or already match the pinned backup are
accepted. Each changed page receives one aligned 256-byte normal config write.
The default is read-only; --execute enables this exact recovery operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct

from probe_sector_erase import (ROOT, baseline, RELATIVE, SECTOR_SIZE, PAGE_SIZE,
                                FLASH_BASE, FLASH_SHA, SCRATCH, SLOT,
                                UsbChocolate, list_usb_devices, capture, require, sha)
from read_spi import POINTER, ram_read


def recovery_pages(current, original):
    require(len(current) == len(original) == 24576, "Wrong backup/capture size")
    require(current[:RELATIVE] == original[:RELATIVE] and
            current[RELATIVE + SECTOR_SIZE:] == original[RELATIVE + SECTOR_SIZE:],
            "Changes outside the one authorized recovery sector")
    pending = []
    for offset in range(RELATIVE, RELATIVE + SECTOR_SIZE, PAGE_SIZE):
        actual, expected = current[offset:offset + PAGE_SIZE], original[offset:offset + PAGE_SIZE]
        require(actual == expected or actual == b"\xff" * PAGE_SIZE,
                f"Page at offset {offset} is neither blank nor already restored; no write authorized")
        if actual != expected:
            pending.append((offset, expected))
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    folder = args.session.resolve()
    require(folder.parent == ROOT.resolve(), "Wrong recovery directory")
    report = json.loads((folder / "report.json").read_text())
    ram, flash = baseline()
    require(report["baseline_sha256"] == FLASH_SHA and report["physical_sector"] == 0x3C000 and
            (folder / "usrflash-backup.bin").read_bytes() == flash and
            (folder / "ram-backup.bin").read_bytes() == ram, "Wrong session or backup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = folder / ("program-only-recovery-" + stamp)
    output.mkdir()

    def record(event, **details):
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), event=event, **details))
        temp = folder / "report.tmp"
        temp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temp.replace(folder / "report.json")
        print(event, json.dumps(details), flush=True)

    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        before_ram, before_flash, _ = capture(device, output / "before")
        require(before_ram == before_flash[:len(ram)], "RAM does not match the post-restart flash image")
        require(ram_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE) and
                ram_read(device, SLOT, 4) == bytes(4) and
                ram_read(device, SCRATCH, 16) == bytes(16), "Diagnostic controls remain")
        pages = recovery_pages(before_flash, flash)
        record("program_only_preflight", page_offsets=[offset for offset, _ in pages],
               before_flash_sha256=sha(before_flash), execute=args.execute)
        if not args.execute:
            return
        try:
            for offset, data in pages:
                record("native_config_page_restore_requested", offset=offset, bytes=len(data))
                device._write(offset, data)
                require(device._read(offset, PAGE_SIZE) == data, "RAM page readback differs")
                record("native_config_page_restore_acknowledged", offset=offset)
            after_ram, after_flash, _ = capture(device, output / "after")
            require(after_ram == ram and after_flash == flash, "Complete restoration differs from the backup")
            report.update(status="program_only_restored_pending_restart", restoration_pending=False,
                          restart_required=True, program_only_capture=str(output),
                          restored_ram_matches_backup=True, restored_mapping_matches_backup=True)
            record("program_only_full_backup_verified", sha256=sha(after_flash))
        except BaseException as error:
            report.update(status="program_only_recovery_interrupted", restoration_pending=True,
                          recovery_error=f"{type(error).__name__}: {error}")
            record("program_only_recovery_interrupted")
            raise
    print("Configuration restored and verified. Physical restart and read-only verification still required.")


if __name__ == "__main__":
    main()
