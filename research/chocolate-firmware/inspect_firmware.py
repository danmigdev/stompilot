"""Offline inspection of the official Chocolate Plus 012/013 updates. No device I/O.

Format references: kagaimiq/jl-misctools (commit
0a5b12db0ef38f3042acffbe2452730a37fd2405), and the existing TANK study.
The output contains original vendor components, never a patched update.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import json
from pathlib import Path
import struct
import urllib.request

HERE = Path(__file__).resolve().parent
URL_BASE = "https://yms-file-store.oss-cn-hongkong.aliyuncs.com/software/firmware/FootCtrlPlus/"
RELEASES = {
    "012": dict(stock_sha256="9deb9d90691415aa7f4514103842237a1500c681db34fe35e5f5136d173cb3bc",
                app_sha256="a289aad3cfd4e7902653dcf29d40f2e1f550d2bc3eeefa3b057ed86f49ff841a"),
    "013": dict(stock_sha256="b5ba2041482640315c7dc70a0f7c79c9e81d24989226763868dc36dab81801c3",
                app_sha256="f535c717a1ce23ef1b0fe5251c0ab7c906f2be068d2c172ad8440be1e0edeadc"),
}
COMPONENTS = ("flash.bin", "info.log", "isd_config.ini", "ota.bin",
              "script.ver", "flash2.bin", "blimit.bin", "tail.bin")
APP_BASE = 0x1000
ENTRY_POINT = 0x01E00120


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def crc(data: bytes) -> int:
    return binascii.crc_hqx(data, 0)


def enc(data: bytes, seed: int = 0xFFFF) -> bytes:
    """Symmetric JL ENC scrambling; not a signature verification."""
    result = bytearray(data)
    for i in range(len(result)):
        result[i] ^= seed & 0xFF
        seed = ((seed << 1) ^ (0x1021 if seed & 0x8000 else 0)) & 0xFFFF
    return bytes(result)


def sfc(data: bytes, address: int, key: int) -> bytes:
    require(address % 32 == 0, "Unaligned SFC extent")
    return b"".join(enc(data[i:i + 32], key ^ ((address + i) >> 2))
                    for i in range(0, len(data), 32))


def chip_key(tail: bytes) -> int:
    require(len(tail) == 64, "Unexpected tail size")
    require(crc(tail[:32]) == int.from_bytes(tail[32:34], "little"), "Tail key CRC mismatch")
    require(tail[48:] == b"JLUFW" + bytes(11), "Unexpected tail marker")
    threshold = sum(tail[:16]) & 0xFF
    if threshold >= 0xE0:
        threshold = 0xAA
    elif threshold <= 0x10:
        threshold = 0x55
    return sum(1 << i for i in range(16) if (tail[16 + i] ^ tail[15 - i]) < threshold)


def inspect_container(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Validate UFW fields and CRCs independently of the CLI's source hash guard."""
    require(len(data) >= 1044, "Truncated FWSC")
    extra = data[47:960:48]
    version = next((v for v in RELEASES if extra[:16] == bytes(
        x + i + 1 for i, x in enumerate(f"FootCtrlPlus_{v}".encode()))), None)
    require(version is not None and extra[16:] == b"@@@@", "Unexpected FWSC label")
    logical = b"".join(data[i:i + 47] for i in range(0, 960, 48)) + data[960:]
    header = enc(logical[:64])
    hcrc, table_crc, size, count, flags, alignment, chip = struct.unpack("<HHIHHI48s", header)
    require(crc(header[2:]) == hcrc, "UFW header CRC mismatch")
    require(size == len(logical), "UFW length mismatch")
    require(count == 8 and flags == 4 and alignment == 512, "Unexpected UFW layout")
    require(chip.split(b"\0")[0] == b"AC636N", "Unexpected chip family")
    table = logical[64:64 + count * 80]
    require(crc(table) == table_crc, "UFW table CRC mismatch")
    entries = []
    extents = []
    for i in range(count):
        typ, index, checksum, unknown, offset, size, stored, reserved, name = struct.unpack(
            "<HHHHIII44s16s", enc(table[i * 80:(i + 1) * 80]))
        name = name.split(b"\0")[0].decode("ascii")
        require(name == COMPONENTS[i] and index == i and unknown == 0, "Unexpected UFW entry")
        require(0 <= size <= stored and offset >= 1024 and offset + stored <= len(logical),
                f"Invalid extent: {name}")
        encrypted_size = int.from_bytes(reserved[4:8], "little")
        require(reserved[:4] == bytes(4) and reserved[8:] == bytes(36)
                and encrypted_size in (0, stored), f"Unexpected encryption extent: {name}")
        if stored:
            require(all(offset + stored <= start or offset >= end for start, end in extents),
                    "Overlapping UFW entries")
            extents.append((offset, offset + stored))
        entries.append(dict(name=name, type=typ, index=index, offset=offset, size=size,
                            stored_size=stored, encrypted=bool(encrypted_size), crc16=checksum))
    tail = entries[-1]
    key = chip_key(logical[tail["offset"]:tail["offset"] + tail["size"]])
    require(key == 0x980F, "Unexpected chip key")
    payloads = {}
    rebuilt = bytearray(logical)
    for e in entries:
        offset, stored = e["offset"], e["stored_size"]
        payload = logical[offset:offset + stored]
        if e["encrypted"]:
            payload = sfc(payload, offset, key)
        require(crc(payload[:e["size"]]) == e["crc16"], f"UFW payload CRC mismatch: {e['name']}")
        e["sha256"] = sha(payload[:e["size"]])
        payloads[e["name"]] = payload[:e["size"]]
        rebuilt[offset:offset + stored] = sfc(payload, offset, key) if e["encrypted"] else payload
    for i in range(count):
        pos = 64 + i * 80
        rebuilt[pos:pos + 80] = enc(enc(logical[pos:pos + 80]))
    rebuilt[:64] = enc(header)
    roundtrip = b"".join(rebuilt[i * 47:(i + 1) * 47] + extra[i:i + 1]
                         for i in range(20)) + rebuilt[940:]
    require(roundtrip == data, "Unmodified decrypt/re-encrypt roundtrip differs")
    label = f"FootCtrlPlus_{version}"
    return dict(source_url=URL_BASE + label + ".fwsc", sha256=sha(data), size=len(data), label=label,
                version=version,
                chip="AC636N", chip_key=key, crc_checks="header, table, tail key, all 8 payloads",
                identical_roundtrip=True, entries=entries), payloads


