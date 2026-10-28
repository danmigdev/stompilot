"""Build/validate the offline V13 USB HOST SysEx pacing package. No device or updater I/O.

The pinned original is required. This checks structural integrity, not bootloader
acceptance, native execution, recovery, or safety to install on a unique unit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

from inspect_firmware import (APP_BASE, ENTRY_POINT, HERE, RELEASES, crc, enc,
                              inspect_container, inspect_flash, jlfs_entry,
                              require, sfc, sha)

PATCH_ADDRESS = 0x1E265C0
PATCH_OFFSET = 0x275C0
RESOURCE_HEADER = 0x275A0
AREA_END = 0x273E0
CONFIG_HEADER = 0x273E0
MD5_HEADER = 0x27560
HOOKS = {
    "boundary": (0x1E0FF54, bytes.fromhex("78187518"), "ch_v13_boundary_glue", "goto"),
    "ready": (0x1E119CC, bytes.fromhex("bfea2af8"), "ch_v13_ready_glue", "call"),
}
SOURCE_NAMES = ("sysex_pacing.c", "sysex_pacing.h", "sysex_pacing_glue.S",
                "sysex_pacing.ld", "build_sysex_pacing.sh", "test_sysex_pacing.c",
                "package_sysex_pacing.py", "test_package_sysex_pacing.py",
                "trace_sysex_pacing.py", "inspect_firmware.py")
NATIVE_SYMBOLS = {
    "v13_output_callback": 0x194A0, "v13_usb_buffer": 0x1E11932,
    "v13_usb_status": 0x1E10A24, "v13_task_delay": 0x2117D78,
    "v13_packet_return": 0x1E1004C, "v13_packet_continue": 0x1E0FF58,
}


def align(size: int, boundary: int) -> int:
    return (size + boundary - 1) & -boundary


def decode_branch(data: bytes, address: int, kind: str) -> int:
    """PI32v2 four-byte PC-relative branch, including the 25-bit PC wrap."""
    require(len(data) == 4 and data[1] == 0xEA and
            data[0] & 0xC0 == {"call": 0x80, "goto": 0xC0}[kind], "Invalid long branch")
    delta = ((data[0] & 0x3F) << 16) | int.from_bytes(data[2:], "little")
    if delta & (1 << 21):
        delta -= 1 << 22
    return (address + 4 + delta * 2) & 0x1FFFFFF


def read_build(directory: Path) -> tuple[bytes, dict[str, bytes], dict]:
    """Cross-check exported bytes against linked ELF sections and symbols."""
    elf = (directory / "sysex-pacing.elf").read_bytes()
    require(len(elf) >= 52 and elf[:7] == b"\x7fELF\x01\x01\x01", "Expected ELF32 little endian")
    fields = struct.unpack_from("<16sHHIIIIIHHHHHH", elf)
    _, kind, machine, version, entry, _, table, _, header_size, _, _, stride, count, strings = fields
    require((kind, machine, version, header_size, stride) == (2, 241, 1, 52, 40),
            "Unexpected linked PI32v2 ELF layout")
    require(0 < strings < count < 64 and table + count * stride <= len(elf), "Invalid section table")
    headers = [struct.unpack_from("<10I", elf, table + i * stride) for i in range(count)]

    def extent(header: tuple) -> bytes:
        require(header[1] != 8 and header[4] + header[5] <= len(elf), "Invalid file-backed section")
        return elf[header[4]:header[4] + header[5]]

    def string(data: bytes, offset: int) -> str:
        require(0 <= offset < len(data) and b"\0" in data[offset:], "Invalid ELF string")
        return data[offset:].split(b"\0", 1)[0].decode("ascii")

    names = extent(headers[strings])
    sections = {string(names, h[0]): h for h in headers}
    allowed = {".patch", *(".hook_" + name for name in HOOKS)}
    require({name for name, h in sections.items() if h[2] & 2 and h[5]} == allowed,
            "Unexpected allocated section (including mutable RAM)")
    require(not any(h[2] & 1 and h[2] & 2 and h[5] for h in headers), "Writable allocated section")
    require(not any(h[1] in (4, 9) and h[5] for h in headers), "Unresolved relocations")
    symtab = sections[".symtab"]
    require(symtab[9] == 16 and symtab[5] % 16 == 0 and symtab[6] < count, "Invalid symbols")
    symbol_strings = extent(headers[symtab[6]])
    symbols = {}
    for pos in range(0, symtab[5], 16):
        name, value, size, info, other, section = struct.unpack_from("<IIIBBH", extent(symtab), pos)
        require(not (section == 0 and name != 0), "Undefined symbol")
        if name:
            symbols[string(symbol_strings, name)] = value
    require(all(symbols.get(name) == address for name, address in NATIVE_SYMBOLS.items()),
            "Native ABI binding differs from audited V13")
    blob = (directory / "sysex-pacing.bin").read_bytes()
    require(sections[".patch"][3] == PATCH_ADDRESS and extent(sections[".patch"]) == blob,
            "Blob and ELF section/address disagree")
    require(0 < len(blob) <= 0x120, "Unexpected patch length")
    require(entry == symbols["ch_v13_boundary_glue"], "Unexpected entry symbol")
    hooks = {}
    for name, (address, _, symbol, kind) in HOOKS.items():
        data = (directory / f"hook-{name}.bin").read_bytes()
        require(len(data) == 4 and sections[".hook_" + name][3] == address and
                data == extent(sections[".hook_" + name]), f"Exported hook differs: {name}")
        target = int.from_bytes(data, "little") if kind == "pointer" else decode_branch(data, address, kind)
        require(target == symbols[symbol] and PATCH_ADDRESS <= target < PATCH_ADDRESS + len(blob),
                f"Hook target differs from linked symbol: {name}")
        hooks[name] = data
    return blob, hooks, dict(elf_sha256=sha(elf), blob_sha256=sha(blob), blob_bytes=len(blob),
                             base=hex(PATCH_ADDRESS), symbols={n: hex(symbols[n]) for n in
                             ("ch_v13_host_boundary", "ch_v13_host_ready",
                              "ch_v13_boundary_glue", "ch_v13_ready_glue")})


def entry_bytes(entry: dict, template: bytes | None = None) -> bytes:
    raw = struct.pack("<HHIIBBH16s", 0, entry["data_crc16"], entry["offset"], entry["size"],
                      entry["flags"], entry["reserved"], entry["index"], entry["name"].encode("ascii"))
    if template is not None:
        require(len(template) == 32, "Invalid original entry template")
        require(template[16:].split(b"\0", 1)[0] == entry["name"].encode("ascii"),
                "Original entry name mismatch")
        raw = raw[:16] + template[16:]
    return struct.pack("<H", crc(raw[2:])) + raw[2:]


def resources(plain: bytes, app: bytes) -> tuple[bytes, dict]:
    """Validate the two original sequential resources beyond app_area_head."""
    cfg = jlfs_entry(plain, CONFIG_HEADER)
    md5 = jlfs_entry(plain, MD5_HEADER)
    require((cfg["name"], cfg["size"], cfg["offset"], cfg["flags"], cfg["index"]) ==
            ("config.dat", 0x180, 32, 0x82, 0), "Unexpected config.dat resource")
    require((md5["name"], md5["size"], md5["offset"], md5["flags"]) ==
            ("md5.bin", 64, 32, 0x82), "Unexpected md5.bin resource")
    require(crc(plain[CONFIG_HEADER + 32:MD5_HEADER]) == cfg["data_crc16"],
            "config.dat resource CRC mismatch")
    value = plain[MD5_HEADER + 32:RESOURCE_HEADER]
    require(value == hashlib.md5(app).hexdigest().encode("ascii") and
            crc(value) == md5["data_crc16"], "Application MD5 resource mismatch")
    return bytes(plain[CONFIG_HEADER:MD5_HEADER]), md5


def patch_flash(raw: bytes, key: int, blob: bytes, hooks: dict[str, bytes]) -> bytes:
    stock, files = inspect_flash(raw, key, "013")
    plain = bytearray(raw[:APP_BASE] + sfc(raw[APP_BASE:RESOURCE_HEADER], 0, key))
    area = jlfs_entry(plain, APP_BASE)
    require(APP_BASE + area["size"] == AREA_END, "Original app extent differs")
    _, md5 = resources(plain, files["app.bin"])
    require(md5["index"] == 1, "Original resources do not terminate at MD5")
    require(raw[RESOURCE_HEADER:] == b"\xff" * (len(raw) - RESOURCE_HEADER),
            "Original raw padding is not blank")
    padded = blob + b"\xff" * (align(len(blob), 32) - len(blob))
    end = PATCH_OFFSET + len(padded)
    image_size = align(end, 4096 if len(raw) == 0x28000 else 256)
    require(image_size <= 0x28000, "Patch exceeds audited candidate image extent")
    for name, (address, original, _, _) in HOOKS.items():
        offset = address - ENTRY_POINT + 0x1120
        require(plain[offset:offset + 4] == original, f"Stock hook mismatch: {name}")
        require(len(hooks[name]) == 4, "Invalid hook length")
        plain[offset:offset + 4] = hooks[name]
    app_entry = next(e for e in stock["entries"] if e["name"] == "app.bin")
    start = APP_BASE + app_entry["offset"]
    app = bytes(plain[start:start + app_entry["size"]])
    for e in stock["entries"]:
        if e["name"] == "app.bin":
            e["data_crc16"] = crc(app)
        elif e["name"] == "VM":
            e.update(offset=image_size, size=0x37000 - image_size)
        elif e["name"] == "PRCT":
            e["size"] = image_size
        pos = e["header_offset"]
        plain[pos:pos + 32] = entry_bytes(e, plain[pos:pos + 32])
    area["data_crc16"] = crc(plain[APP_BASE + 32:AREA_END])
    plain[APP_BASE:APP_BASE + 32] = entry_bytes(area, plain[APP_BASE:APP_BASE + 32])
    value = hashlib.md5(app).hexdigest().encode("ascii")
    md5.update(index=0, data_crc16=crc(value))
    plain[MD5_HEADER:RESOURCE_HEADER] = entry_bytes(md5, plain[MD5_HEADER:MD5_HEADER + 32]) + value
    resource = dict(name="midigap.bin", offset=32, size=32 + len(padded),
                    flags=0x82, reserved=0xFF, index=1, data_crc16=crc(padded))
    plain.extend(entry_bytes(resource) + padded)
    require(len(plain) == end, "Candidate resource extent mismatch")
    return bytes(plain[:APP_BASE]) + sfc(bytes(plain[APP_BASE:]), 0, key) + b"\xff" * (image_size - end)


def rebuild_container(original: bytes, replacements: dict[str, bytes]) -> bytes:
    """Preserve every unchanged stored payload, including encryption/padding."""
    manifest, _ = inspect_container(original)
    require(set(replacements) <= {"flash.bin", "flash2.bin"}, "Unexpected component replacement")
    extra = original[47:960:48]
    logical = b"".join(original[i:i + 47] for i in range(0, 960, 48)) + original[960:]
    rebuilt = bytearray(logical[:1024])
    old_cursor = 1024
    for i, entry in enumerate(manifest["entries"]):
        require(entry["offset"] == old_cursor and old_cursor % 32 == 0,
                "Original component placement changed")
        table_pos = 64 + i * 80
        fields = list(struct.unpack("<HHHHIII44s16s", enc(logical[table_pos:table_pos + 80])))
        offset = entry["offset"]
        stored = logical[offset:offset + entry["stored_size"]]
        old_cursor += len(stored)
        if entry["encrypted"]:
            stored = sfc(stored, offset, manifest["chip_key"])
        if entry["name"] in replacements:
            payload = replacements[entry["name"]]
            require(len(payload) % 32 == 0, "Replacement component is unaligned")
            stored = payload
            fields[2], fields[5], fields[6] = crc(payload), len(payload), len(payload)
        fields[4] = len(rebuilt)
        if entry["encrypted"]:
            fields[7] = bytes(4) + len(stored).to_bytes(4, "little") + bytes(36)
            stored = sfc(stored, fields[4], manifest["chip_key"])
        rebuilt.extend(stored)
        rebuilt[table_pos:table_pos + 80] = enc(struct.pack("<HHHHIII44s16s", *fields))
    require(old_cursor == len(logical), "Unexpected bytes after original components")
    header = bytearray(enc(logical[:64]))
    struct.pack_into("<HI", header, 2, crc(rebuilt[64:704]), len(rebuilt))
    struct.pack_into("<H", header, 0, crc(header[2:]))
    rebuilt[:64] = enc(header)
    return b"".join(rebuilt[i * 47:(i + 1) * 47] + extra[i:i + 1] for i in range(20)) + rebuilt[940:]


def validate_flash(raw: bytes, original: bytes, key: int, blob: bytes,
                   hooks: dict[str, bytes]) -> dict:
    """Independently parse/diff app, sequential resources, and reservations."""
    stock, files = inspect_flash(original, key, "013")
    require(raw[:APP_BASE] == original[:APP_BASE], "Boot/header bytes changed")
    padded = blob + b"\xff" * (align(len(blob), 32) - len(blob))
    end = PATCH_OFFSET + len(padded)
    expected_size = align(end, 4096 if len(original) == 0x28000 else 256)
    require(len(raw) == expected_size <= 0x28000, "Image/reservation size mismatch")
    plain = raw[:APP_BASE] + sfc(raw[APP_BASE:end], 0, key)
    before = original[:APP_BASE] + sfc(original[APP_BASE:RESOURCE_HEADER], 0, key)
    area, stock_area = jlfs_entry(plain, APP_BASE), jlfs_entry(before, APP_BASE)
    require(all(area[k] == stock_area[k] for k in area if k != "data_crc16"),
            "Unexpected area metadata change")
    require(APP_BASE + area["size"] == AREA_END and
            crc(plain[0x1020:AREA_END]) == area["data_crc16"], "App area CRC mismatch")
    entries = [jlfs_entry(plain, pos) for pos in range(0x1020, 0x1100, 32)]
    require(len(stock["entries"]) == 7 and [e["name"] for e in entries] ==
            [e["name"] for e in stock["entries"]], "App directory changed")
    for e, old in zip(entries, stock["entries"]):
        expected = dict(old)
        if e["name"] == "app.bin":
            expected["data_crc16"] = e["data_crc16"]
        elif e["name"] == "VM":
            expected.update(offset=len(raw), size=0x37000 - len(raw))
        elif e["name"] == "PRCT":
            expected["size"] = len(raw)
        require(e == expected, f"Unexpected directory metadata: {e['name']}")
    actual_files = {}
    for e in entries:
        if not e["flags"] & 0x10:
            start = APP_BASE + e["offset"]
            require(0x1120 <= start < start + e["size"] <= AREA_END, "File extent outside app area")
            value = plain[start:start + e["size"]]
            require(crc(value) == e["data_crc16"], f"File CRC mismatch: {e['name']}")
            actual_files[e["name"]] = value
    require(actual_files["cfg_tool.bin"] == files["cfg_tool.bin"], "Configuration tool changed")
    app = actual_files["app.bin"]
    expected_app = bytearray(files["app.bin"])
    hook_report = []
    for name, (address, old, _, _) in HOOKS.items():
        pos = address - ENTRY_POINT
        require(expected_app[pos:pos + 4] == old, "Unexpected original hook")
        expected_app[pos:pos + 4] = hooks[name]
        hook_report.append(dict(name=name, address=hex(address), original=old.hex(), patched=hooks[name].hex()))
    require(app == expected_app, "App contains changes outside the two verified hooks")
    # Cover alignment gaps and raw name padding as well as parsed files.
    expected_area = bytearray(before[APP_BASE:AREA_END])
    for name, (address, _, _, _) in HOOKS.items():
        pos = address - ENTRY_POINT + 0x120
        expected_area[pos:pos + 4] = hooks[name]
    for e in entries:
        pos = e["header_offset"] - APP_BASE
        expected_area[pos:pos + 32] = entry_bytes(e, before[e["header_offset"]:e["header_offset"] + 32])
    expected_area[:32] = entry_bytes(area, before[APP_BASE:APP_BASE + 32])
    require(bytes(expected_area) == plain[APP_BASE:AREA_END], "Unexpected app area padding change")
    cfg, md5 = resources(plain, app)
    old_cfg, old_md5 = resources(before, files["app.bin"])
    require(cfg == old_cfg and md5["index"] == 0 and old_md5["index"] == 1,
            "Original resources changed or incorrectly terminated")
    module = jlfs_entry(plain, RESOURCE_HEADER)
    require(module == dict(name="midigap.bin", header_offset=RESOURCE_HEADER,
            offset=32, size=32 + len(padded), flags=0x82, reserved=0xFF,
            index=1, data_crc16=crc(padded)), "Patch directory entry mismatch")
    require(plain[PATCH_OFFSET:end] == padded, "Patch code differs from linked build")
    require(plain[0x1100:0x1120] == before[0x1100:0x1120], "Spare app directory bytes changed")
    require(raw[end:] == b"\xff" * (len(raw) - end), "Non-FF raw image padding")
    return dict(bytes=len(raw), sha256=sha(raw), app_sha256=sha(app), patch_sha256=sha(padded),
                app_changed_bytes=sum(a != b for a, b in zip(app, files["app.bin"])),
                area_end=hex(AREA_END), module_address=hex(PATCH_ADDRESS), resource_end=hex(end),
                preserved_config_dat_sha256=sha(cfg), app_md5=hashlib.md5(app).hexdigest(),
                hooks=hook_report, entries=entries)


def validate_package(data: bytes, original: bytes, blob: bytes, hooks: dict[str, bytes]) -> dict:
    require(sha(original) == RELEASES["013"]["stock_sha256"], "Unrecognized original update")
    original_meta, original_payloads = inspect_container(original)
    meta, payloads = inspect_container(data)
    require(meta["label"] == original_meta["label"]
            and data[47:960:48] == original[47:960:48],
            "Protocol identity changed")
    images = {}
    for e, before in zip(meta["entries"], original_meta["entries"]):
        name = e["name"]
        require(all(e[k] == before[k] for k in ("name", "type", "index", "encrypted")),
                "Container entry kind changed")
        if name in ("flash.bin", "flash2.bin"):
            images[name] = validate_flash(payloads[name], original_payloads[name], meta["chip_key"], blob, hooks)
        else:
            require(payloads[name] == original_payloads[name], f"Original component changed: {name}")
    require(images["flash.bin"]["app_sha256"] == images["flash2.bin"]["app_sha256"],
            "The two application images disagree")
    return dict(status="offline_verified_NOT_INSTALLED", sha256=sha(data), bytes=len(data),
                original_sha256=sha(original), protocol_identity=meta["label"],
                unchanged_components=[n for n in payloads if n not in images], flash_images=images,
                checks=["outer header/table/all payload CRCs", "inner directory/area/file CRCs",
                        "two exact hooks only", "unchanged boot/config.dat/cfg_tool and calibration reservations", "identical app in both images"],
                not_verified=["secondary image grows by 256 bytes into its VM reservation; updater migration behavior untested",
                              "modified package acceptance by updater/bootloader", "native patch execution",
                              "measured HOST wire gap and MIDI task stack margin",
                              "recovery after a nonbooting patched application",
                              "Stompilot native verified saving with this pacing variant"],
                device_writes=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, default=HERE / "artifacts/sysex-pacing")
    parser.add_argument("--stock", type=Path, default=HERE / "artifacts/FootCtrlPlus_013.fwsc")
    parser.add_argument("--output", type=Path, default=HERE / "artifacts/sysex-pacing-package")
    args = parser.parse_args()
    original = args.stock.read_bytes()
    require(sha(original) == RELEASES["013"]["stock_sha256"], "Unrecognized original update")
    blob, hooks, build = read_build(args.build)
    traced = json.loads((args.build / "instruction-tests.json").read_text())
    require(traced.get("blob_sha256") == sha(blob) and
            traced.get("hooks") == {name:data.hex() for name,data in hooks.items()},
            "Instruction trace is stale for this linked build")
    manifest, payloads = inspect_container(original)
    require(rebuild_container(original, {}) == original, "Unmodified rebuild differs")
    images = {n: patch_flash(payloads[n], manifest["chip_key"], blob, hooks)
              for n in ("flash.bin", "flash2.bin")}
    candidate = rebuild_container(original, images)
    report = validate_package(candidate, original, blob, hooks)
    report.update(build=build, host_tests=json.loads((args.build / "host-tests.json").read_text()),
                  instruction_tests=traced,
                  stock_rebuild_byte_identical=True,
                  sources={name: sha((HERE / name).read_bytes()) for name in SOURCE_NAMES})
    args.output.mkdir(parents=True, exist_ok=True)
    filename = "FootCtrlPlus_013_midigap_EXPERIMENTAL_NOT_INSTALLED.fwsc"
    (args.output / filename).write_bytes(candidate)
    (args.output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, raw in images.items():
        (args.output / name).write_bytes(raw)
    print(json.dumps({k: report[k] for k in ("status", "sha256", "bytes", "device_writes")}, indent=2))
    print(f"Candidate: {args.output / filename}")


if __name__ == "__main__":
    main()
