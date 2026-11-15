"""Read-only post-restart verification of the exact V13 MIDI pacing build.

Checks the complete running app, pacing module, relocated flash driver, normal
save controls, and two RAM/mapped-flash configuration copies. Sends only query
and read requests. HOST behavior still requires a test with the attached TANK.
"""
from datetime import datetime, timezone
import json
import sys

from prepare_sysex_install import HERE, load_plan, require, sha

sys.path.insert(0,str(HERE.parent.parent))
from midi_configurator.chocolate_persistence import (
    VerifiedWriter, PACED_APP_SHA256, PACING_BASE, PACING_SIZE, PACING_SHA256,
    CONFIG_SIZE, FLASH_BASE, FLASH_VIEW, FLASH_SIZE)
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices


def main():
    folder = HERE / "artifacts/sysex-install"
    plan, original, target = load_plan(folder)
    ports = list_usb_devices()
    require(len(ports) == 1,"Expected exactly one Chocolate in U")
    output = folder / "native-checks" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    report = dict(status="starting",device_writes=0,host_behavior_verified=False)
    try:
        with UsbChocolate(ports[0].input_name,ports[0].output_name) as device:
            writer = VerifiedWriter(device,output)
            writer.audit()
            require(writer.firmware_sha256 == PACED_APP_SHA256,"Running app is not the pacing build")
            writer.normal_controls()
            require(sha(writer.read(PACING_BASE,PACING_SIZE)) == PACING_SHA256,"Pacing module changed")
            ram = device.read_config()
            require(ram == device.read_config() == original[FLASH_BASE:FLASH_BASE+CONFIG_SIZE],
                    "Configuration RAM differs after restart")
            flash = writer.read(FLASH_VIEW,FLASH_SIZE)
            require(flash == writer.read(FLASH_VIEW,FLASH_SIZE) == original[FLASH_BASE:FLASH_BASE+FLASH_SIZE],
                    "Configuration flash differs after restart")
            (output / "configuration-ram.bin").write_bytes(ram)
            (output / "configuration-flash.bin").write_bytes(flash)
            report.update(status="native_U_verified_HOST_behavior_not_checked",app_sha256=writer.firmware_sha256,
                          module_sha256=PACING_SHA256,configuration_ram_sha256=sha(ram),
                          configuration_flash_sha256=sha(flash),normal_save_controls=True,
                          relocated_flash_driver_verified=True,ram_reads=2,mapped_flash_reads=2)
    except BaseException as error:
        report.update(status="stopped",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
