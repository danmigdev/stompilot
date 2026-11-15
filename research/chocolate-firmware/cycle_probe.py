"""Controlled persistence test in 32 unused padding bytes of a disabled bank.

prepare saves a stable backup, writes FF only at 13405..13436, then verifies
that RAM and mapped flash differ from the backup only in those bytes.
check only reads; restore writes back only those 32 original zero bytes.
No firmware, reset, erase or mode-change command is available here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from capture_persistence import (HERE, CONFIG_SIZE, FLASH_VIEW, RESERVATION_SIZE,
                                 UsbChocolate, list_usb_devices, audit, capture,
                                 read_audited, require, sha, compare_config)
from midi_configurator.chocolate import PAGE_COUNT_OFFSET

OFFSET, LENGTH = 13405, 32
SWITCH_START = 93 + 31 * 417


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def expected(image: bytes) -> bytes:
    return image[:OFFSET] + b"\xff" * LENGTH + image[OFFSET + LENGTH:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "restore"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--label", default="manual")
    parser.add_argument("--wait", type=int, default=0, help="Observe USB disappear/reappear, up to 180 seconds")
    args = parser.parse_args()
    require(0 <= args.wait <= 180, "Wait must be between 0 and 180 seconds")
    require(args.action == "check" or not args.wait, "Only checks can wait for a reconnect")
    if args.action == "prepare":
        require(args.session is None, "Preparation always creates a new backup")
        folder = HERE / "artifacts/cycle-probes" / stamp()
        folder.mkdir(parents=True)
        result = dict(offset=OFFSET, length=LENGTH, pattern="ff", firmware_writes=0,
                      status="preparing", restore_pending=False)
    else:
        require(args.session is not None, "Supply the existing session directory")
        folder = args.session.resolve()
        result = json.loads((folder / "session.json").read_text())
        require((result["offset"], result["length"]) == (OFFSET, LENGTH), "Wrong probe coordinates")
        require(result["restore_pending"], "Session has no pending pattern to check or restore")
    manifest = folder / "session.json"
    save(manifest, result)
    events = []
    if args.wait:
        began = time.monotonic()
        missing = False
        previous = None
        print("Waiting for USB disconnect/reconnect", flush=True)
        while time.monotonic() - began < args.wait:
            present = bool(list_usb_devices())
            if present != previous:
                event = dict(seconds=round(time.monotonic() - began, 2), usb_present=present)
                events.append(event)
                print(json.dumps(event), flush=True)
                previous = present
            if not present:
                missing = True
            if present and missing:
                time.sleep(2)
                break
            time.sleep(0.5)
        else:
            raise SystemExit("No reconnect observed; the pattern remains in unused padding. Restore is pending.")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        if args.action == "prepare":
            ram, flash, _ = capture(device, folder / "baseline")
            require(ram == flash[:CONFIG_SIZE], "RAM and flash baseline differ")
            require(ram[PAGE_COUNT_OFFSET] < 7 and ram[SWITCH_START] == 0,
                    "Page 8 must be disabled and the target switch in its inactive-bank mode")
            require(ram[SWITCH_START + 81:SWITCH_START + 417] == bytes(336), "Target bank is not empty")
            result.update(ram_sha256=sha(ram), flash_sha256=sha(flash), restore_pending=True,
                          status="write_requested")
            save(manifest, result)
            try:
                device._write(OFFSET, b"\xff" * LENGTH)
                after_ram, after_flash, _ = capture(device, folder / "prepared")
                require(after_ram == expected(ram) and after_flash == expected(flash),
                        "The prepared pattern or neighboring bytes differ from expectation")
            except BaseException:
                device._write(OFFSET, ram[OFFSET:OFFSET + LENGTH])
                recovered_ram = device.read_config()
                recovered_flash = read_audited(device, FLASH_VIEW, RESERVATION_SIZE)
                (folder / "failed-prepare-restored-ram.bin").write_bytes(recovered_ram)
                (folder / "failed-prepare-restored-flash.bin").write_bytes(recovered_flash)
                result.update(status="prepare_failed", restore_pending=not (
                    recovered_ram == ram and recovered_flash == flash))
                save(manifest, result)
                raise
            result.update(status="pattern_prepared", prepared_utc=stamp(),
                          expected_ram_sha256=sha(after_ram), expected_flash_sha256=sha(after_flash))
        else:
            ram = (folder / "baseline/ram-before.bin").read_bytes()
            flash = (folder / "baseline/usrflash-first.bin").read_bytes()
            require(sha(ram) == result["ram_sha256"] and sha(flash) == result["flash_sha256"], "Backup hash mismatch")
            require(len(ram) == CONFIG_SIZE and len(flash) == RESERVATION_SIZE and
                    ram[OFFSET:OFFSET + LENGTH] == bytes(LENGTH), "Invalid baseline")
            if args.action == "restore":
                audit(device)
                device._write(OFFSET, ram[OFFSET:OFFSET + LENGTH])
            out = folder / f"{args.action}-{stamp()}"
            actual_ram, actual_flash, _ = capture(device, out)
            target_ram, target_flash = (ram, flash) if args.action == "restore" else (expected(ram), expected(flash))
            report = dict(label=args.label, usb_events=events, action=args.action,
                          ram_diff=compare_config(target_ram, actual_ram),
                          flash_public_diff=compare_config(target_flash[:CONFIG_SIZE], actual_flash[:CONFIG_SIZE]),
                          entire_usrflash_equal=actual_flash == target_flash,
                          bytes_outside_probe_changed=sum(a != b for i, (a, b) in enumerate(zip(target_flash, actual_flash))
                                                          if not OFFSET <= i < OFFSET + LENGTH),
                          probe_ram=actual_ram[OFFSET:OFFSET + LENGTH].hex(),
                          probe_flash=actual_flash[OFFSET:OFFSET + LENGTH].hex())
            save(out / "comparison.json", report)
            print(json.dumps(report, indent=2), flush=True)
            result["last_capture"] = str(out)
            if args.action == "restore":
                result.update(status="restored" if actual_ram == ram and actual_flash == flash else "restore_differs",
                              restore_pending=actual_ram[OFFSET:OFFSET + LENGTH] != bytes(LENGTH) or
                              actual_flash[OFFSET:OFFSET + LENGTH] != bytes(LENGTH))
        save(manifest, result)
    print(json.dumps(result, indent=2), flush=True)
    print(f"Session: {folder}", flush=True)


if __name__ == "__main__":
    main()
