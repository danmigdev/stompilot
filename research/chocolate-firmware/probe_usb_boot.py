"""Use V13's unchanged USB soft-key entry after checked read-only snapshots.

Default is an offline description. --enter sends the one audited stock reset
packet once. It deliberately disconnects MIDI and may leave the screen blank
in ROM USB download mode. It never sends erase, program, upload or firmware data.
Powering fully off exits a software-selected ROM session; independently entering
ROM when the application is broken is a different, still unverified capability.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os

from capture_persistence import capture, UsbChocolate, list_usb_devices
from inspect_firmware import ENTRY_POINT, HERE, RELEASES, require, sha
from midi_configurator.chocolate_transport import _mido
from midi_configurator.chocolate_persistence import PACED_APP_SHA256, PACING_BASE, PACING_SIZE, PACING_SHA256

COMMAND = bytes.fromhex("f0 22 24 35 7d f7")
BOOT_RAM, BOOT_XIP, BOOT_SIZE = 0x1C934, 0x1E25648, 28


def read(device, address, length):
    require((address == ENTRY_POINT and length == 155488) or
            (address == BOOT_RAM and length == BOOT_SIZE) or
            (address == PACING_BASE and length == PACING_SIZE), "Outside fixed probe read ranges")
    out = bytearray()
    while len(out) < length:
        count = min(512, length - len(out))
        prefix = b"\x04" + (address + len(out) - 0x13490).to_bytes(4,"little") + count.to_bytes(3,"little")
        value = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
        require(len(value) == count, "Incomplete probe read")
        out.extend(value)
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enter", action="store_true")
    args = parser.parse_args()
    stock = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(stock) == RELEASES["013"]["app_sha256"], "Original application mismatch")
    require(stock[BOOT_XIP-ENTRY_POINT:BOOT_XIP-ENTRY_POINT+BOOT_SIZE] == bytes.fromhex(
        "bf ea 0a fe af ea 26 96 af ea d6 5d 40 22 87 ea 41 db 60 e0 f0 09 00 ef 10 00 f7 9f"),
        "Stock boot routine mismatch")
    require(stock[0x1E23D0C-ENTRY_POINT+0x4D:0x1E23D0C-ENTRY_POINT+0x55] ==
            bytes.fromhex("04 f0 22 24 07 35 7d f7"), "Stock command template mismatch")
    if not args.enter:
        print(json.dumps(dict(device_io=False, command=COMMAND.hex(" "),
                              operation="stock software-selected USB ROM entry", flash_writes=0)))
        return
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected exactly one Chocolate Plus in U")
    dest = HERE / "artifacts/usb-boot-probe" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest.mkdir(parents=True)
    report = dict(status="preflight", flash_writes=0, firmware_uploads=0, entry_requests=0,
                  independent_recovery_established=False)

    def journal():
        data = (json.dumps(report, indent=2) + "\n").encode()
        with (dest / "probe.json").open("wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())

    journal()
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        require(device.query().name == "FootCtrlPlus_013", "Unexpected device identity")
        app = read(device, ENTRY_POINT, len(stock))
        require(app == stock or sha(app) == PACED_APP_SHA256,
                "Installed application differs from the two audited V13 builds")
        if app != stock:
            require(sha(read(device,PACING_BASE,PACING_SIZE)) == PACING_SHA256,
                    "Installed pacing module differs; boot request not sent")
        boot = read(device, BOOT_RAM, BOOT_SIZE)
        require(boot == stock[BOOT_XIP-ENTRY_POINT:BOOT_XIP-ENTRY_POINT+BOOT_SIZE],
                "Relocated boot routine differs")
        ram, flash, _ = capture(device, dest / "before")
        require(ram == flash[:len(ram)], "Unsaved RAM settings; entry not sent")
        report.update(app_sha256=sha(app), configuration_sha256=sha(ram),
                      mapped_usrflash_sha256=sha(flash), status="preflight_verified")
        journal()
    # Release the MIDI input before reset to avoid stale Windows native handles.
    midi = _mido()
    with midi.open_output(ports[0].output_name) as output:
        report.update(status="entry_about_to_send", entry_requests=1)
        journal()
        output.send(midi.Message.from_bytes(COMMAND))
    report["status"] = "entry_sent_enumeration_not_yet_verified"
    journal()
    print(json.dumps(report, indent=2))
    print("Probe record:", dest.relative_to(HERE))


if __name__ == "__main__":
    main()
