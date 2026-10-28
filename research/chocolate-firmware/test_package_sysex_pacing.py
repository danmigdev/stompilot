"""Offline integrity and tamper-rejection checks; requires the local build."""
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from inspect_firmware import APP_BASE, HERE, crc, inspect_container, jlfs_entry, sfc
from package_sysex_pacing import (HOOKS, PATCH_OFFSET, RESOURCE_HEADER, decode_branch, entry_bytes, patch_flash, read_build,
                              rebuild_container, validate_flash, validate_package)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (HERE / "artifacts/FootCtrlPlus_013.fwsc").read_bytes()
        cls.blob, cls.hooks, cls.build = read_build(HERE / "artifacts/sysex-pacing")
        cls.meta, cls.parts = inspect_container(cls.original)
        cls.key = cls.meta["chip_key"]
        cls.images = {n: patch_flash(cls.parts[n], cls.key, cls.blob, cls.hooks)
                      for n in ("flash.bin", "flash2.bin")}
        cls.candidate = rebuild_container(cls.original, cls.images)

    def check_candidate(self, data):
        return validate_package(data, self.original, self.blob, self.hooks)

    def reseal_flash(self, name, mutate):
        raw = self.images[name]
        # The directory's area size is independent of the image's raw FF padding.
        prefix = raw[:APP_BASE] + sfc(raw[APP_BASE:APP_BASE + 32], 0, self.key)
        end = APP_BASE + jlfs_entry(prefix, APP_BASE)["size"]
        plain = bytearray(raw[:APP_BASE] + sfc(raw[APP_BASE:0x276A0], 0, self.key))
        mutate(plain)
        for pos in range(0x1020, 0x1100, 32):
            e = jlfs_entry(plain, pos)
            if not e["flags"] & 0x10:
                start = APP_BASE + e["offset"]
                e["data_crc16"] = crc(plain[start:start + e["size"]])
                plain[pos:pos + 32] = entry_bytes(e, plain[pos:pos + 32])
        area = jlfs_entry(plain, APP_BASE)
        area["data_crc16"] = crc(plain[0x1020:end])
        plain[APP_BASE:APP_BASE + 32] = entry_bytes(area, plain[APP_BASE:APP_BASE + 32])
        return bytes(plain[:APP_BASE]) + sfc(bytes(plain[APP_BASE:]), 0, self.key) + raw[len(plain):]

    def test_original_rebuild_is_identical(self):
        self.assertEqual(rebuild_container(self.original, {}), self.original)

    def test_complete_candidate(self):
        result = self.check_candidate(self.candidate)
        self.assertEqual(result["status"], "offline_verified_NOT_INSTALLED")
        self.assertEqual(result["device_writes"], 0)
        self.assertEqual(result["bytes"], 583732)
        self.assertEqual(len(self.images["flash.bin"]), 0x28000)
        self.assertEqual(len(self.images["flash2.bin"]), 0x27700)

    def test_encrypted_component_position_and_bytes_are_identical(self):
        meta, parts = inspect_container(self.candidate)
        before = next(e for e in self.meta["entries"] if e["name"] == "blimit.bin")
        after = next(e for e in meta["entries"] if e["name"] == "blimit.bin")
        self.assertEqual(after["offset"] - before["offset"], 256)
        self.assertTrue(after["encrypted"])
        self.assertEqual(parts["blimit.bin"], self.parts["blimit.bin"])

    def test_tampered_outer_header_table_payload_and_label(self):
        for offset in (0, 47, 80, 2000, len(self.candidate) - 16):
            with self.subTest(offset=offset):
                bad = bytearray(self.candidate)
                bad[offset] ^= 0x40
                with self.assertRaises(ValueError):
                    self.check_candidate(bytes(bad))

    def test_truncations(self):
        for size in (0, 1000, len(self.candidate) - 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.check_candidate(self.candidate[:size])

    def test_wrong_stock_version(self):
        v12 = (HERE / "artifacts/FootCtrlPlus_012.fwsc").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unrecognized original"):
            validate_package(self.candidate, v12, self.blob, self.hooks)

    def test_no_other_replacement_components(self):
        with self.assertRaisesRegex(ValueError, "Unexpected component"):
            rebuild_container(self.original, {"ota.bin": self.parts["ota.bin"]})

    def test_inner_crc_is_checked_even_with_valid_outer_crcs(self):
        raw = bytearray(self.images["flash.bin"])
        raw[0x1200] ^= 1
        bad = rebuild_container(self.original, dict(self.images, **{"flash.bin": bytes(raw)}))
        with self.assertRaisesRegex(ValueError, "CRC"):
            self.check_candidate(bad)

    def test_app_change_outside_hooks_rejected_after_resealing_all_crcs(self):
        raw = self.reseal_flash("flash.bin", lambda p: p.__setitem__(0x1200, p[0x1200] ^ 1))
        with self.assertRaisesRegex(ValueError, "outside the two"):
            validate_flash(raw, self.parts["flash.bin"], self.key, self.blob, self.hooks)

    def test_bootloader_changes_rejected(self):
        raw = bytearray(self.images["flash.bin"])
        raw[0x80] ^= 1
        with self.assertRaisesRegex(ValueError, "Boot/header"):
            validate_flash(bytes(raw), self.parts["flash.bin"], self.key, self.blob, self.hooks)

    def test_reserved_area_change_rejected_after_resealing(self):
        def move_user(plain):
            e = jlfs_entry(plain, 0x10E0)
            e["offset"] -= 0x1000
            plain[0x10E0:0x1100] = entry_bytes(e)
        raw = self.reseal_flash("flash2.bin", move_user)
        with self.assertRaisesRegex(ValueError, "metadata: USRFLASH"):
            validate_flash(raw, self.parts["flash2.bin"], self.key, self.blob, self.hooks)

    def test_module_corruption_rejected_after_resealing(self):
        raw = self.reseal_flash("flash.bin", lambda p: p.__setitem__(PATCH_OFFSET, p[PATCH_OFFSET] ^ 1))
        with self.assertRaisesRegex(ValueError, "Patch code differs"):
            validate_flash(raw, self.parts["flash.bin"], self.key, self.blob, self.hooks)

    def test_raw_padding_is_checked(self):
        raw = bytearray(self.images["flash.bin"])
        raw[-1] = 0
        with self.assertRaisesRegex(ValueError, "Non-FF"):
            validate_flash(bytes(raw), self.parts["flash.bin"], self.key, self.blob, self.hooks)

    def test_exported_hook_must_match_linked_elf(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            for filename in ("sysex-pacing.elf", "sysex-pacing.bin", *(f"hook-{n}.bin" for n in HOOKS)):
                shutil.copyfile(HERE / "artifacts/sysex-pacing" / filename, dest / filename)
            (dest / "hook-ready.bin").write_bytes(self.hooks["boundary"])
            with self.assertRaisesRegex(ValueError, "Exported hook differs"):
                read_build(dest)

    def test_config_dat_change_is_rejected(self):
        raw = self.reseal_flash("flash.bin", lambda p: p.__setitem__(0x27410, p[0x27410] ^ 1))
        with self.assertRaisesRegex(ValueError, "config.dat resource CRC"):
            validate_flash(raw, self.parts["flash.bin"], self.key, self.blob, self.hooks)

    def test_branch_wrap_and_signed_displacement(self):
        self.assertEqual(decode_branch(bytes.fromhex("bfea2af8"), 0x1E119CC, "call"), 0x1E10A24)
        self.assertEqual(decode_branch(bytes.fromhex("97ea7b8c"), 0x1E2647E, "call"), 0x117D78)
        with self.assertRaises(ValueError):
            decode_branch(bytes.fromhex("cfea65af"), 0x1E266F4, "call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
