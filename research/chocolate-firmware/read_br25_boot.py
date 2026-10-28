"""Windows BR25 bootloader diagnostic: inquiry and two fixed flash reads.

Uses a SHA-pinned vendor RAM loader distributed by kagaimiq/jl-uboot-tool
at commit adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a. Only that loader can be
uploaded to RAM. The command gate has no flash erase/program, key, reset or
arbitrary-execution operation. Windows SCSI access requires administrator rights.
Default is offline; --execute is the bounded hardware diagnostic.
"""
from __future__ import annotations

import argparse
import binascii
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
LOADER = HERE / "artifacts/jl-uboot-tool/data/loaderblobs/usb/br25loader.bin"
LOADER_SHA = "0bc69023d6bf4c4d5d5bc048e06249ee39c484acd11196ffc73f88940787d77d"
LOAD_BASE, LOAD_SIZE, FLASH_SIZE = 0x12000, 11264, 0x40000
# Read from the stock firmware's audited SPI driver before entering UBOOT.
EXPECTED_JEDEC = 0x854512


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def command(code, args=b""):
    require(len(args) <= 14, "Command too long")
    return code.to_bytes(2,"big") + args + b"\xff" * (14-len(args))


def verify_online_device(reply):
    """V2 GET_ONLINE_DEVICE carries type at 0 and little-endian JEDEC at 2:6.

    The BR25 READ_ID response does not have the generic tool's assumed
    three-byte JEDEC layout. Never infer capacity from that separate reply.
    Require the complete ID captured by the stock application's SPI driver.
    """
    require(len(reply) == 14 and reply[0] == 3, "RAM loader did not select SPI NOR")
    identity = int.from_bytes(reply[2:6], "little")
    require(identity == EXPECTED_JEDEC, "Loader flash ID differs from the stock driver capture")
    require(1 << (identity & 255) == FLASH_SIZE, "Flash density differs from 256 KiB")
    return identity


def gate(cdb, outgoing, incoming, phase, loader):
    """Reject every command outside this diagnostic's exact fixed operations."""
    if cdb == bytes.fromhex("12 00 00 00 24 00"):
        require(outgoing is None and incoming == 36, "Invalid inquiry transfer")
        return
    require(len(cdb) == 16, "Invalid vendor command length")
    code = int.from_bytes(cdb[:2],"big")
    address, count = int.from_bytes(cdb[2:6],"big"), int.from_bytes(cdb[6:8],"big")
    if phase == "rom" and code in (0xFB06,0xFD07):
        require(LOAD_BASE <= address and address + count <= LOAD_BASE + LOAD_SIZE and
                count == 512 and (address-LOAD_BASE) % 512 == 0, "Outside fixed loader RAM blocks")
        if code == 0xFB06:
            expected = loader[address-LOAD_BASE:address-LOAD_BASE+count]
            args = address.to_bytes(4,"big") + count.to_bytes(2,"big") + b"\0" + binascii.crc_hqx(expected,0).to_bytes(2,"little")
            require(cdb == command(code,args) and outgoing == expected and incoming == 0,
                    "RAM upload differs from pinned loader")
        else:
            require(cdb == command(code,cdb[2:8]) and outgoing is None and incoming == count,
                    "Invalid loader readback")
    elif phase == "rom" and code == 0xFB08:
        require(cdb == command(code,LOAD_BASE.to_bytes(4,"big") + b"\0\1") and
                outgoing is None and incoming == 16, "Only pinned loader entry with SPI NOR argument is allowed")
    elif phase == "loader" and code in (0xFC0A,0xFC0B,0xFC14):
        require(cdb == command(code) and outgoing is None and incoming == 16, "Invalid loader query")
    elif phase == "loader" and code == 0xFD05:
        require(0 <= address < address + count <= FLASH_SIZE and count == 256 and address % 256 == 0,
                "Outside fixed physical flash read extent")
        require(cdb == command(code,cdb[2:8]) and outgoing is None and incoming == count,
                "Invalid flash read")
    else:
        raise ValueError(f"Command {code:04x} is not allowed by the read-only diagnostic")


class SPTD(ctypes.Structure):
    _fields_ = [("Length",wintypes.WORD),("ScsiStatus",ctypes.c_ubyte),
                ("PathId",ctypes.c_ubyte),("TargetId",ctypes.c_ubyte),("Lun",ctypes.c_ubyte),
                ("CdbLength",ctypes.c_ubyte),("SenseInfoLength",ctypes.c_ubyte),("DataIn",ctypes.c_ubyte),
                ("DataTransferLength",wintypes.DWORD),("TimeOutValue",wintypes.DWORD),
                ("DataBuffer",ctypes.c_void_p),("SenseInfoOffset",wintypes.DWORD),("Cdb",ctypes.c_ubyte*16)]


class Packet(ctypes.Structure):
    _fields_ = [("sptd",SPTD),("sense",ctypes.c_ubyte*32)]


