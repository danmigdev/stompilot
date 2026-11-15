# Chocolate Plus V13: USB HOST MIDI pacing

This experimental patch addresses consecutive TANK SysEx messages being sent
in one USB transfer. Direct tests with the TANK accepted separated messages
but did not apply the consecutive pair. The patch ends a HOST transfer at each
SysEx terminator and yields the MIDI routing task for three ticks before a
SysEx start. Ordinary messages and USB device-mode transfer layout retain the
original behavior. Saved assignments are unchanged.

On the tested Chocolate Plus, four sectors have been written and verified with
two complete physical flash reads. After a cold restart in U, the complete
running app, pacing module, relocated flash driver, normal save controls and
configuration RAM/flash verified. **The physical A/B test in HOST is pending.**
Private installation evidence is under `artifacts/sysex-install/`; no device
backup, vendor firmware, loader binary, build output or personal path belongs
in Git. The earlier investigation history is retained locally under ignored
`artifacts/`.

## Exact scope

Only the pinned `FootCtrlPlus_013` release is supported:

| Item | SHA-256 |
| --- | --- |
| Original FWSC | `b5ba2041482640315c7dc70a0f7c79c9e81d24989226763868dc36dab81801c3` |
| Original app | `f535c717a1ce23ef1b0fe5251c0ab7c906f2be068d2c172ad8440be1e0edeadc` |
| Patched app | `c8f000ec252f20987df9bb1ed98ea4bbf718a11806c153787ec41dc3b08ad85a` |
| Padded pacing module | `b084c9d607210116d5887729c391541e786d6760fa027f83639455c973d4e5cd` |
| Reviewed FWSC candidate | `cd3ea30c4c4c662baf274f55e9e5d964a5879ccc3a03a7bf7a34ee8ba38c2a00` |

The linked module is 202 bytes, padded to 224, at mapped address `0x1E265C0`.
Two four-byte hooks replace the USB builder instructions at `0x1E0FF54` and
the HOST sender's status call at `0x1E119CC`. No static RAM or heap is added.
The delay occurs in `midi_route`, before the sender disables interrupts, and
connection status is rechecked after yielding. It is not inserted in the
switch scanner's interrupt context. Three ticks are not a measured wire delay.

The packager preserves `config.dat`, updates the application MD5 resource and
appends `midigap.bin`. `flash2.bin` grows from `0x27600` to `0x27700`.
Native code at `0x1E14DA6..0x1E14E38` clamps VM length to 16 KiB, forces sector
mode at that length, rounds the start up to 4 KiB and usable length down to
8 KiB. Both layouts retain two 4-KiB VM banks at `0x28000` and `0x29000`.
The installer requires the extension to be physically blank and preserves all
bytes after `0x27700`, including Bluetooth, calibration and user configuration.

The old save-fix candidate was **never installed and has been withdrawn**:
its proposed placement overlapped `config.dat` and `md5.bin`. Its generated
images were removed. The retired sources, build scripts and tests are kept
only in the local, ignored `artifacts/retired-save-fix-20261115.zip` archive.
Configuration saving remains the separate verified Stompilot implementation.

## Reproduce the offline candidate

Use Python 3.12 and a Linux/WSL environment with a native C compiler plus the
JieLi Linux toolchain `20250324.1` (`pi32v2/` and `common/` directories).
Run these from the repository root; substitute the actual toolchain location.

```sh
python research/chocolate-firmware/inspect_firmware.py --download --version 013
python research/chocolate-firmware/inspect_firmware.py --download --version 012
bash research/chocolate-firmware/disassemble.sh /path/to/jieli-toolchain 013
bash research/chocolate-firmware/build_sysex_pacing.sh /path/to/jieli-toolchain
python research/chocolate-firmware/trace_sysex_pacing.py
python research/chocolate-firmware/test_package_sysex_pacing.py
python research/chocolate-firmware/package_sysex_pacing.py
```

V12 is only a wrong-version test fixture. The inspector verifies downloaded
hashes before extraction. The resulting FWSC retains its original
`EXPERIMENTAL_NOT_INSTALLED` build filename; installation status is recorded
separately in each device's deployment report. Do not infer updater acceptance
from the container's passing structural checks: the tested installation used
the bounded raw-sector installer, not Bluetooth OTA or M-UPGRADE.

Validation: 131,580 C checks with ASan/UBSan, 251 bounded instruction traces and
16 packaging tests. The trace byte-checks instructions but stubs ROM decoding,
memory copying and USB/scheduler functions; it is not hardware emulation.
Windows diagnostic tests reject unrelated commands, ranges and device IDs.
Installer tests cover partial operations, lost replies, failed blank checks,
damaged programming, preserved regions and explicit restoration.

## Device workflow and recovery limits

The unchanged stock soft-key command enters `BR25 UBOOT1.00` through the normal
USB cable. [`probe_usb_boot.py`](probe_usb_boot.py) defaults to an offline
description; `--enter` checks the exact stock or pacing app, relocated boot
routine and saved configuration before sending it once.

The diagnostic requires the pinned loader from
[`kagaimiq/jl-uboot-tool`](https://github.com/kagaimiq/jl-uboot-tool/tree/adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a)
at `artifacts/jl-uboot-tool/data/loaderblobs/usb/br25loader.bin`, SHA-256
`0bc69023d6bf4c4d5d5bc048e06249ee39c484acd11196ffc73f88940787d77d`.
The loader is uploaded only to its fixed RAM range and read back before entry.
No chip-key operation is required. `GET_ONLINE_DEVICE` must report JEDEC
`854512`, matching the stock driver. This BR25 loader's separate `READ_ID`
reply does not have the generic three-byte layout; actual flash reads remain
256 bytes even though the loader reports a 32-KiB USB buffer.

`read_br25_boot.py --execute` reads two complete 256-KiB physical copies and
contains no flash erase/program commands. `--resume-loader` queries the already
running loader without uploading it again. Windows raw SCSI access requires
administrator rights; the PowerShell launchers use UAC without installing a
service or changing permanent policy.

`prepare_sysex_install.py --backup <completed-backup-directory>` constructs an
immutable, device-specific plan from both verified copies and the pinned
candidate. The four sector addresses, in write order, are `0x27000`, `0x10000`,
`0x12000`, then `0x1000` (directory/CRCs last). `install_sysex_pacing.py` is
offline by default; explicit execution rechecks the complete device twice,
performs two blank checks per sector, programs exact aligned 256-byte pages,
and verifies every page, each sector and two final complete flash images.

An interrupted operation stops without retry or reset. Keep the RAM loader
connected. The explicit `--restore` mode restores original bytes in the same
four sectors and requires every other sector still to match that backup; it
does not overwrite later configuration changes. Restoration in a mock NOR
model has passed; an actual restoration has not yet been needed.

After verified programming, power-cycle in U and run
`verify_sysex_pacing.py` for read-only app/module, driver and configuration
checks. Then test with the TANK on HOST. Stompilot accepts this exact app and
complete module hash for verified configuration saves; changing firmware
during a pending save is rejected.

**Recovery from an application that cannot boot remains unverified with only
the normal USB cable.** Successful software-selected entry is not independent
cold recovery; see the [upstream boot-entry documentation](https://github.com/kagaimiq/jl-uboot-tool/blob/adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a/docs/how-to-enter-uboot.md).
Boot bytes remain unchanged, but this patch has no transactional boot rollback.
