"""Compare FF writes in the same 32 inactive padding bytes, restoring each case.

Default reads only. --execute compares one FF byte, 32 individual FF-byte writes,
and a single 32-byte FF write. Fixed zero baseline only; no firmware/RAM-code,
erase, reset or mode command. Each case ends with full backup verification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json

from capture_persistence import (HERE, CONFIG_SIZE, UsbChocolate, list_usb_devices,
                                 capture, require, sha)
from cycle_probe import OFFSET, LENGTH, SWITCH_START
from midi_configurator.chocolate import PAGE_COUNT_OFFSET
from retention_probe import RAM_SHA, FLASH_SHA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    folder = HERE / "artifacts/granularity-probes" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True)
    report = dict(status="checking", execute=args.execute, offset=OFFSET, length=LENGTH,
                  firmware_writes=0, ram_code_writes=0, restore_pending=False, cases=[])

    def save():
        pending = folder / "report.tmp"
        pending.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        pending.replace(folder / "report.json")

    save()
    print(f"Saved: {folder}", flush=True)
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        ram, flash, _ = capture(device, folder / "baseline")
        require(sha(ram) == RAM_SHA and sha(flash) == FLASH_SHA, "Wrong baseline; no write sent")
        require(ram[PAGE_COUNT_OFFSET] < 7 and ram[SWITCH_START] == 0 and
                ram[SWITCH_START + 81:SWITCH_START + 417] == bytes(336),
                "Probe bank must be empty and disabled")
        if not args.execute:
            report["status"] = "read_only_verified"
            save()
            print(json.dumps(report, indent=2))
            return
        scenarios = (
            ("single_ff_byte", [(OFFSET + LENGTH - 1, b"\xff")]),
            ("32_individual_bytes", [(OFFSET + i, b"\xff") for i in range(LENGTH)]),
            ("one_32_byte_write", [(OFFSET, b"\xff" * LENGTH)]),
        )
        for name, writes in scenarios:
            case = dict(name=name, writes_requested=0, writes_acknowledged=0)
            report["cases"].append(case)
            report.update(status=name, restore_pending=True)
            save()
            expected_ram, expected_flash = bytearray(ram), bytearray(flash)
            try:
                for address, data in writes:
                    case["writes_requested"] += 1
                    save()
                    device._write(address, data)
                    case["writes_acknowledged"] += 1
                    save()
                    expected_ram[address:address + len(data)] = data
                    expected_flash[address:address + len(data)] = data
                dest = folder / name
                try:
                    capture(device, dest)
                except ValueError as error:
                    # An unstable complete flash snapshot is the measurement,
                    # not permission to ignore an audit or transport failure.
                    if str(error) != "Unstable snapshot; all reads preserved":
                        raise
                snapshots = {key: (dest / (key + ".bin")).read_bytes() for key in
                             ("ram-before", "ram-after", "usrflash-first", "usrflash-second")}
                require(all(len(value) == (CONFIG_SIZE if key.startswith("ram") else len(flash))
                            for key, value in snapshots.items()), "Incomplete capture")
                case["snapshots"] = {}
                for key, value in snapshots.items():
                    expected = expected_ram if key.startswith("ram") else expected_flash
                    outside = sum(a != b for i, (a, b) in enumerate(zip(expected, value))
                                  if not OFFSET <= i < OFFSET + LENGTH)
                    case["snapshots"][key] = dict(
                        pattern=value[OFFSET:OFFSET + LENGTH].hex(),
                        changed_bytes=sum(a != b for a, b in zip(expected, value)),
                        cleared_bits=sum((a & ~b).bit_count() for a, b in zip(expected, value)),
                        set_bits=sum((b & ~a).bit_count() for a, b in zip(expected, value)),
                        changed_outside_probe=outside)
                save()
                require(snapshots["ram-before"] == expected_ram == snapshots["ram-after"],
                        "RAM differs from the requested pattern")
                require(not any(x["changed_outside_probe"] for x in case["snapshots"].values()),
                        "A byte outside the fixed probe changed; stop after restoration")
            finally:
                case["restoration_requested"] = True
                save()
                device._write(OFFSET, bytes(LENGTH))
                restored_ram, restored_flash, _ = capture(device, folder / (name + "-restored"))
                case["restored"] = restored_ram == ram and restored_flash == flash
                report["restore_pending"] = not case["restored"]
                save()
                require(case["restored"], "Complete backup comparison failed; no further experiments")
            print(json.dumps(case), flush=True)
        report["status"] = "restored"
        save()


if __name__ == "__main__":
    main()
