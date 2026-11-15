"""Host-only fault tests of the exact sector-probe sequencing and native guards."""
import unittest
from unittest.mock import Mock, patch

from probe_sector_erase import (SECTOR, SECTOR_SIZE, PAGE_SIZE, RELATIVE, FLASH_BASE,
                               RESERVATION_SIZE, NativeBackend, baseline, run_experiment,
                               restore_sector, erase_verified, SLOT, SCRATCH, TABLE,
                               ChocolateTransportError, main)
from contextlib import redirect_stderr, redirect_stdout
import io
import struct


class Flash:
    def __init__(self, original, fail=None):
        self.data = bytearray(original)
        self.fail = fail
        self.erases = 0
        self.programs = 0
        self.events = []
        self.pages = set()

    def read(self, address, size):
        offset = address - FLASH_BASE
        assert 0 <= offset < offset + size <= RESERVATION_SIZE
        return bytes(self.data[offset:offset + size])

    def erase(self, address):
        assert address == SECTOR
        self.erases += 1
        self.pages.clear()
        self.data[RELATIVE:RELATIVE + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
        if self.fail == "erase_ack" and self.erases == 1:
            raise TimeoutError("erase completed but acknowledgement lost")

    def program(self, address, data):
        assert address % PAGE_SIZE == 0 and SECTOR <= address < SECTOR + SECTOR_SIZE
        assert len(data) == PAGE_SIZE and address not in self.pages
        self.pages.add(address)
        self.programs += 1
        if self.fail == "program_ack" and self.programs == 2:
            raise TimeoutError("page acknowledgement lost")
        offset = address - FLASH_BASE
        for i, byte in enumerate(data):
            self.data[offset + i] &= byte

    def record(self, event, **details):
        self.events.append((event, details))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        _, self.original = baseline()

    def test_suspended_actions_never_access_device(self):
        for argv in (["probe", "--execute"], ["probe", "--recover", "unused-session"]):
            with self.subTest(argv=argv), patch("sys.argv", argv), \
                 patch("probe_sector_erase.list_usb_devices") as ports, \
                 patch("probe_sector_erase.baseline") as backup, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                ports.assert_not_called()
                backup.assert_not_called()

    def test_default_reports_suspension_without_device_access(self):
        with patch("sys.argv", ["probe"]), patch("probe_sector_erase.list_usb_devices") as ports, \
             redirect_stdout(io.StringIO()) as output:
            main()
        self.assertIn('"execution_enabled": false', output.getvalue())
        ports.assert_not_called()

    def test_normal_and_two_erases(self):
        model = Flash(self.original)
        run_experiment(model, self.original, model.record)
        self.assertEqual(model.data, self.original)
        self.assertEqual(model.erases, 2)
        measured = [d for e, d in model.events if e == "pattern_measured"]
        self.assertTrue(measured[0]["complete_image_matches"])

    def test_lost_ack_restores(self):
        for fault in ("erase_ack", "program_ack"):
            with self.subTest(fault=fault):
                model = Flash(self.original, fault)
                with self.assertRaises(TimeoutError):
                    run_experiment(model, self.original, model.record)
                self.assertEqual(model.data, self.original)
                self.assertEqual(model.events[-1][0], "physical_backup_restored")

    def test_baseline_mismatch_does_not_erase(self):
        model = Flash(self.original)
        model.data[0] ^= 1
        with self.assertRaises(ValueError):
            run_experiment(model, self.original, model.record)
        self.assertEqual(model.erases, 0)

    def test_recover_erased_sector(self):
        model = Flash(self.original)
        model.erase(SECTOR)
        restore_sector(model, self.original[RELATIVE:RELATIVE + SECTOR_SIZE], self.original)
        self.assertEqual(model.data, self.original)

    def test_native_guards_before_device_access(self):
        backend = NativeBackend(None, lambda *a, **k: None, self.original)
        for address in (SECTOR - SECTOR_SIZE, SECTOR + SECTOR_SIZE, 0, 0xFFFFFFFF):
            with self.assertRaises(ValueError):
                backend.erase(address)
        for address, data in ((SECTOR - PAGE_SIZE, bytes(PAGE_SIZE)),
                              (SECTOR + SECTOR_SIZE, bytes(PAGE_SIZE)),
                              (SECTOR + 1, bytes(PAGE_SIZE)),
                              (SECTOR, bytes(PAGE_SIZE - 1)),
                              (SECTOR, b"\x97" * PAGE_SIZE)):
            with self.assertRaises(ValueError):
                backend.program(address, data)

    def test_unconfirmed_erase_ack_requires_two_blank_reads(self):
        for blank in (True, False):
            with self.subTest(blank=blank):
                device = Mock()
                device._request.side_effect = ChocolateTransportError("acknowledgement missing")
                backend = NativeBackend(device, lambda *a, **k: None, self.original)
                def ram_value(_device, address, size):
                    self.assertEqual((address, size), (SLOT, 4) if address == SLOT else (SCRATCH, 16))
                    return struct.pack("<I", SCRATCH) if address == SLOT else TABLE
                value = (b"\xff" if blank else b"\0") * SECTOR_SIZE
                with patch("probe_sector_erase.ram_read", side_effect=ram_value), \
                     patch("probe_sector_erase.spi_read", return_value=value) as reads:
                    if blank:
                        erase_verified(backend)
                    else:
                        with self.assertRaises(ValueError):
                            erase_verified(backend)
                    self.assertEqual(reads.call_count, 2)
                    self.assertEqual(device._request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
