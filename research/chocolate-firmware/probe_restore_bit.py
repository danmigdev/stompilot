"""Prepare a one-byte persistence probe; default mode only reads the device.

--execute temporarily requests 00 -> 80 at offset 13436, the final padding
byte in advanced page 8, switch D, bank B, then restores 00. Requires an
empty bank and page 8 disabled. No reset, mode change or firmware write.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json

from capture_persistence import (HERE, CONFIG_SIZE, FLASH_VIEW, RESERVATION_SIZE,
                                 UsbChocolate, list_usb_devices, capture, read_audited,
                                 require, sha)
from midi_configurator.chocolate import PAGE_COUNT_OFFSET

OFFSET = 13436
SWITCH_START = 93 + 31 * 417


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Perform the two configuration writes")
    args = parser.parse_args()
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one Chocolate in U")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = HERE / "artifacts/probes" / stamp
    result = dict(execute=args.execute, offset=OFFSET, before="00", requested="80", restore="00",
                  location="Page 8 switch D bank B, last unused SysEx byte", firmware_writes=0,
                  complete=False, write_attempts=0)
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        ram, flash, _ = capture(device, dest)
        require(ram == flash[:CONFIG_SIZE], "RAM and flash baseline differ")
        require(ram[PAGE_COUNT_OFFSET] < 7, "Page 8 must be disabled")
        require(ram[SWITCH_START] == 0 and ram[SWITCH_START + 81:SWITCH_START + 417] == bytes(336),
                "Target bank or its SysEx storage is not empty")
        require(ram[OFFSET] == 0, "Target is not 00")
        path = dest / "probe.json"
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if args.execute:
            try:
                result["write_attempts"] = 1
                path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                device._write(OFFSET, b"\x80")
                result["ram_after_probe"] = device._read(OFFSET, 1).hex()
                result["flash_after_probe"] = read_audited(device, FLASH_VIEW + OFFSET, 1).hex()
                result["failure_reproduced_without_h"] = (result["ram_after_probe"] == "80" and
                                                           result["flash_after_probe"] == "00")
            finally:
                # The test only raises a bit. Restoring 00 needs no 0 -> 1 transition.
                result["write_attempts"] += 1
                path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                device._write(OFFSET, b"\x00")
                final_ram = device.read_config()
                final_flash = read_audited(device, FLASH_VIEW, RESERVATION_SIZE)
                (dest / "ram-restored.bin").write_bytes(final_ram)
                (dest / "usrflash-restored.bin").write_bytes(final_flash)
                result["ram_restored"] = final_ram == ram
                result["flash_restored"] = final_flash == flash
                result["final_ram_sha256"] = sha(final_ram)
                result["final_flash_sha256"] = sha(final_flash)
                path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                require(result["ram_restored"] and result["flash_restored"],
                        "Restoration differs from backup; stop further experiments")
        result["complete"] = True
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Saved: {dest}")


if __name__ == "__main__":
    main()
