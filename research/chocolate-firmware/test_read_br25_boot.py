"""The elevated diagnostic must reject writes to flash and other devices/ranges."""
import binascii
import ctypes
import os
import unittest

from read_br25_boot import command, gate, LOAD_BASE, LOAD_SIZE, FLASH_SIZE, Packet, SPTD, verify_online_device


class CommandGateTests(unittest.TestCase):
    def setUp(self):
        self.loader = bytes(range(256)) * (LOAD_SIZE // 256)

    def test_only_exact_inquiry(self):
        gate(bytes.fromhex("12 00 00 00 24 00"),None,36,"rom",self.loader)
        for data in (bytes.fromhex("12 01 00 00 24 00"),b"\x12",bytes(6)):
            with self.assertRaises(ValueError): gate(data,None,36,"rom",self.loader)

    def test_erase_program_key_and_reset_forbidden_in_every_phase(self):
        for phase in ("rom","loader"):
            for code in (0xFB00,0xFB01,0xFB02,0xFB04,0xFC12,0xFC0C,0xFC0D):
                with self.subTest(phase=phase,code=code),self.assertRaises(ValueError):
                    gate(command(code),None,16,phase,self.loader)

    def test_ram_upload_and_readback_are_limited_to_pinned_blocks(self):
        for offset in range(0,LOAD_SIZE,512):
            data=self.loader[offset:offset+512]
            fields=(LOAD_BASE+offset).to_bytes(4,"big")+b"\x02\x00"
            cdb=command(0xFB06,fields+b"\0"+binascii.crc_hqx(data,0).to_bytes(2,"little"))
            gate(cdb,data,0,"rom",self.loader)
            gate(command(0xFD07,fields),None,512,"rom",self.loader)
            with self.assertRaises(ValueError): gate(cdb,bytes(512),0,"rom",self.loader)
            with self.assertRaises(ValueError): gate(cdb,data,0,"loader",self.loader)

    def test_no_arbitrary_memory_or_entry(self):
        for addr in (0,0x10000,LOAD_BASE-512,LOAD_BASE+1,LOAD_BASE+LOAD_SIZE,0x1E0000):
            fields=addr.to_bytes(4,"big")+b"\x02\x00"
            with self.assertRaises(ValueError): gate(command(0xFD07,fields),None,512,"rom",self.loader)
        gate(command(0xFB08,LOAD_BASE.to_bytes(4,"big")+b"\0\1"),None,16,"rom",self.loader)
        for addr,arg in ((LOAD_BASE+4,1),(LOAD_BASE,7),(LOAD_BASE,0)):
            with self.assertRaises(ValueError):
                gate(command(0xFB08,addr.to_bytes(4,"big")+arg.to_bytes(2,"big")),None,16,"rom",self.loader)

    def test_flash_reads_exactly_256_bytes_in_range(self):
        for addr in range(0,FLASH_SIZE,256):
            gate(command(0xFD05,addr.to_bytes(4,"big")+b"\x01\x00"),None,256,"loader",self.loader)
        for addr,size in ((FLASH_SIZE,256),(1,256),(0,512),(0,0),(FLASH_SIZE-128,256)):
            with self.assertRaises(ValueError):
                gate(command(0xFD05,addr.to_bytes(4,"big")+size.to_bytes(2,"big")),None,size,"loader",self.loader)

    def test_read_commands_cannot_carry_outgoing_data(self):
        for code,args,size in ((0xFC0A,b"",16),(0xFC0B,b"",16),(0xFC14,b"",16),
                              (0xFD05,bytes(4)+b"\x01\x00",256)):
            with self.assertRaises(ValueError): gate(command(code,args),b"x",size,"loader",self.loader)

    @unittest.skipUnless(os.name == "nt", "Windows ABI layout")
    def test_windows_struct_layout(self):
        self.assertEqual(ctypes.sizeof(SPTD),56)
        self.assertEqual(Packet.sense.offset,56)
        self.assertEqual(SPTD.DataBuffer.offset,24)

    def test_online_id_matches_stock_driver_and_density(self):
        reply = bytes.fromhex("030012458500") + bytes(8)
        self.assertEqual(verify_online_device(reply), 0x854512)
        for offset in (0, 2, 3, 4, 5):
            invalid = bytearray(reply)
            invalid[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                verify_online_device(invalid)
        with self.assertRaises(ValueError):
            verify_online_device(reply[:-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
