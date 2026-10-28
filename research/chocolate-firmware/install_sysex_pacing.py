"""Install/restore only the four sectors of a validated V13 pacing plan.

Default is offline. --execute requires the already running, verified BR25 RAM
loader and Windows administrator rights. No loader upload, reset, whole-chip
erase, key programming, or writes outside the exact backed-up plan are allowed.
After an interrupted command, stop without retrying or rebooting the device.
"""
from __future__ import annotations

import argparse
import binascii
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from prepare_sysex_install import (HERE, FULL_SIZE, SECTOR_SIZE, PAGE_SIZE,
                                  SECTOR_ORDER, load_plan, require, sha)
from read_br25_boot import Device, command, gate as read_gate, verify_online_device


class FlashGate:
    def __init__(self, target):
        require(len(target) == FULL_SIZE, "Expected complete validated target")
        self.target = target
        self.sector = None
        self.erased = set()
        self.programmed = set()
        self.ready = False

    def arm(self, address):
        require(self.sector is None and address in SECTOR_ORDER and address not in self.erased,
                "Sector not allowed, already attempted or previous sector not verified")
        self.sector, self.ready = address, False

    def blank_verified(self, first, second):
        require(self.sector in self.erased and first == second == b"\xff" * SECTOR_SIZE,
                "Two complete blank checks required before programming")
        self.ready = True

    def finish(self, first, second):
        require(self.sector is not None and self.ready, "No verified sector in progress")
        expected = self.target[self.sector:self.sector+SECTOR_SIZE]
        require(first == second == expected, "Complete sector readback differs from target")
        self.sector, self.ready = None, False

    def authorize(self, cdb, outgoing, incoming):
        code = int.from_bytes(cdb[:2], "big")
        if code == 0xFB01:
            require(self.sector is not None and self.sector not in self.erased and
                    cdb == command(code,self.sector.to_bytes(4,"big")) and
                    outgoing is None and incoming == 16, "Erase outside the armed sector or repeat request")
            self.erased.add(self.sector)  # Mark BEFORE I/O: a lost reply never permits a retry.
        elif code == 0xFB04:
            address = int.from_bytes(cdb[2:6],"big")
            require(self.sector is not None and self.ready and address not in self.programmed and
                    self.sector <= address < self.sector+SECTOR_SIZE and address % PAGE_SIZE == 0,
                    "Program outside the blank-verified sector or repeat request")
            expected = self.target[address:address+PAGE_SIZE]
            fields = address.to_bytes(4,"big") + PAGE_SIZE.to_bytes(2,"big")
            require(cdb == command(code,fields+b"\0"+binascii.crc_hqx(expected,0).to_bytes(2,"little"))
                    and outgoing == expected and incoming == 0, "Page does not match the immutable plan")
            self.programmed.add(address)
        else:
            read_gate(cdb,outgoing,incoming,"loader",b"")


class PatchDevice(Device):
    def __init__(self, path, target):
        self.flash_gate = FlashGate(target)
        super().__init__(path,b"")
        self.phase = "loader"

    def authorize(self, cdb, outgoing, incoming):
        self.flash_gate.authorize(cdb,outgoing,incoming)


def read_region(dev, address, size):
    require(address % PAGE_SIZE == size % PAGE_SIZE == 0 and size > 0 and
            0 <= address < address+size <= FULL_SIZE, "Invalid physical read extent")
    return b"".join(dev.execute(command(0xFD05,pos.to_bytes(4,"big")+b"\x01\x00"),incoming=PAGE_SIZE)
                    for pos in range(address,address+size,PAGE_SIZE))


def verify_preserved(current, original):
    require(len(current) == len(original) == FULL_SIZE, "Incomplete flash image")
    for pos in range(0,FULL_SIZE,SECTOR_SIZE):
        if pos not in SECTOR_ORDER:
            require(current[pos:pos+SECTOR_SIZE] == original[pos:pos+SECTOR_SIZE],
                    f"Device differs from backup outside patch sectors at {pos:#x}")