def jlfs_entry(data: bytes, pos: int) -> dict:
    require(0 <= pos <= len(data) - 32, "JLFS header outside image")
    hcrc, dcrc, offset, size, flags, reserved, index, name = struct.unpack_from(
        "<HHIIBBH16s", data, pos)
    require(crc(data[pos + 2:pos + 32]) == hcrc, f"JLFS header CRC mismatch at {pos:#x}")
    name = name.split(b"\0")[0].decode("ascii")
    require(name and all(c.isalnum() or c in "_." for c in name), "Invalid JLFS name")
    return dict(name=name, header_offset=pos, offset=offset, size=size, flags=flags,
                reserved=reserved, index=index, data_crc16=dcrc)


def inspect_flash(raw: bytes, key: int, version: str) -> tuple[dict, dict[str, bytes]]:
    require(len(raw) in (0x27600, 0x28000), "Unexpected flash image size")
    header = enc(raw[:32])
    require(crc(header[2:]) == int.from_bytes(header[:2], "little"), "Flash header CRC mismatch")
    plain = raw[:APP_BASE] + sfc(raw[APP_BASE:], 0, key)
    area = jlfs_entry(plain, APP_BASE)
    require(area["name"] == "app_area_head" and area["offset"] == ENTRY_POINT,
            "Unexpected app entry point")
    area_end = APP_BASE + area["size"]
    require(APP_BASE + 32 <= area_end <= len(plain), "App area outside image")
    require(crc(plain[APP_BASE + 32:area_end]) == area["data_crc16"], "App area CRC mismatch")
    entries = []
    payloads = {}
    for pos in range(0x1020, 0x1120, 32):
        e = jlfs_entry(plain, pos)
        if not e["flags"] & 0x10:
            start = APP_BASE + e["offset"]
            require(0x1120 <= start <= start + e["size"] <= area_end, "App file outside area")
            payload = plain[start:start + e["size"]]
            require(crc(payload) == e["data_crc16"], f"App file CRC mismatch: {e['name']}")
            payloads[e["name"]] = payload
        # Reserved-region offsets are recorded as stored, not shifted by APP_BASE.
        entries.append(e)
        if e["index"]:
            break
    else:
        raise ValueError("Unterminated app directory")
    require("app.bin" in payloads and sha(payloads["app.bin"]) == RELEASES[version]["app_sha256"],
            "Unexpected application code")
    user = next(e for e in entries if e["name"] == "USRFLASH")
    require((user["offset"], user["size"], user["flags"]) == (0x39000, 0x6000, 0x92),
            "Unexpected configuration reservation")
    require(sfc(plain[APP_BASE:], 0, key) == raw[APP_BASE:], "Flash roundtrip differs")
    return dict(size=len(raw), sha256=sha(raw), app_sha256=sha(payloads["app.bin"]),
                entry_point=ENTRY_POINT, entries=entries, usrflash=user,
                crc_checks="flash header, app area, app directory headers, app files"), payloads


