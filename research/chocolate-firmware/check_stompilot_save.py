"""Owned-device test of Stompilot's V13 writer using the pinned inactive FF pattern.

Only 32 inactive padding bytes differ. All configuration is checked and later
restored. No executable payload or firmware operation is used.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices
from midi_configurator.chocolate_persistence import pending_save, digest, require

ROOT = HERE / "artifacts/stompilot-save-tests"
APP_BACKUPS = HERE.parents[1] / "dist/.backups"
RAM_SHA = "ce96586fcd7dd43003345fc43cc48fbd2da85111b32143008200b25cf085542e"
FLASH_SHA = "bc3edc46a280fa5a6bf660b1efd5c011abc2a5abe4f5c7517b8c7f516a35a499"
PATTERN_SHA = "df122b54ee0f920d1a403177141ec35bc18a1098832edfea572ea44385ed71d6"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--verify", type=Path)
    action.add_argument("--restore", type=Path)
    parser.add_argument("--wait-reconnect", type=int, default=0)
    args = parser.parse_args()
    require(0 <= args.wait_reconnect <= 180, "Reconnect wait outside bounds")
    original = (HERE / "artifacts/spi-probes/20261027T175717.404580Z/baseline/ram-before.bin").read_bytes()
    require(digest(original) == RAM_SHA, "Original backup differs")
    require(original[0] == 3 and original[23639] < 6 and original[93 + 31 * 417 + 289] == 0
            and original[13405:13437] == bytes(32), "Original inactive padding differs")
    pattern = original[:13405] + bytes([255]) * 32 + original[13437:]
    if args.prepare:
        ROOT.mkdir(exist_ok=True)
        require(not any(json.loads(path.read_text())["phase"].endswith("pending_restart")
                        for path in ROOT.glob("*/test.json")), "An earlier test still needs restart verification")
        folder = ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        folder.mkdir()
        report = dict(phase="preparing", firmware_writes=0, changed_offsets=[13405, 13437], usb_events=[],
                      backup_directory=str(APP_BACKUPS))
    else:
        folder = (args.verify or args.restore).resolve()
        require(folder.parent == ROOT.resolve(), "Wrong test session")
        report = json.loads((folder / "test.json").read_text())
    backups = Path(report.get("backup_directory", str(folder))).resolve()
    require(backups in (APP_BACKUPS.resolve(), folder.resolve()), "Unexpected application backup location")

    def save():
        (folder / "test.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report), flush=True)

    save()
    print(f"SESSION={folder}", flush=True)
    if args.wait_reconnect:
        absent, previous, deadline = False, None, time.monotonic() + args.wait_reconnect
        while True:
            present = bool(list_usb_devices())
            if present != previous:
                report["usb_events"].append(dict(utc=datetime.now(timezone.utc).isoformat(), present=present))
                previous = present
                save()
            if not present:
                absent = True
            elif absent:
                time.sleep(2)
                break
            require(time.monotonic() < deadline, "Waiting for reconnect timed out; verification remains pending")
            time.sleep(.5)
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected exactly one Chocolate Plus in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        if args.verify:
            require(report["phase"] in ("pattern_pending_restart", "restore_pending_restart"), "No pending test verification")
            expected = original if report["phase"] == "restore_pending_restart" else pattern
            result = device.verify_config_restart(backups, progress_callback=lambda *parts: print(*parts, flush=True))
            require(result == expected, "Verified configuration differs from the pinned test")
            report["phase"] = "restored_verified" if expected == original else "pattern_verified"
        else:
            if args.restore:
                require(report["phase"] == "pattern_verified", "Verify the cold-start pattern before restoring")
            before, after = (pattern, original) if args.restore else (original, pattern)
            require(device.read_config() == before, "Live configuration differs from the expected test state")
            device.write_config(before, after, backup_directory=backups,
                                progress_callback=lambda *parts: print(*parts, flush=True))
            journal = pending_save(backups)
            require(journal is not None and digest(journal.target) == (FLASH_SHA if args.restore else PATTERN_SHA),
                    "The physical image differs from the pinned complete test image")
            report.update(phase="restore_pending_restart" if args.restore else "pattern_pending_restart",
                          latest_journal=journal.directory.name, physical_sha256=digest(journal.target))
    save()


if __name__ == "__main__":
    main()
