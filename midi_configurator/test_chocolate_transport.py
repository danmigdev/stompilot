"""Native USB protocol checks using a memory-backed controller, no hardware."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .chocolate_transport import (
    CONFIG_SIZE, READ_CHUNK, ChocolateTransportError, UsbChocolate,
    changed_ranges, list_usb_devices, usb_packet,
)
from .core import parse_packet


class FakeMessage:
    def __init__(self, wire):
        self.wire = bytes(wire)
        self.type = "sysex" if self.wire[0] == 0xF0 else "control_change"

    @classmethod
    def from_bytes(cls, wire):
        return cls(wire)

    def bytes(self):
        return list(self.wire)


class FakeInput:
    def __init__(self):
        self.pending = []
        self.closed = False

    def iter_pending(self):
        messages, self.pending = self.pending, []
        return iter(messages)

    def close(self):
        self.closed = True


class FakeController:
    def __init__(self):
        self.image = bytearray(CONFIG_SIZE)
        self.image[18546:] = bytes([0xAB]) * (CONFIG_SIZE - 18546)
        self.input = FakeInput()
        self.closed = False
        self.requests = []
        self.reject_write = False
        self.lose_ack = False
        self.mute = False
        self.short_read = False
        self.mismatch_first = False
        self.corrupt_readback = False
        self.name = b"FootCtrlPlus_016\0"

    def send(self, message):
        request = parse_packet(bytes(message.bytes()))
        command, payload = request["opcode"], request["payload"]
        self.requests.append((command, payload))
        if self.mute:
            return
        if command == 0x11:
            self.reply(0x11, self.name + bytes(27 - len(self.name)))
        elif command == 0x23:
            address = int.from_bytes(payload[1:5], "little")
            count = int.from_bytes(payload[5:8], "little")
            if self.mismatch_first:
                self.reply(0x23, b"\x00" + payload[1:] + bytes(count))
            data = bytes(self.image[address:address + count])
            self.reply(0x23, payload + (data[:-1] if self.short_read else data))
        elif command == 0x22:
            if self.reject_write:
                self.reply(0, b"\x01")
            else:
                address = int.from_bytes(payload[1:5], "little")
                self.image[address:address + len(payload[8:])] = payload[8:]
                if self.corrupt_readback:
                    self.image[-1] ^= 1
                if not self.lose_ack:
                    self.reply(0, b"\x00")

    def reply(self, command, payload):
        self.input.pending.append(FakeMessage(usb_packet(command, payload)))

    def close(self):
        self.closed = True

    def module(self):
        return SimpleNamespace(
            Message=FakeMessage,
            get_input_names=lambda: ["FootCtrlPlus 0", "Unrelated Synth 1"],
            get_output_names=lambda: ["Microsoft GS Wavetable Synth 0", "FootCtrlPlus 1"],
            open_input=lambda name: self.input,
            open_output=lambda name: self,
        )


class UsbTransportTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.midi = self.controller.module()
        self.patch = patch("midi_configurator.chocolate_transport._mido", return_value=self.midi)
        self.patch.start()
        self.usb = UsbChocolate("FootCtrlPlus 0", "FootCtrlPlus 1", timeout=0.025).open()

    def tearDown(self):
        self.usb.close()
        self.patch.stop()

    def test_query_has_exact_native_length_and_identity(self):
        # Seven raw bytes use exactly eight 7-bit bytes, without a ninth zero.
        self.assertEqual(len(usb_packet(0x11)), 10)
        identity = self.usb.query()
        self.assertEqual(identity.name, "FootCtrlPlus_016")
        self.assertEqual(identity.firmware_version, 16)
        self.assertEqual(len(identity.raw_payload), 27)

    def test_read_collects_bounded_chunks_and_matches_response_prefix(self):
        self.controller.mismatch_first = True
        progress = []
        actual = self.usb.read_config(lambda done, total: progress.append((done, total)))
        self.assertEqual(actual, bytes(self.controller.image))
        reads = [payload for command, payload in self.controller.requests if command == 0x23]
        self.assertEqual(len(reads), (CONFIG_SIZE + READ_CHUNK - 1) // READ_CHUNK)
        self.assertTrue(all(int.from_bytes(p[5:8], "little") <= READ_CHUNK for p in reads))
        self.assertEqual(progress[-1], (CONFIG_SIZE, CONFIG_SIZE))

    def test_write_sends_only_changed_spans_and_verifies_unknown_tail(self):
        before = bytes(self.controller.image)
        after = bytearray(before)
        after[0] = 8
        after[300:304] = b"\xF0\x01\x02\xF7"
        progress = []
        verified = self.usb.write_config(before, after, lambda done, total: progress.append((done, total)))
        self.assertEqual(verified, bytes(after))
        writes = [payload for command, payload in self.controller.requests if command == 0x22]
        self.assertEqual([(int.from_bytes(p[1:5], "little"), p[8:]) for p in writes], [(300, bytes(after[300:304])), (0, b"\x08")])
        self.assertEqual(verified[18546:], before[18546:])
        self.assertEqual(progress[-1], (5, 5))

    def test_stale_backup_aborts_without_writing(self):
        before = bytes(self.controller.image)
        self.controller.image[4] = 1
        after = bytearray(before)
        after[0] = 8
        with self.assertRaisesRegex(ChocolateTransportError, "changed since"):
            self.usb.write_config(before, after)
        self.assertFalse(any(command == 0x22 for command, _ in self.controller.requests))

    def test_wrong_ack_aborts_and_does_not_claim_success(self):
        self.controller.reject_write = True
        before = bytes(self.controller.image)
        after = bytearray(before)
        after[0] = 8
        with self.assertRaisesRegex(ChocolateTransportError, "refused.*status 1"):
            self.usb.write_config(before, after)
        self.assertEqual(self.controller.image, before)

    def test_lost_ack_is_not_retried(self):
        self.controller.lose_ack = True
        before = bytes(self.controller.image)
        after = bytearray(before)
        after[0] = 8
        with self.assertRaisesRegex(ChocolateTransportError, "did not answer"):
            self.usb.write_config(before, after)
        self.assertEqual(sum(command == 0x22 for command, _ in self.controller.requests), 1)

    def test_full_readback_detects_other_settings_changed_by_write(self):
        self.controller.corrupt_readback = True
        before = bytes(self.controller.image)
        after = bytearray(before)
        after[0] = 8
        with self.assertRaisesRegex(ChocolateTransportError, "did not retain.*byte 23645"):
            self.usb.write_config(before, after)

    def test_timeout_and_incomplete_reply_are_clear(self):
        self.controller.mute = True
        with self.assertRaisesRegex(ChocolateTransportError, "did not answer command 0x11"):
            self.usb.query()
        self.controller.mute = False
        self.controller.short_read = True
        with self.assertRaisesRegex(ChocolateTransportError, "Incomplete controller configuration"):
            self.usb.read_config()

    def test_identity_guard_prevents_writes_to_another_device(self):
        self.controller.name = b"TANK MINI_009\0"
        before = bytes(self.controller.image)
        with self.assertRaisesRegex(ChocolateTransportError, "expected FootCtrlPlus"):
            self.usb.write_config(before, before)
        self.assertFalse(any(command == 0x22 for command, _ in self.controller.requests))

    def test_discovery_excludes_synth_and_closes_ports(self):
        devices = list_usb_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].input_name, "FootCtrlPlus 0")
        self.usb.close()
        self.assertTrue(self.controller.input.closed)
        self.assertTrue(self.controller.closed)

    def test_output_open_failure_closes_input(self):
        self.usb.close()
        self.controller.input.closed = False
        def fail(name):
            raise OSError("busy")
        self.midi.open_output = fail
        with self.assertRaisesRegex(ChocolateTransportError, "Cannot open"):
            self.usb.open()
        self.assertTrue(self.controller.input.closed)

    def test_patch_size_and_port_pair_are_bounded(self):
        with self.assertRaisesRegex(ChocolateTransportError, "23646-byte"):
            changed_ranges(bytes(10), bytes(10))
        with self.assertRaisesRegex(ChocolateTransportError, "FootCtrlPlus"):
            UsbChocolate("Unrelated synth", "Unrelated synth")
        with self.assertRaisesRegex(ChocolateTransportError, "same controller"):
            UsbChocolate("FootCtrlPlus one", "FootCtrlPlus two")


if __name__ == "__main__":
    unittest.main()
