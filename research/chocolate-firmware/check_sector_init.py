"""Read-only checkpoint after a physical restart or U/H/U of the sector test."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time

from probe_sector_init import (ROOT, ERASE_UNIT, SCRATCH, SLOT, POINTER, FLASH_BASE,
                                OFFSET, LENGTH, UsbChocolate, list_usb_devices,
                                capture, native_read, baseline, expected_pattern,
                                require, sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--label", choices=("cold-start", "host-cycle", "restored"), required=True)
    parser.add_argument("--wait-reconnect", type=int, default=0)
    args = parser.parse_args()
    require(0 <= args.wait_reconnect <= 180, "Reconnect wait outside bounds")
    folder = args.session.resolve()
    require(folder.parent == ROOT.resolve(), "Wrong diagnostic directory")
    report = json.loads((folder / "report.json").read_text())
    ram, flash = baseline()
    require(report["baseline_sha256"] == sha(flash) and
            (folder / "usrflash-backup.bin").read_bytes() == flash, "Wrong backup")
    expected_flash = flash if args.label == "restored" else expected_pattern(flash)
    expected_ram = expected_flash[:len(ram)]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = folder / (args.label + "-" + stamp)
    dest.mkdir()
    checkpoint = dict(label=args.label, writes=0, status="started", usb_events=[])

    def save():
        (dest / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2) + "\n")

    save()
    try:
        if args.wait_reconnect:
            began, missing, previous = time.monotonic(), False, None
            while True:
                present = bool(list_usb_devices())
                if present != previous:
                    event = dict(utc=datetime.now(timezone.utc).isoformat(),
                                 seconds=round(time.monotonic() - began, 2), present=present)
                    checkpoint["usb_events"].append(event)
                    print(json.dumps(event), flush=True)
                    save()
                    previous = present
                if not present:
                    missing = True
                elif missing:
                    checkpoint["usb_reconnect_observed"] = True
                    time.sleep(2)
                    break
                require(time.monotonic() - began < args.wait_reconnect, "Reconnect wait timed out")
                time.sleep(.5)
        ports = list_usb_devices()
        require(len(ports) == 1, "Expected one Chocolate in U")
        with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
            controls = dict(pointer=native_read(device, POINTER, 4) == struct.pack("<I", FLASH_BASE),
                            slot=native_read(device, SLOT, 4) == bytes(4),
                            scratch=native_read(device, SCRATCH, 16) == bytes(16),
                            erase_unit=native_read(device, ERASE_UNIT, 1) == b"\0")
            checkpoint["controls_restored"] = controls
            require(all(controls.values()), "Temporary controls remain")
            actual_ram, actual_flash, _ = capture(device, dest / "capture")
            checkpoint.update(ram_matches_expected=actual_ram == expected_ram,
                              flash_matches_expected=actual_flash == expected_flash,
                              pattern=actual_flash[OFFSET:OFFSET + LENGTH].hex(),
                              ram_sha256=sha(actual_ram), flash_sha256=sha(actual_flash))
            require(actual_ram == expected_ram and actual_flash == expected_flash,
                    "Checkpoint differs from the complete expected configuration")
            checkpoint["status"] = "verified"
        report.update(restart_required=False, status="recovered_verified" if args.label == "restored"
                      else "pattern_verified_after_" + args.label)
        if args.label == "restored":
            report["restoration_pending"] = False
        report.setdefault("checkpoints", []).append(dict(label=args.label, directory=str(dest), status="verified"))
        temp = folder / "report.tmp"
        temp.write_text(json.dumps(report, indent=2) + "\n")
        temp.replace(folder / "report.json")
    except BaseException as error:
        checkpoint.update(status="not_verified", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()
        print(json.dumps(checkpoint, indent=2), flush=True)
        print(f"Saved: {dest}", flush=True)


if __name__ == "__main__":
    main()
