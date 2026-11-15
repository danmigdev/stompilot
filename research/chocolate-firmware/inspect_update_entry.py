"""Compare pinned M-UPGRADE entry bytes with V13's special USB packet branch.

Offline byte guards and a narrow branch model, not an emulator or a live test.
Neither the updater executable nor either packet is executed/sent.
"""
from __future__ import annotations

import json
import struct

from inspect_firmware import HERE, RELEASES, require, sha

UPDATER_SHA = "d49de406878cf88e4eb0a9fddedd8e176ad96493f0a9fbd46318aa2ae08fe6f7"
APP_BASE = 0x1E00120


def pe_offset(data: bytes, rva: int, size: int) -> int:
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    require(data[pe:pe + 4] == b"PE\0\0", "Invalid PE signature")
    machine, count = struct.unpack_from("<HH", data, pe + 4)
    require(machine == 0x8664, "Expected x64 updater")
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    for index in range(count):
        section = pe + 24 + optional_size + 40 * index
        _, address, raw_size, raw_offset = struct.unpack_from("<IIII", data, section + 8)
        if address <= rva and rva + size <= address + raw_size:
            start = raw_offset + rva - address
            require(start + size <= len(data), "PE extent outside file")
            return start
    raise ValueError("PE RVA has no complete stored extent")


def main() -> None:
    root = HERE / "artifacts/updater-inspection"
    updater = (root / "M-UPGRADE.exe").read_bytes()
    app = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(updater) == UPDATER_SHA and sha(app) == RELEASES["013"]["app_sha256"],
            "Input differs from the statically inspected binaries")
    pos = pe_offset(updater, 0x2913E, 13)
    require(updater[pos:pos + 13] == bytes.fromhex("c745f8f022243566c745fc7ff7"),
            "Updater command construction changed")
    command = updater[pos + 3:pos + 7] + updater[pos + 11:pos + 13]
    packet = bytes([4]) + command[:3] + bytes([7]) + command[3:]

    def guard(address: int, expected: str) -> None:
        value = bytes.fromhex(expected)
        offset = address - APP_BASE
        require(app[offset:offset + len(value)] == value, f"V13 code guard failed: {address:#x}")

    # Startup copies 0xD68 bytes from mapped 0x1E23D0C to RAM 0x11200.
    guard(0x1E00146, "c4ff00120100c1ff0c3de201c2ff680d0000")
    prefix_offset = 0x1E23D0C - APP_BASE + 0x4D
    template = app[prefix_offset:prefix_offset + 8]
    require(template == bytes.fromhex("04f0222407357df7"), "Special packet template changed")
    # len == 8, compare first 6 bytes to RAM 0x1124D, then byte[7] == F7,
    # byte[6] == 7D; only that branch calls RAM 0x1C934.
    guard(0x1E102A0, "81f80f10c1ff4d120100422698ea663e8048"
                       "50ee970090f804ee50ee960000f8bbfa")
    guard(0x1E10438, "90ea7c62f79f")

    def special_branch(data: bytes) -> bool:
        return len(data) == 8 and data[:6] == template[:6] and data[7] == 0xF7 and data[6] == 0x7D

    require(special_branch(template), "Special-branch model rejected its template")
    require(not special_branch(packet), "Unexpected updater/special-branch equivalence")
    report = dict(status="offline_partial_path_inspection", device_io=False, updater_executed=False,
                  packets_sent=0, updater_sha256=sha(updater), app_sha256=sha(app),
                  updater_command_address="0x14002913e", updater_sysex=command.hex(" "),
                  updater_usb_midi_packet=packet.hex(" "),
                  stock_special_usb_packet=template.hex(" "),
                  stock_packet_ram_address="0x1124d", stock_packet_app_offset=hex(prefix_offset),
                  special_branch_address="0x1e102a0", special_branch_target="0x1c934",
                  updater_takes_this_special_branch=False,
                  other_receive_paths_fully_audited=False, usb_ota_support_established=False,
                  independent_recovery_established=False,
                  interpretation="M-UPGRADE's 7F command does not take V13's observed 7D special "
                                 "USB branch. This alone neither proves all USB update paths absent "
                                 "nor supplies recovery when the application cannot run.")
    output = root / "entry-audit.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