class Device:
    def __init__(self, path, loader):
        require(os.name == "nt" and re.fullmatch(r"\\\\\.\\PHYSICALDRIVE\d+",path,re.I), "Expected Windows physical device path")
        self.kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                           wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.DeviceIoControl.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,
                                               ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
        self.kernel.DeviceIoControl.restype = wintypes.BOOL
        self.handle = self.kernel.CreateFileW(path,0xC0000000,3,None,3,0,None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.loader, self.phase, self.transactions = loader,"rom",0

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def authorize(self, cdb, outgoing, incoming):
        gate(cdb,outgoing,incoming,self.phase,self.loader)

    def execute(self,cdb,outgoing=None,incoming=0):
        self.authorize(cdb,outgoing,incoming)
        packet = Packet()
        s = packet.sptd
        s.Length, s.CdbLength, s.TimeOutValue = ctypes.sizeof(SPTD),len(cdb),5
        s.SenseInfoLength, s.SenseInfoOffset = 32,Packet.sense.offset
        s.TargetId = 1
        s.DataIn = 0 if outgoing is not None else 1
        length = len(outgoing) if outgoing is not None else incoming
        buffer = ctypes.create_string_buffer(length)
        if outgoing is not None:
            ctypes.memmove(buffer,outgoing,length)
        s.DataBuffer,s.DataTransferLength = ctypes.addressof(buffer),length
        for i,v in enumerate(cdb): s.Cdb[i] = v
        returned = wintypes.DWORD()
        ok = self.kernel.DeviceIoControl(self.handle,0x4D014,ctypes.byref(packet),ctypes.sizeof(packet),
                                        ctypes.byref(packet),ctypes.sizeof(packet),ctypes.byref(returned),None)
        self.transactions += 1
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        require(s.ScsiStatus == 0 and s.DataTransferLength == length,
                f"SCSI status/length mismatch: {s.ScsiStatus}, {s.DataTransferLength}/{length}; sense {bytes(packet.sense).hex()}")
        return buffer.raw[:incoming]

    def reply(self,code,args=b""):
        response = self.execute(command(code,args),incoming=16)
        require(response[:2] == code.to_bytes(2,"big"), "Loader reply command mismatch")
        return response[2:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--resume-loader",action="store_true",
                        help="Query/read the already verified RAM loader without uploading it again")
    args = parser.parse_args()
    loader = LOADER.read_bytes()
    require(len(loader) == LOAD_SIZE and sha(loader) == LOADER_SHA, "Vendor RAM loader fingerprint mismatch")
    if not args.execute:
        print(json.dumps(dict(hardware_access=False,loader_bytes=LOAD_SIZE,flash_read_bytes=FLASH_SIZE,
                              flash_reads=2,flash_write_commands_allowed=False)))
        return
    root = HERE / "artifacts/rom-flash-reads" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    root.mkdir(parents=True)
    report = dict(status="starting",flash_writes=0,flash_erases=0,firmware_installed=False,
                  loader_sha256=LOADER_SHA,loader_ram_upload_started=False,
                  resume_loader=args.resume_loader)

    def save():
        with (root / "report.json").open("w",encoding="utf-8") as f:
            json.dump(report,f,indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    save()
    dev = None
    try:
        command_text = "@(Get-CimInstance Win32_DiskDrive | Where-Object { $_.Model -eq 'BR25 UBOOT1.00 USB Device' } | Select-Object -ExpandProperty DeviceID) | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command_text],
                                check=True,capture_output=True,text=True,creationflags=0x08000000,timeout=20)
        paths = json.loads(result.stdout or "[]")
        if isinstance(paths,str): paths = [paths]
        require(len(paths) == 1,"Expected exactly one BR25 UBOOT1.00 device")
        dev = Device(paths[0],loader)
        inquiry = dev.execute(bytes.fromhex("12 00 00 00 24 00"),incoming=36)
        require(inquiry[8:16].decode("ascii").strip() == "BR25" and
                inquiry[16:32].decode("ascii").strip() == "UBOOT1.00", "SCSI identity differs from BR25 ROM")
        report.update(status="rom_verified",identity="BR25 UBOOT1.00")
        save()
        if not args.resume_loader:
            report.update(status="uploading_pinned_ram_loader",loader_ram_upload_started=True)
            save()
            for offset in range(0,LOAD_SIZE,512):
                data = loader[offset:offset+512]
                fields = (LOAD_BASE+offset).to_bytes(4,"big")+len(data).to_bytes(2,"big")
                dev.execute(command(0xFB06,fields+b"\0"+binascii.crc_hqx(data,0).to_bytes(2,"little")),outgoing=data)
                require(dev.execute(command(0xFD07,fields),incoming=len(data)) == data,
                        "RAM loader readback mismatch; entry not executed")
            report["status"] = "ram_loader_verified"
            save()
            dev.reply(0xFB08,LOAD_BASE.to_bytes(4,"big")+b"\0\1")
        dev.phase = "loader"
        online = dev.reply(0xFC0A)
        report["online_reply"] = online.hex()
        save()
        jedec = verify_online_device(online)
        identity = dev.reply(0xFC0B)
        report.update(id_reply=identity.hex(),jedec_id=f"{jedec:06x}",
                      jedec_source="GET_ONLINE_DEVICE; exact match to stock SPI driver")
        save()
        capacity = dev.reply(0xFC14)
        maximum = int.from_bytes(capacity[:4],"big")
        report.update(buffer_reply=capacity.hex(),reported_transfer_capacity=maximum)
        save()
        require(maximum in (256,512,1024,2048,4096,8192,16384,32768),
                "Unexpected flash read transfer capacity")
        report.update(status="reading_flash",flash_capacity=FLASH_SIZE,read_block_bytes=256)
        save()
        digests = []
        for attempt in (1,2):
            digest = hashlib.sha256()
            with (root / f"flash-{attempt}.bin").open("xb") as file:
                for address in range(0,FLASH_SIZE,256):
                    data = dev.execute(command(0xFD05,address.to_bytes(4,"big")+b"\x01\x00"),incoming=256)
                    file.write(data)
                    digest.update(data)
                file.flush()
                os.fsync(file.fileno())
            digests.append(digest.hexdigest())
            report.update(completed_reads=attempt,sha256=digests)
            save()
        require(digests[0] == digests[1],"The two complete flash reads differ")
        report.update(status="two_physical_reads_verified",identical=True)
    except BaseException as error:
        report.update(status="stopped",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if dev:
            report["scsi_transactions"] = dev.transactions
            dev.close()
        save()
        print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