def compare_config(before: bytes, after: bytes) -> dict:
    require(len(before) == len(after) == 23646, "Expected two 23646-byte configuration snapshots")
    differences = [(i, a, b) for i, (a, b) in enumerate(zip(before, after)) if a != b]
    sysex = {93 + switch * 417 + 161 + byte for switch in range(32) for byte in range(256)}
    ff = [(i, a, b) for i, a, b in differences if i in sysex and a == 255]
    return dict(before_sha256=sha(before), after_sha256=sha(after), changed_bytes=len(differences),
                cleared_bits=sum((a & ~b).bit_count() for _, a, b in differences),
                set_bits=sum((b & ~a).bit_count() for _, a, b in differences),
                changed_outside_sysex=sum(i not in sysex for i, _, _ in differences),
                ff_sysex_bytes=sum(before[i] == 255 for i in sysex),
                changed_ff_sysex_bytes=len(ff),
                cleared_ff_sysex_bits=sum((a & ~b).bit_count() for _, a, b in ff))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch the pinned official stock update")
    parser.add_argument("--version", choices=RELEASES, default="013")
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    expected = RELEASES[args.version]
    args.firmware = args.firmware or HERE / f"artifacts/FootCtrlPlus_{args.version}.fwsc"
    args.output = args.output or HERE / f"artifacts/inspection-{args.version}"
    if args.download and not args.firmware.exists():
        with urllib.request.urlopen(URL_BASE + f"FootCtrlPlus_{args.version}.fwsc", timeout=40) as response:
            data = response.read()
        require(sha(data) == expected["stock_sha256"], "Download differs from the audited stock package")
        args.firmware.parent.mkdir(parents=True, exist_ok=True)
        args.firmware.write_bytes(data)
    data = args.firmware.read_bytes()
    require(sha(data) == expected["stock_sha256"], "Firmware differs from the selected pinned stock package")
    manifest, payloads = inspect_container(data)
    require(manifest["version"] == args.version, "Firmware version mismatch")
    manifest["flash_images"] = {}
    extracted = {}
    for name in ("flash.bin", "flash2.bin"):
        info, files = inspect_flash(payloads[name], manifest["chip_key"], args.version)
        manifest["flash_images"][name] = info
        extracted[name] = files
    require(extracted["flash.bin"]["app.bin"] == extracted["flash2.bin"]["app.bin"],
            "The two flash images contain different application code")
    report = HERE.parents[1] / "m-vave-report"
    manifest["configuration_evidence"] = {}
    for label in ("test1", "test2"):
        before, after = report / f"{label}-written.bin", report / f"{label}-read-after-H.bin"
        if before.exists() and after.exists():
            manifest["configuration_evidence"][label] = compare_config(before.read_bytes(), after.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (args.output / name).write_bytes(payload)
    for name, files in extracted.items():
        dest = args.output / f"{name}-files"
        dest.mkdir(exist_ok=True)
        for filename, payload in files.items():
            (dest / filename).write_bytes(payload)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {manifest['label']}: {manifest['chip']}; both apps {expected['app_sha256']}")
    print("All UFW payload CRCs and app CRCs match; unchanged roundtrip is byte-identical.")
    print(json.dumps(manifest["configuration_evidence"], indent=2))
    print(f"Original components and manifest: {args.output}")


if __name__ == "__main__":
    main()
