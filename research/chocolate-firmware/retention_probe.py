"""One-byte cold-start experiment, restricted to the previously tested padding.

prepare writes 80 at offset 13436 and leaves that single inactive byte pending.
finish reads after a user-performed restart and restores 00. restore only cleans
up. No custom RAM code, callback registration, firmware, erase or reset command.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from capture_persistence import (HERE, UsbChocolate, list_usb_devices, capture,
                                 audit, require, sha)
from probe_restore_bit import OFFSET, SWITCH_START
from midi_configurator.chocolate import PAGE_COUNT_OFFSET

ROOT = HERE / "artifacts/retention-probes"
RAM_SHA = "ce96586fcd7dd43003345fc43cc48fbd2da85111b32143008200b25cf085542e"
FLASH_SHA = "bc3edc46a280fa5a6bf660b1efd5c011abc2a5abe4f5c7517b8c7f516a35a499"


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def save(folder, report):
    pending = folder / "session.tmp"
    pending.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    pending.replace(folder / "session.json")


def require_inactive(ram, *, empty):
    require(ram[PAGE_COUNT_OFFSET] < 7 and ram[SWITCH_START] == 0,
            "The target bank must remain inactive and page 8 disabled")
    bank = bytearray(ram[SWITCH_START + 81:SWITCH_START + 417])
    if not empty:
        bank[OFFSET - SWITCH_START - 81] = 0
    require(bank == bytes(336), "Target bank is no longer empty")


def with_pattern(image):
    return image[:OFFSET] + b"\x80" + image[OFFSET + 1:]


def cleanup(device, folder, report, baseline_ram, baseline_flash):
    audit(device)
    require_inactive(device.read_config(), empty=False)
    report["restore_attempted"] = True
    save(folder, report)
    device._write(OFFSET, b"\0")
    ram, flash, _ = capture(device, folder / ("restored-" + stamp()))
    report.update(restore_pending=False, restored_ram_matches_backup=ram == baseline_ram,
                  restored_flash_matches_backup=flash == baseline_flash)
    # A full mismatch remains unresolved, even when the single target is zero.
    report["restore_pending"] = not (ram == baseline_ram and flash == baseline_flash)
    save(folder, report)
    require(not report["restore_pending"], "Restoration differs from backup; no other bytes were written")


def wait_reconnect(folder, report, seconds):
    began = time.monotonic()
    missing = False
    previous = None
    report["usb_events"] = []
    while time.monotonic() - began < seconds:
        present = bool(list_usb_devices())
        if present != previous:
            event = dict(utc=stamp(), seconds=round(time.monotonic() - began, 2), present=present)
            report["usb_events"].append(event)
            save(folder, report)
            print(json.dumps(event), flush=True)
            previous = present
        if not present:
            missing = True
        elif missing:
            time.sleep(2)
            return True
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "finish", "restore"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--wait-reconnect", type=int, default=0)
    args = parser.parse_args()
    require(0 <= args.wait_reconnect <= 180, "Reconnect wait must be 0..180 seconds")
    require(not args.wait_reconnect or args.action == "finish", "Only finish can wait")
    if args.action == "prepare":
        require(args.session is None, "Preparation creates a new immutable baseline")
        require(not any(json.loads(p.read_text()).get("restore_pending")
                        for p in ROOT.glob("*/session.json")), "A previous probe still needs restoration")
        folder = ROOT / stamp()
        folder.mkdir(parents=True)
        report = dict(status="preparing", offset=OFFSET, length=1, pattern="80",
                      original="00", restore_pending=False, firmware_writes=0,
                      ram_code_writes=0, configuration_write_scope="one unused byte only")
    else:
        require(args.session is not None, "An existing session is required")
        folder = args.session.resolve()
        require(folder.parent == ROOT.resolve(), "Session must be in the fixed retention-probes directory")
        report = json.loads((folder / "session.json").read_text())
        require((report["offset"], report["length"], report["pattern"], report["original"])
                == (OFFSET, 1, "80", "00"), "Wrong probe specification")
        require(report["restore_pending"], "Session is already restored")
    save(folder, report)
    print(f"Session: {folder}", flush=True)
    if args.wait_reconnect:
        report["reconnect_observed"] = wait_reconnect(folder, report, args.wait_reconnect)
        save(folder, report)
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U; pending pattern is recorded")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        if args.action == "prepare":
            ram, flash, _ = capture(device, folder / "baseline")
            require(sha(ram) == RAM_SHA and sha(flash) == FLASH_SHA,
                    "Device differs from the verified recovery baseline; no write sent")
            require_inactive(ram, empty=True)
            report.update(status="write_requested", restore_pending=True,
                          ram_sha256=sha(ram), flash_sha256=sha(flash))
            save(folder, report)
            try:
                device._write(OFFSET, b"\x80")
                new_ram, new_flash, _ = capture(device, folder / "prepared")
                require(new_ram == with_pattern(ram) and new_flash == with_pattern(flash),
                        "Pattern was not stable and isolated before restart")
            except BaseException as error:
                report.update(status="prepare_failed", error=f"{type(error).__name__}: {error}")
                save(folder, report)
                cleanup(device, folder, report, ram, flash)
                raise
            report.update(status="awaiting_physical_restart", prepared_utc=stamp())
        else:
            ram = (folder / "baseline/ram-before.bin").read_bytes()
            flash = (folder / "baseline/usrflash-first.bin").read_bytes()
            require(sha(ram) == RAM_SHA == report["ram_sha256"] and
                    sha(flash) == FLASH_SHA == report["flash_sha256"], "Backup hash mismatch")
            try:
                if args.action == "finish":
                    new_ram, new_flash, _ = capture(device, folder / ("after-" + stamp()))
                    report.update(after_ram_byte=new_ram[OFFSET:OFFSET + 1].hex(),
                                  after_flash_byte=new_flash[OFFSET:OFFSET + 1].hex(),
                                  entire_ram_matches_prepared=new_ram == with_pattern(ram),
                                  entire_flash_matches_prepared=new_flash == with_pattern(flash),
                                  ram_changes_outside_target=sum(a != b for i, (a, b) in
                                      enumerate(zip(ram, new_ram)) if i != OFFSET),
                                  flash_changes_outside_target=sum(a != b for i, (a, b) in
                                      enumerate(zip(flash, new_flash)) if i != OFFSET))
                    save(folder, report)
            finally:
                cleanup(device, folder, report, ram, flash)
            report["status"] = "restored"
        save(folder, report)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
