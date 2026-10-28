"""Prepare a device-specific four-sector installation/rollback plan, offline.

Requires two identical full physical backups, the exact stock release and the
reviewed pacing package. Preserves every byte outside the four changed sectors.
Private flash images and generated plans belong only under ignored artifacts/.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from inspect_firmware import HERE, RELEASES, inspect_container, require, sha
from package_sysex_pacing import read_build, validate_package

PACKAGE_SHA256 = "cd3ea30c4c4c662baf274f55e9e5d964a5879ccc3a03a7bf7a34ee8ba38c2a00"
FULL_SIZE, SECTOR_SIZE, PAGE_SIZE = 0x40000, 4096, 256
SECTOR_ORDER = (0x27000, 0x10000, 0x12000, 0x1000)
STOCK_END, PATCHED_END = 0x27600, 0x27700


def vm_geometry(start, reservation_size):
    """Stock V13 at 1e14da6..1e14e38, after GET_VM_INFO.

    The native code clamps length to 16384. At that length it forces 4-KiB
    mode even when boot_info.align is 1. It rounds the start up to 4096 and
    usable length down to 8192 before dividing into two banks. This is a
    translation of the audited stock instructions, not generic SDK defaults.
    """
    length = min(reservation_size, 16384)
    require(length == 16384, "VM extent is outside the audited 4-KiB branch")
    aligned = (start + 4095) & -4096
    length = (start + length - aligned) & -8192
    return dict(start=aligned, bank_bytes=length // 2, second_bank=aligned + length // 2,
                end=aligned + length)


def construct_target(original, stock, patched):
    require(len(original) == FULL_SIZE, "Full physical backup must be 256 KiB")
    require(len(stock) == STOCK_END and len(patched) == PATCHED_END, "Unexpected firmware geometry")
    require(original[:len(stock)] == stock, "Device firmware differs from the exact official flash2 image")
    require(original[len(stock):len(patched)] == b"\xff" * (len(patched)-len(stock)),
            "Candidate extension contains occupied device data")
    before = vm_geometry(STOCK_END, 0x37000-STOCK_END)
    after = vm_geometry(PATCHED_END, 0x37000-PATCHED_END)
    require(before == after, "Patch changes the native VM bank geometry")
    target = patched + original[len(patched):]
    changed = {pos for pos in range(0, FULL_SIZE, SECTOR_SIZE)
               if original[pos:pos+SECTOR_SIZE] != target[pos:pos+SECTOR_SIZE]}
    require(changed == set(SECTOR_ORDER), "Changes extend beyond the four reviewed sectors")
    require(target[:0x1000] == original[:0x1000] and target[PATCHED_END:] == original[PATCHED_END:],
            "Boot or reserved device data changed")
    return target, before


def package_images():
    stock = (HERE / "artifacts/FootCtrlPlus_013.fwsc").read_bytes()
    require(sha(stock) == RELEASES["013"]["stock_sha256"], "Unknown original package")
    candidate = (HERE / "artifacts/sysex-pacing-package/FootCtrlPlus_013_midigap_EXPERIMENTAL_NOT_INSTALLED.fwsc").read_bytes()
    require(sha(candidate) == PACKAGE_SHA256, "Pacing package differs from the reviewed candidate")
    blob, hooks, _ = read_build(HERE / "artifacts/sysex-pacing")
    validate_package(candidate, stock, blob, hooks)
    _, old = inspect_container(stock)
    _, new = inspect_container(candidate)
    return old["flash2.bin"], new["flash2.bin"]


def load_plan(directory):
    directory = Path(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    original = (directory / "original.bin").read_bytes()
    target = (directory / "patched.bin").read_bytes()
    require(plan.get("package_sha256") == PACKAGE_SHA256 and
            sha(original) == plan.get("original_sha256") and sha(target) == plan.get("patched_sha256"),
            "Plan fingerprint mismatch")
    stock, patched = package_images()
    expected, geometry = construct_target(original, stock, patched)
    require(target == expected and plan.get("vm_geometry") == geometry, "Plan differs from validated reconstruction")
    require(plan.get("sector_order") == list(SECTOR_ORDER), "Plan sector order differs")
    return plan, original, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "artifacts/sysex-install")
    args = parser.parse_args()
    report = json.loads((args.backup / "report.json").read_text(encoding="utf-8"))
    original = (args.backup / "flash-1.bin").read_bytes()
    require(report.get("status") == "two_physical_reads_verified" and report.get("identical") is True
            and report.get("flash_writes") == report.get("flash_erases") == 0
            and report.get("identity") == "BR25 UBOOT1.00" and report.get("jedec_id") == "854512",
            "Backup diagnostic did not complete with the expected device")
    require(original == (args.backup / "flash-2.bin").read_bytes()
            and report.get("sha256") == [sha(original)] * 2, "Backup copies or fingerprints differ")
    stock, patched = package_images()
    target, geometry = construct_target(original, stock, patched)
    plan = dict(status="prepared_NOT_INSTALLED", package_sha256=PACKAGE_SHA256,
                original_sha256=sha(original), patched_sha256=sha(target), flash_bytes=FULL_SIZE,
                sector_order=list(SECTOR_ORDER), vm_geometry=geometry,
                original_vm_reservation_start=STOCK_END, patched_vm_reservation_start=PATCHED_END,
                preserved_from=PATCHED_END, boot_unchanged=True,
                config_sha256=sha(original[0x39000:0x3f000]),
                sector_hashes=[dict(address=pos, original_sha256=sha(original[pos:pos+SECTOR_SIZE]),
                                    patched_sha256=sha(target[pos:pos+SECTOR_SIZE])) for pos in SECTOR_ORDER],
                activation="Module sector first, USB builder hook, HOST sender hook, directory/CRCs last",
                independent_cold_recovery_verified=False, native_execution_verified=False)
    args.output.mkdir(parents=True, exist_ok=True)
    require(not any((args.output / name).exists() for name in ("original.bin", "patched.bin", "plan.json")),
            "Plan already exists; refusing to replace its immutable backup")
    for name, data in (("original.bin", original), ("patched.bin", target)):
        with (args.output / name).open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    with (args.output / "plan.json").open("x", encoding="utf-8") as file:
        json.dump(plan,file,indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    load_plan(args.output)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
