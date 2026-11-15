"""Temporary RAM-only B-bank length isolation for the installed pacing build.

Disables the stock save pointer before changing exactly one known length byte.
The complete persistent configuration is compared before/after. Restore returns
the original RAM byte; a cold restart is then required to restore save controls.
No firmware command, physical flash erase/program or pointer re-enable is sent.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent.parent))
from midi_configurator.chocolate_persistence import (
    VerifiedWriter, PACED_APP_SHA256, RAM_BASE, POINTER, DISABLED_POINTER,
    FLASH_BASE, FLASH_VIEW, FLASH_SIZE, CONFIG_SIZE)
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices
from inspect_firmware import require, sha

LENGTH_OFFSET = 93 + 417 + 161
FX_ON = bytes.fromhex("f0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 01 42 03 f7")
REV_MIX = bytes.fromhex("f0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 0a 00 64 04 f7")


def target_for(original):
    require(len(original) == CONFIG_SIZE and original[0] == 3 and original[23640] == 0,
            "Expected advanced mode, page 1")
    require(original[LENGTH_OFFSET] == 43 and
            original[LENGTH_OFFSET+1:LENGTH_OFFSET+44] == FX_ON+REV_MIX,
            "B is not the captured FX/reverb pair")
    target = bytearray(original)
    target[LENGTH_OFFSET] = len(FX_ON)
    return bytes(target)


def write_fixed(device,writer,address,data):
    require((address,data) in ((POINTER,struct.pack("<I",DISABLED_POINTER)),
                              (RAM_BASE+LENGTH_OFFSET,b"\x15"),(RAM_BASE+LENGTH_OFFSET,b"\x2b")),
            "Outside fixed diagnostic RAM writes")
    expected_pointer = FLASH_BASE if address == POINTER else DISABLED_POINTER
    require(writer.read(POINTER,4) == struct.pack("<I",expected_pointer),"Unexpected save pointer")
    prefix = b"\x04"+(address-RAM_BASE).to_bytes(4,"little")+len(data).to_bytes(3,"little")
    require(device._request(0x22,prefix+data,0)["payload"] == b"\0","RAM write was not acknowledged")
    require(writer.read(address,len(data)) == data,"RAM write readback differs")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare",action="store_true")
    group.add_argument("--restore",type=Path)
    args=parser.parse_args()
    folder=args.restore or HERE/"artifacts/sysex-isolation"/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if args.prepare: folder.mkdir(parents=True)
    report=json.loads((folder/"session.json").read_text()) if args.restore else dict(
        status="starting",firmware_writes=0,physical_flash_write_commands=0,
        original_length=43,test_length=21,length_offset=LENGTH_OFFSET,events=[])

    def save(status,**fields):
        report.update(status=status,**fields)
        report["events"].append(dict(utc=datetime.now(timezone.utc).isoformat(),phase=status,**fields))
        temp=folder/"session.tmp"
        with temp.open("w",encoding="utf-8") as file:
            json.dump(report,file,indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp,folder/"session.json")

    save("preflight")
    ports=list_usb_devices()
    require(len(ports)==1,"Expected one Chocolate in U")
    with UsbChocolate(ports[0].input_name,ports[0].output_name) as device:
        writer=VerifiedWriter(device,folder)
        writer.audit()
        require(writer.firmware_sha256 == PACED_APP_SHA256,"Expected installed pacing build")
        if args.prepare:
            writer.normal_controls()
            original=device.read_config()
            require(original==device.read_config(),"Unstable configuration RAM")
            target=target_for(original)
            flash=writer.read(FLASH_VIEW,FLASH_SIZE)
            require(flash==writer.read(FLASH_VIEW,FLASH_SIZE) and flash[:CONFIG_SIZE]==original,
                    "Persistent config differs from RAM")
            for name,data in (("original.bin",original),("test.bin",target),("flash.bin",flash)):
                with (folder/name).open("xb") as file:
                    file.write(data);file.flush();os.fsync(file.fileno())
            save("pointer_disable_armed",original_sha256=sha(original),test_sha256=sha(target),flash_sha256=sha(flash))
            write_fixed(device,writer,POINTER,struct.pack("<I",DISABLED_POINTER))
            save("length_change_armed")
            write_fixed(device,writer,RAM_BASE+LENGTH_OFFSET,b"\x15")
        else:
            original=(folder/"original.bin").read_bytes()
            target=(folder/"test.bin").read_bytes()
            flash=(folder/"flash.bin").read_bytes()
            require(sha(original)==report["original_sha256"] and sha(target)==report["test_sha256"]
                    and sha(flash)==report["flash_sha256"] and target_for(original)==target,"Backup mismatch")
            current=device.read_config()
            require(current in (original,target),"RAM differs beyond the one diagnostic byte")
            save("return_from_host",test_byte_survived=current==target)
            if current==target:
                save("restore_length_armed")
                write_fixed(device,writer,RAM_BASE+LENGTH_OFFSET,b"\x2b")
            target=original
        require(device.read_config()==target,"Complete RAM readback differs")
        require(writer.read(FLASH_VIEW,FLASH_SIZE)==writer.read(FLASH_VIEW,FLASH_SIZE)==flash,
                "Persistent configuration differs; keep the backup")
        pointer=struct.unpack("<I",writer.read(POINTER,4))[0]
        require(pointer in (FLASH_BASE,DISABLED_POINTER),"Unexpected final save pointer")
        if not args.prepare and pointer==FLASH_BASE:
            writer.normal_controls()
        status=("test_ready_in_RAM" if args.prepare else
                "RAM_restored_cold_restart_required" if pointer==DISABLED_POINTER else
                "original_config_and_normal_controls_verified")
        save(status,flash_unchanged=True,save_pointer=pointer,
             restart_required=pointer==DISABLED_POINTER)
    print(json.dumps({k:v for k,v in report.items() if k!='events'},indent=2))
    print(folder.resolve().relative_to(HERE))


if __name__=="__main__":
    main()
