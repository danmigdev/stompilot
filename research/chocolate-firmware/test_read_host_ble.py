"""Offline guards for the read-only hardware diagnostic; no Bluetooth access."""
import unittest

from read_host_ble import Frames, RAM_BASE, read_prefix, request_frame
from midi_configurator.chocolate_transport import usb_packet
from midi_configurator.core import pack7


def reply(command,payload):
    return b'\0Y'+bytes([command])+len(payload).to_bytes(3,'little')+payload+bytes([~sum(payload)&255])


class ReadOnlyBleTests(unittest.TestCase):
    def test_all_write_reset_update_opcodes_rejected(self):
        for command in range(256):
            if command not in (0x11,0x23):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    request_frame(command)

    def test_only_exact_read_request_layout_and_bounded_ranges(self):
        prefix=read_prefix(0x13280,64)
        self.assertEqual((int.from_bytes(prefix[1:5],'little')+RAM_BASE)&0xffffffff,0x13280)
        for address,size in ((0x1327f,1),(0x132ff,2),(0x1e1800,4),(0x13280,129),(0x13280,0)):
            with self.subTest(address=address,size=size), self.assertRaises(ValueError):
                read_prefix(address,size)
        for bad in (b'',prefix+b'\0',b'\x05'+prefix[1:]):
            with self.assertRaises(ValueError):
                request_frame(0x23,bad)
        with self.assertRaises(ValueError):
            request_frame(0x11,b'\0')

    def test_ble_frames_match_existing_usb_protocol_before_encoding(self):
        for command,body in ((0x11,b''),(0x23,read_prefix(0x13280,64))):
            frame=request_frame(command,body)
            encoded=pack7(frame)[:(len(frame)*8+6)//7]
            self.assertEqual(b'\xf0'+encoded+b'\xf7',usb_packet(command,body))

    def test_fragmented_and_coalesced_notifications(self):
        first=reply(0x11,b'FootCtrlPlus_013\0')
        second=reply(0x23,read_prefix(0x13280,64)+bytes(range(64)))
        for split in range(len(first+second)+1):
            parser=Frames()
            frames=parser.feed((first+second)[:split])+parser.feed((first+second)[split:])
            self.assertEqual(frames,[(0x11,first[6:-1]),(0x23,second[6:-1])])

    def test_bad_checksum_noise_and_oversized_reply_do_not_pass(self):
        good=reply(0x11,b'FootCtrlPlus_013\0')
        bad=good[:-1]+bytes([good[-1]^1])
        parser=Frames()
        self.assertEqual(parser.feed(b'noise\0Y\x11\xff\xff\xff'+bad+good),[(0x11,good[6:-1])])


if __name__=='__main__':
    unittest.main()
