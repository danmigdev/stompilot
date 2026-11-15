"""Read-only verification after restarting the interrupted RAM probe through O.

Uses only identity and memory-type-4 reads. Never reinstalls the temporary
callback or sends a write, reset, erase, update or bootloader request.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time

from capture_persistence import (HERE, FLASH_BASE, UsbChocolate, list_usb_devices,
                                 capture, require, sha)
from read_spi import POINTER, SLOT, SCRATCH, SCRATCH_SIZE, ram_read

BASELINE = HERE / "artifacts/spi-probes/20261027T175717.404580Z/baseline"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=float, default=0)
    parser.add_argument("--wait-reconnect", type=int, default=0)
    parser.add_argument("--full-flash-session", type=Path)
    args = parser.parse_args()
    require(0 <= args.wait <= 45, "Wait must be between 0 and 45 seconds")
    require(0 <= args.wait_reconnect <= 180, "Reconnect wait must be between 0 and 180 seconds")
    session = args.full_flash_session.resolve() if args.full_flash_session else None
    if session:
        require(session.parent == (HERE / "artifacts/native-spi-probes").resolve(), "Wrong native session path")
        pending = json.loads((session / "report.json").read_text())
        require(pending.get("full_flash") and not pending.get("ff_byte"), "Not a full-flash read session")
    expected_ram = (BASELINE / "ram-before.bin").read_bytes()
    expected_flash = (BASELINE / "usrflash-first.bin").read_bytes()
    require(sha(expected_ram) == "ce96586fcd7dd43003345fc43cc48fbd2da85111b32143008200b25cf085542e"
            and sha(expected_flash) == "bc3edc46a280fa5a6bf660b1efd5c011abc2a5abe4f5c7517b8c7f516a35a499",
            "Pre-probe backup differs from the recorded baseline")
    dest = HERE / "artifacts/recovery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest.mkdir(parents=True)
    report = dict(status="waiting_for_usb", writes=0, baseline=str(BASELINE), usb_events=[])
    manifest = dest / "recovery.json"

    def save():
        manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)

    save()
    try:
        if args.wait_reconnect:
            began, absent, previous = time.monotonic(), False, None
            while True:
                present = bool(list_usb_devices())
                if present != previous:
                    report["usb_events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), present=present))
                    previous = present
                    save()
                if not present:
                    absent = True
                elif absent:
                    report["usb_reconnect_observed"] = True
                    time.sleep(2)
                    break
                require(time.monotonic() - began < args.wait_reconnect, "USB reconnect wait timed out")
                time.sleep(.5)
        deadline = time.monotonic() + args.wait
        while True:
            ports = list_usb_devices()
            if ports or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        report["usb_midi_devices_found"] = len(ports)
        require(len(ports) == 1, f"Expected one Chocolate Plus in U; found {len(ports)}")
        report["status"] = "checking"
        with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
            report["identity"] = device.query().name
            require(report["identity"] == "FootCtrlPlus_013", "Unexpected firmware identity")
            report["ram_control_restored"] = {
                "flash_pointer": ram_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE),
                "type5_unregistered": ram_read(device, SLOT, 4) == bytes(4),
                "scratch_zero": ram_read(device, SCRATCH, SCRATCH_SIZE) == bytes(SCRATCH_SIZE),
            }
            save()
            require(all(report["ram_control_restored"].values()), "Temporary RAM state remains; no writes sent")
            ram, flash, _ = capture(device, dest / "capture")
            report.update(ram_matches_backup=ram == expected_ram,
                          flash_mapping_matches_backup=flash == expected_flash)
            require(report["ram_matches_backup"] and report["flash_mapping_matches_backup"],
                    "Configuration differs from the pre-probe backup; captures preserved")
            report["status"] = "verified"
            if session:
                pending.update(status="recovered_verified", restart_required=False,
                               post_restart_verification=str(dest),
                               restored_ram_matches_backup=True, restored_flash_matches_backup=True)
                (session / "report.json").write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    except BaseException as error:
        report.update(status="not_verified", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()
        print(f"Saved: {dest}", flush=True)


if __name__ == "__main__":
    main()
