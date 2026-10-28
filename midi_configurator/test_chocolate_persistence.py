"""Sector-save integration/fault tests with a NOR model; no hardware is opened."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import chocolate_persistence as p
from .chocolate_transport import ChocolateTransportError, UsbChocolate


class NativeDevice:
    def __init__(self):
        self.app = bytes((i * 13 + 3) & 255 for i in range(p.APP_SIZE))
        self.memory = bytearray(0x20000)
        self.flash = bytearray([255]) * 0x40000
        image = bytearray(p.CONFIG_SIZE)
        image[0], image[23639] = 3, 2
        image[18000:18100] = bytes(range(100))  # Unexposed settings must survive.
        for index in range(32):
            for bank in (161, 289):
                start = 93 + index * 417 + bank
                image[start + 1:start + 128] = bytes([0xA6]) * 127
        self.flash[p.FLASH_BASE:p.FLASH_BASE + p.CONFIG_SIZE] = image
        self.flash[p.FLASH_BASE + p.CONFIG_SIZE:p.FLASH_BASE + 23652] = b"\x12\x34\x56\x78\x9a\xbc"
        self.flash[p.FLASH_BASE + 23652:p.FLASH_BASE + 23660] = b"usrdata\0"
        self.memory[0x1CF60:0x1CF60 + 0x3E4] = self.app[0x25B54:0x25F38]
        self.memory[0x197E8:0x19808] = struct.pack("<8I", 0x40000, 0x854512, 0, 0, 0, 0, 0x1000, 0x1E00000)
        self.requests, self.erases, self.programs, self.ram_writes, self.stock_programs = [], [], [], [], []
        self.failure = None
        self.offline = False
        self.bad_spi = False
        self.bad_program = False
        self.name = "FootCtrlPlus_013"
        self.power_cycle()

    def power_cycle(self):
        self.memory[p.RAM_BASE:p.RAM_BASE + p.CONFIG_SIZE] = self.flash[p.FLASH_BASE:p.FLASH_BASE + p.CONFIG_SIZE]
        self.memory[p.POINTER:p.POINTER + 4] = struct.pack("<I", p.FLASH_BASE)
        self.memory[p.SLOT:p.SLOT + 4] = bytes(4)
        self.memory[p.ERASE_UNIT] = 0

    def query(self):
        if self.offline:
            raise ChocolateTransportError("Disconnected")
        return SimpleNamespace(name=self.name, firmware_version=13)

    def read_config(self, progress_callback=None):
        if self.offline:
            raise ChocolateTransportError("Disconnected")
        return bytes(self.memory[p.RAM_BASE:p.RAM_BASE + p.CONFIG_SIZE])

    def _request(self, command, payload, wanted, prefix=None):
        if self.offline:
            raise ChocolateTransportError("Disconnected")
        self.requests.append((command, payload))
        if command not in (0x21, 0x22, 0x23):
            raise AssertionError("No firmware, bootloader or command-24 operations permitted")
        kind, offset = payload[0], int.from_bytes(payload[1:5], "little")
        count = int.from_bytes(payload[5:8], "little")
        if kind == 4:
            address = p.RAM_BASE + offset
            if command == 0x23:
                if p.APP_BASE <= address < p.APP_BASE + p.APP_SIZE:
                    data = self.app[address - p.APP_BASE:address - p.APP_BASE + count]
                elif p.FLASH_VIEW <= address < p.FLASH_VIEW + p.FLASH_SIZE:
                    start = p.FLASH_BASE + address - p.FLASH_VIEW
                    data = self.flash[start:start + count]
                else:
                    data = self.memory[address:address + count]
                return {"payload": payload + bytes(data)}
            assert command == 0x22
            data = payload[8:]
            assert count == len(data)
            self.ram_writes.append((address, data))
            self.memory[address:address + count] = data
            pointer = int.from_bytes(self.memory[p.POINTER:p.POINTER + 4], "little")
            start = pointer + offset
            if start + count <= len(self.flash):
                self.stock_programs.append(start)
                for i, byte in enumerate(data):
                    self.flash[start + i] &= byte
            return {"payload": b"\0"}
        assert kind == 5
        scratch = int.from_bytes(self.memory[p.SLOT:p.SLOT + 4], "little")
        assert bytes(self.memory[scratch:scratch + 16]) == p.TABLE
        assert p.FLASH_BASE <= offset < p.FLASH_BASE + p.FLASH_SIZE
        if command == 0x23:
            assert offset + count <= p.FLASH_BASE + p.FLASH_SIZE
            data = bytearray(self.flash[offset:offset + count])
            if self.bad_spi:
                data[-1] ^= 1
            return {"payload": payload + bytes(data)}
        if command == 0x21:
            flag = self.memory[p.ERASE_UNIT]
            assert flag in (0, 16)
            if flag:
                assert offset % 4096 == 0
                self.erases.append(offset)
                self.flash[offset:offset + 4096] = bytes([255]) * 4096
                self.maybe_fail("erase")
        elif command == 0x22:
            assert count == 256 and offset % 256 == 0 and self.memory[p.ERASE_UNIT] == 0
            self.programs.append(offset)
            for i, byte in enumerate(payload[8:]):
                self.flash[offset + i] &= byte
            if self.bad_program:
                self.flash[offset + 7] &= 0x7F
            self.maybe_fail("program")
        return {"payload": b"\0"}

    def maybe_fail(self, kind):
        if self.failure == kind:
            self.failure = None
            raise ChocolateTransportError(f"Lost {kind} acknowledgement")
        if self.failure == "disconnect_" + kind:
            self.failure, self.offline = None, True
            raise ChocolateTransportError("Disconnected")


class VerifiedPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="stompilot-persistence-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.device = NativeDevice()
        self.original_ram = self.device.read_config()
        self.original_flash = bytes(self.device.flash)
        self.sha_patch = patch.object(p, "APP_SHA256", p.digest(self.device.app))
        self.sha_patch.start()
        self.addCleanup(self.sha_patch.stop)
        self.writer = p.VerifiedWriter(self.device, self.directory)

    def changed(self):
        after = bytearray(self.original_ram)
        for pos in (450, 4095, 4096, 8600, 12700, 16800, 22000):
            after[pos] ^= 255
        return bytes(after)

    def assert_no_firmware_changes(self):
        self.assertEqual(bytes(self.device.flash[:p.FLASH_BASE]), self.original_flash[:p.FLASH_BASE])
        self.assertEqual(bytes(self.device.flash[p.FLASH_BASE + p.FLASH_SIZE:]), self.original_flash[p.FLASH_BASE + p.FLASH_SIZE:])
        self.assertEqual(self.device.stock_programs, [])
        self.assertFalse(any(command == 0x24 for command, _ in self.device.requests))

    def test_all_sectors_verified_preserved_tail_ram_unchanged_and_restart_required(self):
        after = self.changed()
        self.assertEqual(self.writer.write(self.original_ram, after), after)
        self.assertEqual(self.device.erases, list(range(p.FLASH_BASE, p.FLASH_BASE + p.FLASH_SIZE, 4096)))
        self.assertEqual(len(self.device.programs), len(set(self.device.programs)))
        self.assertTrue(all(address % 256 == 0 for address in self.device.programs))
        self.assertEqual(self.device.flash[p.FLASH_BASE:p.FLASH_BASE + p.CONFIG_SIZE], after)
        self.assertEqual(self.device.flash[p.FLASH_BASE + p.CONFIG_SIZE:p.FLASH_BASE + p.FLASH_SIZE],
                         self.original_flash[p.FLASH_BASE + p.CONFIG_SIZE:p.FLASH_BASE + p.FLASH_SIZE])
        self.assertEqual(self.device.read_config(), self.original_ram)
        journal = p.pending_save(self.directory)
        self.assertEqual(journal.state["status"], "restart_required")
        self.assertEqual(journal.original, self.original_flash[p.FLASH_BASE:p.FLASH_BASE + p.FLASH_SIZE])
        with self.assertRaisesRegex(ChocolateTransportError, "restart"):
            self.writer.verify_restart()
        self.device.power_cycle()
        self.assertEqual(p.VerifiedWriter(self.device, self.directory).verify_restart(), after)
        self.assertIsNone(p.pending_save(self.directory))
        self.assert_no_firmware_changes()

    def test_only_changed_sectors_and_no_change_needs_no_ram_control_writes(self):
        self.assertEqual(self.writer.write(self.original_ram, self.original_ram), self.original_ram)
        self.assertEqual(self.device.ram_writes, [])
        self.assertIsNone(p.pending_save(self.directory))
        after = bytearray(self.original_ram)
        after[8000] ^= 1
        self.writer.write(self.original_ram, after)
        self.assertEqual(self.device.erases, [p.FLASH_BASE + 4096])

    def test_matching_flash_with_stale_ram_still_requires_restart(self):
        after = self.changed()
        self.device.flash[p.FLASH_BASE:p.FLASH_BASE + p.CONFIG_SIZE] = after
        self.writer.write(self.original_ram, after)
        self.assertEqual(self.device.erases, [])
        self.assertEqual(p.pending_save(self.directory).state["status"], "restart_required")
        self.device.power_cycle()
        self.assertEqual(self.writer.verify_restart(), after)

    def test_stale_snapshot_wrong_identity_app_or_driver_refuse_before_mutation(self):
        for fault in ("snapshot", "identity", "app", "driver"):
            with self.subTest(fault=fault):
                device = NativeDevice()
                before = device.read_config()
                if fault == "snapshot":
                    device.memory[p.RAM_BASE + 300] ^= 1
                elif fault == "identity":
                    device.name = "FootCtrlPlus_016"
                elif fault == "app":
                    device.app = b"X" + device.app[1:]
                else:
                    device.memory[0x1CF60] ^= 1
                with self.assertRaises(ChocolateTransportError):
                    p.VerifiedWriter(device, self.directory).write(before, self.changed())
                self.assertEqual(device.ram_writes, [])
                self.assertEqual(device.erases, [])

    def test_backup_flush_failure_prevents_even_ram_control_writes(self):
        with patch.object(p.os, "fsync", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                self.writer.write(self.original_ram, self.changed())
        self.assertEqual(self.device.ram_writes, [])
        self.assertEqual(self.device.erases, [])

    def test_raw_spi_disagreement_stops_before_any_erase(self):
        self.device.bad_spi = True
        with self.assertRaisesRegex(ChocolateTransportError, "Raw SPI differs"):
            self.writer.write(self.original_ram, self.changed())
        self.assertEqual(self.device.erases, [])
        self.assertEqual(bytes(self.device.flash), self.original_flash)
        self.assertEqual(p.pending_save(self.directory).state["status"], "recovery_required")

    def test_lost_ack_is_not_retried_and_backup_can_be_restored_after_reopening(self):
        for fault in ("erase", "program", "disconnect_erase", "disconnect_program"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="stompilot-save-fault-") as directory:
                device = NativeDevice()
                device.failure = fault
                writer = p.VerifiedWriter(device, directory)
                original = bytes(device.flash)
                with self.assertRaisesRegex(ChocolateTransportError, "Restore backup"):
                    writer.write(device.read_config(), self.changed())
                self.assertEqual(len(device.erases), 1)  # No retry after an uncertain ACK.
                self.assertLessEqual(len(device.programs), 1)
                device.offline = False
                restored = p.VerifiedWriter(device, directory).restore()
                self.assertEqual(restored, self.original_ram)
                self.assertEqual(bytes(device.flash), original)
                device.power_cycle()
                p.VerifiedWriter(device, directory).verify_restart()
                self.assertIsNone(p.pending_save(directory))

    def test_power_loss_after_partial_write_restores_complete_original_from_disk(self):
        self.device.failure = "disconnect_program"
        with self.assertRaises(ChocolateTransportError):
            self.writer.write(self.original_ram, self.changed())
        self.device.offline = False
        self.device.power_cycle()
        p.VerifiedWriter(self.device, self.directory).restore()
        self.assertEqual(bytes(self.device.flash), self.original_flash)
        self.device.power_cycle()
        p.VerifiedWriter(self.device, self.directory).verify_restart()
        self.assert_no_firmware_changes()

    def test_corrupt_program_detected_and_no_success_until_readback_matches(self):
        self.device.bad_program = True
        after = bytearray(self.original_ram)
        after[263] = 255
        with self.assertRaisesRegex(ChocolateTransportError, "readback failed"):
            self.writer.write(self.original_ram, after)
        self.assertEqual(p.pending_save(self.directory).state["status"], "recovery_required")
        self.device.bad_program = False
        self.writer.restore()
        self.assertEqual(bytes(self.device.flash), self.original_flash)

    def test_restart_mismatch_keeps_recovery_available_and_blocks_next_write(self):
        self.writer.write(self.original_ram, self.changed())
        self.device.power_cycle()
        self.device.memory[p.RAM_BASE + 500] ^= 1
        with self.assertRaisesRegex(ChocolateTransportError, "after restart differs"):
            self.writer.verify_restart()
        with self.assertRaisesRegex(ChocolateTransportError, "previous save"):
            self.writer.write(self.original_ram, self.changed())
        self.assertIsNotNone(p.pending_save(self.directory))

    def test_damaged_backup_refuses_restoration_without_mutating(self):
        self.writer.write(self.original_ram, self.changed())
        journal = p.pending_save(self.directory)
        (journal.directory / "original.bin").write_bytes(bytes(p.FLASH_SIZE))
        count = len(self.device.ram_writes)
        with self.assertRaisesRegex(ChocolateTransportError, "backup is damaged"):
            self.writer.restore()
        self.assertEqual(len(self.device.ram_writes), count)

    def test_no_padding_refuses_before_mutation_and_selection_never_touches_payload(self):
        full = bytearray(self.original_ram)
        for index in range(32):
            for bank in (161, 289):
                full[93 + index * 417 + bank] = 127
        with self.assertRaisesRegex(ChocolateTransportError, "unused bytes"):
            p.choose_scratch(full)
        full[93 + 161] = 90
        scratch = p.choose_scratch(full)
        self.assertGreaterEqual(scratch, 93 + 161 + 91)
        self.assertLessEqual(scratch + 16, 93 + 161 + 128)
        self.assertEqual((p.RAM_BASE + scratch) % 4, 0)

    def test_firmware13_public_transport_routes_to_verified_writer_without_legacy_fallback(self):
        usb = UsbChocolate("FootCtrlPlus 0", "FootCtrlPlus 1")
        with patch.object(usb, "query", return_value=SimpleNamespace(name="FootCtrlPlus_013")), \
             patch.object(usb, "_persistence", return_value=self.writer), \
             patch.object(usb, "_write", side_effect=AssertionError("Legacy V13 write forbidden")):
            usb.write_config(self.original_ram, self.changed(), backup_directory=self.directory)
            with self.assertRaisesRegex(ChocolateTransportError, "previous save"):
                usb.write_config(self.original_ram, self.changed(), backup_directory=self.directory)


class VerifiedSaveServiceTests(unittest.TestCase):
    def setUp(self):
        from .desktop_service import DesktopService
        self.Service = DesktopService
        self.temp = TemporaryDirectory(prefix="stompilot-save-ui-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.device = NativeDevice()
        self.original_flash = bytes(self.device.flash)
        self.sha_patch = patch.object(p, "APP_SHA256", p.digest(self.device.app))
        self.sha_patch.start()
        self.addCleanup(self.sha_patch.stop)
        self.transports = []
        self.present = True
        self.usb_generation = 0
        owner = self

        class Transport:
            def __init__(self, *_):
                self.opened = False
                self.open_count = 0
                self.generation = None
                owner.transports.append(self)
            def open(self):
                owner.assertTrue(owner.present)
                self.opened = True
                self.open_count += 1
                self.generation = owner.usb_generation
                return self
            def close(self):
                if self.opened:
                    # Model the unsafe native close that caused the Windows
                    # heap crash. Idempotent closes of released handles are safe.
                    owner.assertEqual(self.generation, owner.usb_generation,
                                      "Native MIDI handle survived USB removal")
                    self.opened = False
            def require_open(self):
                owner.assertTrue(self.opened, "Each I/O operation must reopen the selected controller")
                owner.assertEqual(self.generation, owner.usb_generation)
            def query(self):
                self.require_open()
                return owner.device.query()
            def read_config(self, progress_callback=None):
                self.require_open()
                return owner.device.read_config(progress_callback)
            def write_config(self, before, after, progress_callback=None, *, backup_directory=None):
                self.require_open()
                return p.VerifiedWriter(owner.device, backup_directory, progress_callback).write(before, after)
            def verify_config_restart(self, directory, progress_callback=None):
                self.require_open()
                return p.VerifiedWriter(owner.device, directory, progress_callback).verify_restart()
            def restore_config_backup(self, directory, progress_callback=None):
                self.require_open()
                return p.VerifiedWriter(owner.device, directory, progress_callback).restore()

        self.factory = Transport
        self.port = SimpleNamespace(name="FootCtrlPlus", input_name="FootCtrlPlus 0", output_name="FootCtrlPlus 1")
        self.service = self.new_service()

    def new_service(self):
        return self.Service(data_directory=self.directory, transport_factory=self.factory,
                            discovery=lambda: [self.port] if self.present else [])

    def request(self, name, **payload):
        reply = self.service.request(name, payload)
        self.assertTrue(reply["ok"], reply)
        return reply["data"]

    def prepare(self):
        self.request("new_project")
        self.request("controller_scan")
        self.request("controller_read", device_name=self.port.name)
        result = self.request("controller_preview")
        self.assertTrue(any("power-off" in text for text in result["controller"]["preview"]["warnings"]))
        self.assertGreater(result["controller"]["preview"]["changed_bytes"], 0)

    def test_write_and_reopened_application_require_physical_restart_then_verify(self):
        self.prepare()
        result = self.request("controller_write")
        self.assertFalse(result["controller"]["can_write"])
        self.assertEqual(result["controller"]["pending_save"]["status"], "restart_required")
        self.assertIn("10 seconds", result["controller"]["status"])
        self.service.close()
        self.service = self.new_service()
        self.assertIsNotNone(self.service.snapshot()["controller"]["pending_save"])
        self.assertFalse(self.service.request("controller_verify")["ok"])
        self.assertIsNotNone(self.service.snapshot()["controller"]["pending_save"])
        self.device.power_cycle()
        result = self.request("controller_verify")
        self.assertIsNone(result["controller"]["pending_save"])
        self.assertIn("Normal saving is active", result["controller"]["status"])
        self.assertEqual(self.service.image, self.device.read_config())

    def test_interrupted_service_write_restores_backup_without_losing_project(self):
        self.prepare()
        project = self.service.snapshot()["project"]
        self.device.failure = "disconnect_erase"
        result = self.service.request("controller_write")
        self.assertFalse(result["ok"])
        self.assertEqual(self.service.snapshot()["controller"]["pending_save"]["status"], "recovery_required")
        self.device.offline = False
        result = self.request("controller_restore")
        self.assertEqual(bytes(self.device.flash), self.original_flash)
        self.assertEqual(result["project"], project)
        self.assertTrue(result["controller"]["pending_save"]["restored"])
        self.assertFalse(result["controller"]["connected"])
        self.assertFalse(any(transport.opened for transport in self.transports))
        self.usb_generation += 1
        self.device.power_cycle()
        result = self.request("controller_verify")
        self.assertIn("Original configuration restored", result["controller"]["status"])
        self.assertIsNone(result["controller"]["pending_save"])

    def test_save_unplug_and_verify_in_same_app_release_handles_and_keep_project(self):
        self.prepare()
        project = self.service.snapshot()["project"]
        original_transport = self.transports[-1]
        self.assertFalse(original_transport.opened)
        result = self.request("controller_write")
        self.assertGreaterEqual(original_transport.open_count, 2)
        self.assertFalse(result["controller"]["connected"])
        self.assertFalse(any(transport.opened for transport in self.transports))
        self.assertEqual(result["controller"]["pending_save"]["status"], "restart_required")
        erases, programs = list(self.device.erases), list(self.device.programs)

        self.present = False
        self.usb_generation += 1
        reply = self.service.request("controller_verify")
        self.assertFalse(reply["ok"])
        self.assertIn("Connect the Chocolate Plus", reply["error"]["message"])
        self.assertEqual(self.service.snapshot()["project"], project)
        self.assertIsNotNone(self.service.snapshot()["controller"]["pending_save"])

        self.device.power_cycle()
        self.present = True
        result = self.request("controller_verify")
        self.assertIsNot(self.transports[-1], original_transport)
        self.assertFalse(any(transport.opened for transport in self.transports))
        self.assertIsNone(result["controller"]["pending_save"])
        self.assertEqual(result["project"], project)
        self.assertEqual(self.device.erases, erases)
        self.assertEqual(self.device.programs, programs)

    def test_read_reopens_selection_and_disconnect_is_safe_after_usb_removal(self):
        self.prepare()
        transport = self.transports[-1]
        self.assertFalse(transport.opened)
        self.request("controller_read", device_name=self.port.name)
        self.assertEqual(transport.open_count, 2)
        self.assertFalse(transport.opened)
        self.present = False
        self.usb_generation += 1
        result = self.request("controller_disconnect")
        self.assertFalse(result["controller"]["connected"])
        self.assertIsNotNone(result["project"])


if __name__ == "__main__":
    unittest.main()
