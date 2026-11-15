"""Bounded read protocol checks; no actual USB or hardware access."""
import io
import unittest

from read_spi_native import PHYSICAL_FLASH_SIZE, read_full_flash


class ReadOnlyDevice:
    def __init__(self, short_packet=None):
        self.calls = []
        self.short_packet = short_packet

    def _request(self, command, prefix, wanted, repeated_prefix):
        if command != 0x23 or wanted != 0x23 or prefix != repeated_prefix or prefix[0] != 5:
            raise AssertionError("Unexpected operation")
        address = int.from_bytes(prefix[1:5], "little")
        length = int.from_bytes(prefix[5:8], "little")
        if address != len(self.calls) * 512 or length != 512 or address + length > PHYSICAL_FLASH_SIZE:
            raise AssertionError("Unbounded/unexpected read")
        self.calls.append((address, length))
        data = bytes([len(self.calls) & 255]) * (511 if address == self.short_packet else 512)
        return {"payload": prefix + data}


class FullFlashTests(unittest.TestCase):
    def test_exact_complete_extent_only_read_packets(self):
        device, output, progress = ReadOnlyDevice(), io.BytesIO(), []
        data = read_full_flash(device, output, progress.append)
        self.assertEqual(data, output.getvalue())
        self.assertEqual(len(data), 0x40000)
        self.assertEqual(device.calls[-1], (0x3FE00, 512))
        self.assertEqual(len(device.calls), 512)
        self.assertEqual(progress, list(range(32768, 0x40001, 32768)))

    def test_short_response_preserves_only_completed_blocks_and_stops(self):
        device, output = ReadOnlyDevice(short_packet=5120), io.BytesIO()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            read_full_flash(device, output, lambda count: None)
        self.assertEqual(len(output.getvalue()), 5120)
        self.assertEqual(len(device.calls), 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
