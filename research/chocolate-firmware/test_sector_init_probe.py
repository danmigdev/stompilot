"""Host fault tests for the fixed command-21 path; no device access."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe_sector_init import (InitBackend, TABLE, SCRATCH, SLOT, POINTER, ERASE_UNIT,
                                DISABLED_FLASH_BASE, FLASH_BASE,
                                SECTOR, SECTOR_SIZE, PAGE_SIZE, RELATIVE,
                                baseline, expected_pattern, program_image)
from trace_init_erase import trace


class InitTests(unittest.TestCase):
    def setUp(self):
        _, self.backup = baseline()
        self.flash = bytearray(self.backup)
        self.ram = {POINTER: struct.pack("<I", DISABLED_FLASH_BASE), SLOT: struct.pack("<I", SCRATCH),
                    SCRATCH: TABLE, ERASE_UNIT: b"\0"}
        self.events, self.commands, self.programs = [], [], []
        self.erases = 0
        self.lose_erase_ack = False
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.device = Mock()
        self.device._request.side_effect = self.command
        self.backend = InitBackend(self.device, lambda event, **data: self.events.append((event, data)),
                                   self.ram_write, self.backup, Path(self.temp.name))
        for target, fn in (("native_read", self.ram_read), ("spi_read", self.spi_read),
                           ("write_command", self.program)):
            mocked = patch("probe_sector_init." + target, side_effect=fn)
            mocked.start()
            self.addCleanup(mocked.stop)

    def ram_read(self, device, address, size):
        data = self.ram[address]
        self.assertEqual(len(data), size)
        return data

    def ram_write(self, address, data):
        self.assertEqual(address, ERASE_UNIT)
        self.assertIn(data, (b"\0", b"\x10"))
        self.ram[address] = data

    def command(self, command, payload, expected):
        self.commands.append(command)
        self.assertEqual((command, payload, expected), (0x21, b"\x05" + struct.pack("<I", SECTOR), 0))
        if self.ram[ERASE_UNIT] == b"\x10":
            self.erases += 1
            self.flash[RELATIVE:RELATIVE + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
            if self.lose_erase_ack:
                raise TimeoutError("erase happened but acknowledgement was lost")
        return {"payload": b"\0"}

    def spi_read(self, device, address, size):
        offset = address - FLASH_BASE
        return bytes(self.flash[offset:offset + size])

    def program(self, device, kind, address, data):
        self.assertEqual(kind, 5)
        self.programs.append(address)
        offset = address - FLASH_BASE
        for i, byte in enumerate(data):
            self.flash[offset + i] &= byte

    def test_original_instruction_paths(self):
        for flag, size, kind in ((0, 0, None), (1, 256, 3), (16, 4096, 2)):
            result = trace(flag)
            erases = [e for e in result["events"] if e["operation"] == "erase"]
            self.assertEqual(result["result"], size)
            self.assertEqual(erases, [] if kind is None else [dict(operation="erase", kind=kind, address=SECTOR)])
            self.assertTrue(result["nonvolatile_registers_preserved"])

    def test_noop_erase_pattern_and_restore(self):
        self.backend.check_dispatch(self.backup)
        self.assertEqual(self.erases, 0)
        desired = expected_pattern(self.backup)
        for expected in (desired, self.backup):
            self.backend.erase(SECTOR)
            program_image(self.backend, expected[RELATIVE:RELATIVE + SECTOR_SIZE])
            self.backend.verify_full(expected, "verified")
        self.assertEqual(self.erases, 2)
        self.assertEqual(self.flash, self.backup)
        self.assertEqual(self.ram[ERASE_UNIT], b"\0")
        self.assertNotIn(0x24, self.commands)

    def test_noop_mismatch_prevents_erase(self):
        self.flash[0] ^= 1
        with self.assertRaises(ValueError):
            self.backend.check_dispatch(self.backup)
        with self.assertRaises(ValueError):
            self.backend.erase(SECTOR)
        self.assertEqual(self.erases, 0)

    def test_erase_lost_ack_clears_unit_and_does_not_retry(self):
        self.backend.check_dispatch(self.backup)
        self.lose_erase_ack = True
        with self.assertRaises(TimeoutError):
            self.backend.erase(SECTOR)
        self.assertEqual(self.erases, 1)
        self.assertEqual(self.ram[ERASE_UNIT], b"\0")
        self.assertFalse(self.backend.erased_blank)
        self.assertEqual(self.programs, [])

    def test_wrong_erase_unit_and_table_prevent_command(self):
        for address, value in ((ERASE_UNIT, b"\x01"), (SCRATCH, bytes(16))):
            original = self.ram[address]
            self.ram[address] = value
            with self.assertRaises(ValueError):
                self.backend.check_dispatch(self.backup)
            self.ram[address] = original
        self.assertEqual(self.commands, [])

    def test_invalid_writes_rejected_before_io(self):
        for address, data in ((SECTOR - PAGE_SIZE, bytes(PAGE_SIZE)), (SECTOR + 1, bytes(PAGE_SIZE)),
                              (SECTOR, bytes(PAGE_SIZE - 1)), (SECTOR, b"\x97" * PAGE_SIZE)):
            with self.assertRaises(ValueError):
                self.backend.program(address, data)
        with self.assertRaises(ValueError):
            self.backend.erase(SECTOR + SECTOR_SIZE)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.programs, [])

    def test_bad_second_blank_read_stops_before_programming(self):
        self.backend.check_dispatch(self.backup)
        with patch("probe_sector_init.spi_read", side_effect=[b"\xff" * SECTOR_SIZE, b"\xfe" * SECTOR_SIZE]):
            with self.assertRaises(ValueError):
                self.backend.erase(SECTOR)
        self.assertFalse(self.backend.erased_blank)
        self.assertEqual(self.programs, [])
        self.assertEqual(self.ram[ERASE_UNIT], b"\0")


if __name__ == "__main__":
    unittest.main()
