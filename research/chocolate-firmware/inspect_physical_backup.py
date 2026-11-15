"""Audit the captured 256-KiB stock flash and post-restart files, offline only.

This verifies the backup bytes, not a procedure for restoring a failed device.
No transport/device module is imported and no flashable image is generated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from inspect_firmware import HERE, RELEASES, inspect_container, inspect_flash, require, sha

SESSION = HERE / "artifacts/native-spi-probes/20261028T082007.414823Z"
FLASH_SHA = "aa487cf97d86f5a3b38161ee82c4020237f5be7eb4352b04c8310763300dcfe1"
RAM_SHA = "ce96586fcd7dd43003345fc43cc48fbd2da85111b32143008200b25cf085542e"
USER_SHA = "bc3edc46a280fa5a6bf660b1efd5c011abc2a5abe4f5c7517b8c7f516a35a499"


def audit_backup(session: Path) -> dict:
    session = session.resolve()
    report = json.loads((session / "report.json").read_text(encoding="utf-8"))
    require(report.get("full_flash") is True and report.get("full_flash_stable") is True,
            "Not a completed full physical flash capture")
    require(report.get("status") == "recovered_verified" and report.get("restart_required") is False,
            "Post-restart verification is outstanding")
    require(report.get("firmware_writes") == 0 and report.get("direct_spi_write_commands") == 0,
            "Capture was not a read-only flash operation")
    first = (session / "flash-physical-first.bin").read_bytes()
    second = (session / "flash-physical-second.bin").read_bytes()
    require(len(first) == len(second) == report.get("full_flash_bytes") == 0x40000,
            "Physical flash size mismatch")
    require(first == second, "Full physical flash copies differ")
    require(sha(first) == report.get("full_flash_sha256") == FLASH_SHA,
            "Full physical flash differs from the recorded device backup")

    source = (HERE / "artifacts/FootCtrlPlus_013.fwsc").read_bytes()
    require(sha(source) == RELEASES["013"]["stock_sha256"], "Stock V13 source differs")
    container, components = inspect_container(source)
    stock = components["flash2.bin"]
    require(first[:len(stock)] == stock, "Boot/application prefix differs from official V13")
    flash, _ = inspect_flash(first[:len(stock)], container["chip_key"], "013")
    user = first[0x39000:0x3F000]
    require(sha(user) == USER_SHA and sha(user[:23646]) == RAM_SHA,
            "Configuration differs from the pinned original backup")

    recovery = Path(report["post_restart_verification"]).resolve()
    require(recovery.parent == (HERE / "artifacts/recovery").resolve(), "Unexpected recovery path")
    verified = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
    require(verified.get("status") == "verified" and verified.get("writes") == 0
            and verified.get("identity") == "FootCtrlPlus_013", "Recovery was not verified")
    expected_controls = {"flash_pointer": True, "type5_unregistered": True, "scratch_zero": True}
    require(verified.get("ram_control_restored") == expected_controls,
            "Temporary diagnostic state is not cleared")
    require(verified.get("ram_matches_backup") is True
            and verified.get("flash_mapping_matches_backup") is True,
            "Recovery configuration verification failed")
    for name, expected in (("ram-before", user[:23646]), ("ram-after", user[:23646]),
                           ("usrflash-first", user), ("usrflash-second", user)):
        require((recovery / "capture" / f"{name}.bin").read_bytes() == expected,
                f"Post-restart capture differs: {name}")

    regions = []
    extents = [("boot_prefix", 0, 0x1000), ("application_image", 0x1000, len(stock))]
    extents += [(e["name"], e["offset"], e["offset"] + e["size"])
                for e in flash["entries"] if e["name"] in ("VM", "BTIF", "EXIF", "USRFLASH")]
    extents += [("unreserved_tail", 0x3F000, 0x40000)]
    cursor = 0
    for name, start, end in extents:
        require(start == cursor and start < end <= len(first), "Invalid region coverage")
        data = first[start:end]
        regions.append(dict(name=name, start=f"0x{start:05x}", end_exclusive=f"0x{end:05x}",
                            bytes=len(data), sha256=sha(data), non_ff_bytes=sum(x != 255 for x in data)))
        cursor = end
    require(cursor == len(first), "Incomplete region coverage")

    gaps = []
    absent = None
    for event in verified.get("usb_events", []):
        stamp = datetime.fromisoformat(event["utc"])
        if event["present"] is False:
            absent = stamp
        elif absent is not None:
            gaps.append((stamp - absent).total_seconds())
            absent = None

    return dict(status="physical_backup_and_post_restart_verified",
                audited_utc=datetime.now(timezone.utc).isoformat(), device_io=False,
                backup_directory=str(session), bytes=len(first), sha256=sha(first), identical_copies=2,
                stock_prefix_bytes=len(stock), stock_prefix_exact=True,
                boot_prefix_exact=True, app_sha256=flash["app_sha256"], crc_checks=flash["crc_checks"],
                regions=regions, recovery_directory=str(recovery), ram_control_restored=expected_controls,
                post_restart_complete_configuration_copies=4, observed_usb_absence_seconds=gaps,
                candidate_extension=dict(start="0x27600", end_exclusive="0x27800", bytes=512,
                                         all_ff=first[0x27600:0x27800] == bytes([255]) * 512,
                                         vm_geometry_compatibility_verified=False),
                firmware_installed=False, independent_bootloader_entry_verified=False,
                full_flash_restore_tested=False,
                scope="Device-specific backup includes VM, Bluetooth/calibration and user configuration; "
                      "byte integrity does not establish a tested restoration procedure.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=SESSION)
    args = parser.parse_args()
    result = audit_backup(args.session)
    output = args.session / "backup-audit.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Verified two identical {result['bytes']}-byte physical backups: {result['sha256']}")
    print("Boot/application prefix exactly matches official V13; post-restart configuration is intact.")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