def transfer(dev, original, target, restore, record):
    record("preflight_two_full_reads")
    current = read_region(dev,0,FULL_SIZE)
    require(current == read_region(dev,0,FULL_SIZE), "Unstable preflight flash reads")
    verify_preserved(current,original)
    if not restore:
        require(current in (original,target), "Flash is neither the original nor the complete target; use restore")
    record("preflight_verified",preflight_sha256=sha(current))
    for address in SECTOR_ORDER:
        expected = target[address:address+SECTOR_SIZE]
        if current[address:address+SECTOR_SIZE] == expected:
            continue
        dev.flash_gate.arm(address)
        record("erase_armed",sector=address)
        dev.reply(0xFB01,address.to_bytes(4,"big"))
        dev.flash_gate.blank_verified(read_region(dev,address,SECTOR_SIZE),read_region(dev,address,SECTOR_SIZE))
        record("blank_verified",sector=address)
        for pos in range(address,address+SECTOR_SIZE,PAGE_SIZE):
            page = target[pos:pos+PAGE_SIZE]
            if page == b"\xff" * PAGE_SIZE:
                continue
            record("program_armed",address=pos)
            fields = pos.to_bytes(4,"big") + PAGE_SIZE.to_bytes(2,"big")
            dev.execute(command(0xFB04,fields+b"\0"+binascii.crc_hqx(page,0).to_bytes(2,"little")),outgoing=page)
            require(read_region(dev,pos,PAGE_SIZE) == page, "Page readback mismatch; stop without retry or reset")
        dev.flash_gate.finish(read_region(dev,address,SECTOR_SIZE),read_region(dev,address,SECTOR_SIZE))
        record("sector_verified",sector=address)
    first, second = read_region(dev,0,FULL_SIZE), read_region(dev,0,FULL_SIZE)
    require(first == second == target, "Final complete physical reads differ from target")
    verify_preserved(first,original)
    record("physically_verified_restart_required",verified_sha256=sha(first),complete_matching_reads=2,
           native_execution_verified=False)
    return first, second


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan",type=Path,default=HERE / "artifacts/sysex-install")
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--restore",action="store_true",help="Restore original bytes in the same four sectors")
    args = parser.parse_args()
    plan, original, patched = load_plan(args.plan)
    target = original if args.restore else patched
    if not args.execute:
        print(json.dumps(dict(hardware_access=False,mode="restore" if args.restore else "install",
                              sector_order=list(SECTOR_ORDER),target_sha256=sha(target))))
        return
    require(os.name == "nt", "Windows SCSI transport required")
    root = args.plan / ("restore-" if args.restore else "install-") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    root.mkdir(parents=True)
    report = dict(mode="restore" if args.restore else "install",target_sha256=sha(target),
                  original_sha256=sha(original),events=[],status="starting",restart_sent=False)

    def record(status,**fields):
        report.update(status=status,**fields)
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(),phase=status,**fields))
        temporary = root / "report.tmp"
        with temporary.open("w",encoding="utf-8") as file:
            json.dump(report,file,indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary,root / "report.json")

    record("starting")
    dev = None
    try:
        query = "@(Get-CimInstance Win32_DiskDrive | Where-Object { $_.Model -eq 'BR25 UBOOT1.00 USB Device' } | Select-Object -ExpandProperty DeviceID) | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",query],
                                check=True,capture_output=True,text=True,creationflags=0x08000000,timeout=20)
        paths = json.loads(result.stdout or "[]")
        if isinstance(paths,str): paths = [paths]
        require(len(paths) == 1,"Expected exactly one BR25 UBOOT1.00 device")
        dev = PatchDevice(paths[0],target)
        identity = dev.execute(bytes.fromhex("12 00 00 00 24 00"),incoming=36)
        require(identity[8:16].strip() == b"BR25" and identity[16:32].strip() == b"UBOOT1.00",
                "SCSI target differs from BR25 ROM")
        verify_online_device(dev.reply(0xFC0A))
        require(int.from_bytes(dev.reply(0xFC14)[:4],"big") == 32768,"Loader buffer differs from verified diagnostic")
        for number,data in enumerate(transfer(dev,original,target,args.restore,record),1):
            with (root / f"verified-{number}.bin").open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
    except BaseException as error:
        record("stopped_do_not_restart",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if dev is not None:
            report.update(erase_requests=len(dev.flash_gate.erased),program_requests=len(dev.flash_gate.programmed),
                          scsi_transactions=dev.transactions)
            dev.close()
        record(report["status"])
        print(json.dumps({key:value for key,value in report.items() if key != "events"},indent=2))


if __name__ == "__main__":
    main()
