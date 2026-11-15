"""Read the mapped V13 application using its statically verified read callback.

Research only. Sends identity and memory-type-4 READ requests; no arbitrary
address CLI, firmware writes, resets, or bootloader commands. Stompilot's normal
configuration transport bounds remain unchanged. This is an application-code
snapshot, not a complete physical flash backup or an installable update.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices

RAM_BASE = 0x13490
APP_ADDRESS = 0x01E00120
APP_SIZE = 155488
APP_SHA256 = "f535c717a1ce23ef1b0fe5251c0ab7c906f2be068d2c172ad8440be1e0edeadc"


def main() -> None:
    reference = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    if len(reference) != APP_SIZE or hashlib.sha256(reference).hexdigest() != APP_SHA256:
        raise SystemExit("Extract and verify the pinned official V13 application first.")
    ports = list_usb_devices()
    if len(ports) != 1:
        raise SystemExit(f"Expected one Chocolate Plus in U mode; found {len(ports)}.")
    port = ports[0]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = HERE / "artifacts/captures" / stamp
    with UsbChocolate(port.input_name, port.output_name) as device:
        identity = device.query()
        if identity.name != "FootCtrlPlus_013":
            raise SystemExit(f"This code-read map is only for FootCtrlPlus_013, got {identity.name}.")

        def read_code(offset: int, size: int) -> bytes:
            if not (0 <= offset < APP_SIZE and 1 <= size <= 512 and offset + size <= APP_SIZE):
                raise ValueError("Read outside the audited application range")
            relative = APP_ADDRESS + offset - RAM_BASE
            prefix = b"\x04" + relative.to_bytes(4, "little") + size.to_bytes(3, "little")
            response = device._request(0x23, prefix, 0x23, prefix)
            payload = response["payload"][8:]
            if len(payload) != size:
                raise ValueError("Incomplete application-code response")
            return payload

        first = read_code(0, 64)
        if first != reference[:64]:
            raise SystemExit("The mapped application prefix differs from public V13; stopped after 64 bytes.")
        dest.mkdir(parents=True)
        path = dest / "app-readback.bin"
        digest = hashlib.sha256()
        with path.open("xb") as output:
            offset = 0
            while offset < APP_SIZE:
                block = first if offset == 0 else read_code(offset, min(512, APP_SIZE - offset))
                output.write(block)
                digest.update(block)
                offset += len(block)
                if offset == APP_SIZE or offset % 16384 == 64:
                    print(f"Read {offset}/{APP_SIZE} application bytes", flush=True)
        actual = digest.hexdigest()
        result = dict(timestamp_utc=stamp, identity=identity.name, bytes=APP_SIZE,
                      sha256=actual, expected_sha256=APP_SHA256, matches_public_v013=actual == APP_SHA256,
                      commands_sent=["0x11 identity", "0x23 read, memory type 4"],
                      configuration_writes=0, firmware_writes=0,
                      scope="Mapped application only, not complete flash or an installable update")
        (dest / "app-capture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"Saved: {dest}")
    if actual != APP_SHA256:
        raise SystemExit("Application differs from public V13; no patch offsets can be assumed.")


if __name__ == "__main__":
    main()
