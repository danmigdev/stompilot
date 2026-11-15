"""Read Chocolate identity and two configuration snapshots; never write a device.

These are configuration RAM snapshots, not a complete flash/firmware backup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=float, default=0, help="Wait up to 45 seconds for the USB device")
    args = parser.parse_args()
    if not 0 <= args.wait <= 45:
        parser.error("--wait must be between 0 and 45 seconds")
    deadline = time.monotonic() + args.wait
    while True:
        ports = list_usb_devices()
        if ports or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if len(ports) != 1:
        raise SystemExit(f"Expected one Chocolate Plus in U mode; found {len(ports)}.")
    port = ports[0]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = HERE / "artifacts/captures" / stamp
    dest.mkdir(parents=True)
    with UsbChocolate(port.input_name, port.output_name) as device:
        identity = device.query()
        metadata = dict(timestamp_utc=stamp, name=identity.name, port=port.name,
                        raw_identity=identity.raw_payload.hex(), complete=False,
                        commands_sent=["0x11 identity", "0x23 read, memory type 4"],
                        configuration_writes=0, firmware_writes=0,
                        scope="Configuration RAM only, not a firmware/flash backup")
        manifest = dest / "capture.json"
        manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(f"Reading {identity.name} twice...", flush=True)
        first = device.read_config()
        (dest / "config-first.bin").write_bytes(first)
        second = device.read_config()
        (dest / "config-second.bin").write_bytes(second)
        metadata.update(complete=True, bytes=len(first), identical=first == second,
                        first_sha256=hashlib.sha256(first).hexdigest(),
                        second_sha256=hashlib.sha256(second).hexdigest())
        manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    print(f"Saved: {dest}")
    if first != second:
        raise SystemExit("The two reads differ; both snapshots were preserved.")


if __name__ == "__main__":
    main()
