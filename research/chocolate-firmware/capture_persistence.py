"""Compare configuration RAM with the V13 flash mapping. Read requests only.

The flash view is the same cached mapping used by the stock load routine,
not a direct SPI bus capture or a complete physical flash backup.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from midi_configurator.chocolate_transport import CONFIG_SIZE, UsbChocolate, list_usb_devices
from inspect_firmware import RELEASES, compare_config, require, sha

RAM_BASE = 0x13490
APP_BASE = 0x1E00120
FLASH_BASE = 0x39000
FLASH_VIEW = 0x1E38000
RESERVATION_SIZE = 0x6000


def read_audited(device: UsbChocolate, address: int, length: int) -> bytes:
    ranges = ((RAM_BASE, CONFIG_SIZE), (0x19100, 4), (0x197E8, 32),
              (0x1CF60, 0x400), (0x1E125CC, 0x92), (0x1E13B02, 0x2A),
              (0x1E13C4E, 0x1C6),  # audited native command dispatch, including 0x24
              (FLASH_VIEW, RESERVATION_SIZE))
    require(length > 0 and any(start <= address < address + length <= start + size
                               for start, size in ranges), "Outside the audited read regions")
    result = bytearray()
    while len(result) < length:
        size = min(512, length - len(result))
        offset = address + len(result) - RAM_BASE
        prefix = b"\x04" + offset.to_bytes(4, "little") + size.to_bytes(3, "little")
        block = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
        require(len(block) == size, "Incomplete memory response")
        result.extend(block)
    return bytes(result)


def audit(device: UsbChocolate) -> dict:
    identity = device.query()
    require(identity.name == "FootCtrlPlus_013", "Only the verified stock V13 map is supported")
    ref = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(ref) == RELEASES["013"]["app_sha256"], "Reference application hash mismatch")
    for address, size in ((0x1E13B02, 0x2A), (0x1E125CC, 0x92)):
        require(read_audited(device, address, size) == ref[address - APP_BASE:address - APP_BASE + size],
                "Installed persistence/read code differs from audited stock V13")
    # Executable section ends at 0x1D344; later words are mutable RAM data.
    driver = read_audited(device, 0x1CF60, 0x3E4)
    require(driver == ref[0x25B54:0x25F38], "Relocated flash driver differs from reference")
    pointer = struct.unpack("<I", read_audited(device, 0x19100, 4))[0]
    state = struct.unpack("<8I", read_audited(device, 0x197E8, 32))
    require(pointer == FLASH_BASE and state[0] == 0x40000 and
            state[6:] == (0x1000, 0x1E00000), "Unexpected flash mapping or capacity")
    require(read_audited(device, FLASH_VIEW + 23652, 8) == b"usrdata\0", "Stored marker mismatch")
    return dict(identity=identity.name, physical_address=pointer, mapped_address=FLASH_VIEW,
                flash_capacity=state[0], driver_jedec_id=f"{state[1]:06x}",
                read_path="Cached mapped flash, same path as stock configuration load",
                audited_driver_sha256=sha(driver))


def capture(device: UsbChocolate, dest: Path) -> tuple[bytes, bytes, dict]:
    result = audit(device)
    dest.mkdir(parents=True, exist_ok=False)
    result.update(complete=False, configuration_writes=0, firmware_writes=0,
                  commands_sent=["0x11 identity", "0x23 read, memory type 4"],
                  scope="Public configuration RAM and entire 24-KiB USRFLASH reservation")
    manifest = dest / "persistence.json"
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    snapshots = {}
    for name, address, size in (("ram-before", RAM_BASE, CONFIG_SIZE),
                                ("usrflash-first", FLASH_VIEW, RESERVATION_SIZE),
                                ("usrflash-second", FLASH_VIEW, RESERVATION_SIZE),
                                ("ram-after", RAM_BASE, CONFIG_SIZE)):
        value = read_audited(device, address, size)
        snapshots[name] = value
        (dest / f"{name}.bin").write_bytes(value)
    ram, flash = snapshots["ram-before"], snapshots["usrflash-first"]
    result.update(complete=True, stable_ram=ram == snapshots["ram-after"],
                  stable_flash=flash == snapshots["usrflash-second"],
                  sha256={key: sha(value) for key, value in snapshots.items()},
                  ram_to_flash=compare_config(ram, flash[:CONFIG_SIZE]),
                  unused_flash_tail_is_ff=flash[23660:] == b"\xff" * (RESERVATION_SIZE - 23660))
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    require(result["stable_ram"] and result["stable_flash"], "Unstable snapshot; all reads preserved")
    return ram, flash, result


def main() -> None:
    ports = list_usb_devices()
    require(len(ports) == 1, f"Expected one Chocolate in U; found {len(ports)}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = HERE / "artifacts/captures" / stamp
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        _, _, result = capture(device, dest)
    print(json.dumps(result, indent=2))
    print(f"Saved: {dest}")


if __name__ == "__main__":
    main()
