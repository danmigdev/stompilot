# Development and usage notes

The [README](../README.md) covers the application workflows and screenshots.
These notes describe the desktop architecture, controller write scope, device
profiles, source layout, and documentation captures.

## Desktop interface and service

The Svelte frontend is compiled into `frontend/build/` and packaged with the
Windows executable. `midi_configurator/desktop.py` hosts it in pywebview using
Microsoft Edge WebView2. The Python `DesktopService` owns project files,
profile validation, MIDI compilation, controller reads, backups, and writes.
UI requests cross the desktop bridge; rendering the interface does not itself
discover or modify a MIDI device.

Startup requires an explicit saved project, controller read, or empty project.
A controller read only shows the configuration; it never creates or replaces a
project. **Write to Chocolate Plus** in the editor opens the controller page,
scans and reads when no controller is connected and exactly one is found, and
requests the preview; writing still needs its own confirmation. The source banner shows the saved filename and tracks local changes.
Saving a project creates or updates a JSON file; applying it to hardware
requires a separate read, preview, and confirmed write.

## Interface preferences

Global zoom affects application text, controls, dialogs, and application
confirmations and errors. Use the Zoom controls or Ctrl + +, Ctrl + -, Ctrl + 0,
and Ctrl + mouse wheel. Zoom ranges from 75% to 200% in 25-point steps.
Large content remains available through scrolling.
Windows file pickers retain their system appearance.

Zoom is stored in `settings.json`, separately from projects. It does not alter
a project's MIDI assignments or its local modification state. The percentage
button resets zoom to 100%.

The window's normal bounds are stored there as `window`, in logical pixels,
when the window closes; a maximized window keeps its restore bounds and
reopens maximized. At startup they are reused only when the title bar lands on
a connected screen, with the size limited to that screen. The desktop smoke
test starts from a saved zoom and window and checks that both come back and
that the window is saved again on close.

Documentation captures, the smoke test, and the screenshot mode use an isolated
temporary data directory, including their preferences, so they cannot change
the user's saved zoom or window.

## Controller write scope

A project replaces the Advanced Custom short-press assignments for switches A-D
on its included pages, with both banks when the switch alternates. It activates
Advanced Custom mode, sets the active page count to the project's page count,
and selects the first page.

Each SysEx bank is written as a length byte followed by complete `F0`…`F7`
messages, up to 127 bytes. A bank written by Stompilot v0.3.2 or earlier, which
lacks the length byte, is rewritten even when its messages are unchanged; the
preview marks these banks.

Data for unused Advanced Custom pages and the Keyboard and Mixed mode banks is
preserved. An included empty bank clears its active assignments. Single mode
retains inactive Bank B. Replacing an Advanced switch that uses press/release
or a long-press gesture can change its behavior; the preview reports this.

Before writing, Stompilot reads a fresh controller image and creates
`.backups/chocolate-plus-<UTC timestamp>.json` beside the app. The backup contains
the full image as hexadecimal, its SHA-256 hash, device identity, and firmware
version. A backup failure prevents writing.

If the controller differs from the displayed preview, Stompilot requires a new
read and review. It also checks for changes between the backup and the write.
For the exact audited stock **V13**, Stompilot saves an additional complete
24-KiB configuration backup in `.backups/verified-saves/<transaction>/`.
Only sectors whose persistent contents differ are erased. Each 4-KiB sector is
verified blank, programmed in aligned 256-byte pages and read back; two complete
physical flash reads must match the intended image. The internal six bytes,
configuration marker and unused reserved tail are preserved. Firmware, boot,
Bluetooth/calibration and VM areas are outside the write range.

This uses a temporary 16-byte data table in unused SysEx padding, selected from
the current configuration. All original padding is restored. A configuration
with no suitable padding, a different V13 build, unstable reads or unexpected
control state is refused. The firmware's normal save pointer is temporarily
disabled during the operation and restored by a physical restart. The existing
RAM assignments stay in place until that restart loads the newly saved image.

When **Flash verified** appears, set O, unplug USB-C from the pedal and leave the
display off for 10 seconds; return to U, reconnect and choose **Verify after
restart**. This checks the normal save controls, two RAM images and two complete
mapped configuration images. Until verification succeeds, another project write
is blocked. An unfinished save is shown again after reopening Stompilot.

If a transfer fails, Stompilot does not retry the failed mutation or report
success. Keep the pedal powered and connected in U and select **Restore backup**.
It restores the checked full original configuration using the same bounded
erase/program sequence, then requires the restart check. Local project edits
remain available. A damaged backup is rejected before recovery writes.

Other supported firmware versions retain their changed-byte write path and
complete RAM readback. Permanent storage is not verified by that legacy path.
The former two-minute waiting advice was removed: it does not establish that
V13's partial flash writes are correct.

Reading never writes and never changes the open project. It also leaves the
write preview empty until **Preview changes** is chosen. A read shows the
Chocolate's MIDI assignments, not a connected TANK's active effect settings.

## Device-profile extensions

Built-in definitions are JSON files in `midi_configurator/profiles/`, packaged
inside the executable. Imported definitions are stored in `profiles/` beside
the portable app. Import through **Device profiles**, or export an existing
definition to create a variant. IDs and names must differ from existing ones.

The engine separates action semantics from the MIDI protocol:

```text
Page / switch / state
    -> ON/OFF and parameter actions
    -> device profile
    -> protocol encoder
    -> controller MIDI assignments
```

The `tank_v09` and `midi_cc` encoders are included. A device using CC can be
described by a new profile based on
[generic_cc_profile.json](../examples/generic_cc_profile.json). That template
must be adapted to the device's actual CC map.

For a different protocol, trusted Python code can register an encoder with
`core.register_protocol(name, encoder, validator=None, decoder=None)`. The
encoder receives `(action, profile)` and returns `CompiledAction`. The optional
decoder receives `(message, profile)` and proposes actions without `device_id`;
`core.decode_message` keeps only the proposals that the encoder turns into
exactly the same bytes. JSON profiles contain data and do not execute code.
Adding Python integrations requires rebuilding the portable app.

A block can declare `toggle` with a `cc` and an optional `value` (default 0)
in any protocol. The **Toggle on / off (CC)** action sends that CC, which the
device uses to switch the block from its current state. The TANK profile
declares its stock CCs 0 to 5.

Device-wide actions are declared in a profile's `device_actions`.
`select_preset` works with any protocol: its `presets` list names the presets
in Program Change order (1 to 128 unique names), and the action stores the
Program Change number as `value`, with a MIDI `channel`. `preset_volume` and
`reload_current_preset` are TANK writes, so they need a `tank_v09` profile.
Each needs its declaration; a project cannot use an action the profile does not
offer. A device with more than 128 presets would also need Bank Select, which
is not implemented.

The Chocolate stores no device information, so the read summary relies on
these decoders to name commands. `tank_v09` has one: ON/OFF, model, parameter,
preset volume, and reload writes decode to the matching actions. A parameter write names the
block and the parameter slot, so every model with that slot is a candidate,
and the summary lists each candidate's parameter. `midi_cc` has no decoder,
because the same CC number can mean different things on different devices; for
the same reason a Program Change is listed by number, not by preset name.
With several profiles of a decodable protocol, the first profile that produces
the bytes names the command.

Projects that Stompilot 0.3.3 and earlier imported from the controller contain
raw actions, with `type: raw_midi` and `message_hex` and no device, block, or
model ID, and each state keeps its `native_bank` bytes. These projects still
open and write: raw actions preserve CC/SysEx bytes, and a bank whose messages
are unchanged is written back exactly. Validation covers complete CC or SysEx
messages, 7-bit data, and the controller's bank constraints.

## TANK Mini profile details

The stock V09 profile contains 6 blocks and 61 model slots, with parameter
descriptors extracted from the firmware. State and parameter changes are SysEx
writes to the live patch. Continuous CC commands from the experimental firmware
extension are not used.

Choose the effect model in the TANK preset first. Selecting a model in the action
editor only chooses the corresponding parameter definitions. Model changes
may load defaults asynchronously, so the interface does not generate them.

Parameters use raw control values from 0 to 100. Firmware display ranges, such
as Boost Gain 0-60, remain metadata; no conversion to dB, Hz, or milliseconds is
assumed. AMP and CAB share parameter descriptors, and their 20 model slots are
labeled by index because commercial names have not been verified.

Program Change 0 to 20 selects presets 1A to 7C. The preset volume is the
live patch's first two bytes, a signed 16-bit level from 0 to 100; like every
live write it lasts until a preset is selected or reloaded.

On the device, selecting an FX, MOD, DLY, or REV model loads exactly that
model's defaults from the profile, which confirms the model numbering. AMP and
CAB keep their current values when the model changes, so the profile's AMP and
CAB defaults are not what the firmware loads.

`midi_configurator/tank_check.py` checks the profile on a TANK connected by
USB; the README describes its commands. Its live-patch layout: bytes 8-13 are
the ON/OFF of FX, AMP, MOD, DLY, REV, and CAB, bytes 14-19 their models, and 12
bytes per block from byte 20 their six 16-bit parameters. The preset state is
at `0x20000000` (current index, cached previous index), the saved presets at 0
(RAM, memory type 4) and `0x7003C000` (flash, type 5), 92 bytes each.
`test_tank_check.py` runs the same checks against a simulated TANK.

The [solo-boost example](../examples/tank_mini_live_boost.json) uses Boost ON,
Hall Reverb Mix 20, and Dual Delay Mix 20 in one state. Its second state reloads
the saved current preset. This restores every saved effect state and parameter,
not a fixed list of OFF values. The
[reload evidence](../research/tank_preset_reload.md) records the experimental
packet and static V09 normal-mode path.

## Source layout

- `frontend/src/App.svelte`: startup, project editor, profiles, controller page,
  zoom, and confirmations.
- `frontend/src/ActionEditor.svelte`: semantic editors, and raw MIDI editors for
  projects imported by earlier releases.
- `frontend/src/bridge.ts`: typed desktop/test bridge requests.
- `frontend/tests/`: isolated browser interaction tests.
- `midi_configurator/desktop.py`: pywebview/WebView2 window and desktop bridge.
- `midi_configurator/desktop_service.py`: project, profile, and USB workflows.
- `midi_configurator/core.py`: MIDI compilation and validation.
- `midi_configurator/chocolate.py`: controller image decoding and write planning.
- `midi_configurator/chocolate_transport.py`: native USB configuration protocol.
- `midi_configurator/chocolate_persistence.py`: audited stock V13 sector saving,
  durable complete backups, interruption recovery and restart verification.
- `frontend/src/LiveView.svelte`: the Android live view.
- `midi_configurator/project.py`: empty projects and example helpers.
- `midi_configurator/runtime_paths.py`: bundled resources and writable storage.
- `midi_configurator/profiles/`: built-in profiles and evidence metadata.
- `midi_configurator/test_*.py`: offline compiler and simulated controller tests.
- `examples/`: semantic projects and the generic CC profile template.
- `docs/screenshots/`: application screenshots for the README.
- `docs/capture_screenshots.py`: actual compiled frontend captures with
  read-only USB fixtures and isolated file/preference storage.
- `docs/preview_server.py`: local test API backed by the actual Python service.
- `requirements.txt`: Python desktop host and USB MIDI dependencies.
- `requirements-docs.txt`: documentation browser dependencies.
- `requirements-build.txt` and `Stompilot.spec`: executable build configuration.
- `Setup Stompilot.cmd`, `Build Stompilot.cmd`, and `Launch Stompilot.cmd`: source
  setup, executable build, and launch helpers.

## Offline checks

The regression suite checks known SysEx vectors, checksums, offsets and limits,
profile validation, decoding of every bundled TANK action back to the same
bytes, the read summary, raw MIDI and native bank roundtrips, configuration
encoding, simulated USB responses, backups, readback behavior, and desktop
service state. Svelte type checks and Playwright tests cover editing, raw byte
preservation, confirmations, zoom, keyboard access, and narrow windows. The
desktop smoke test checks the packaged interface and Python bridge without
MIDI discovery or writing.

`midi_configurator/test_desktop_ui.py` tests the compiled frontend against the
actual isolated `DesktopService`. The unittest discovery command includes these
checks, but they skip when the frontend build, Python Playwright, or its Chromium
browser is unavailable. To enable and run them explicitly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-docs.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m unittest midi_configurator.test_desktop_ui -v
```

Run the commands listed in the [README](../README.md#run-or-build-from-source).
After source changes, rebuild the executable so released code and embedded
profiles match the source.

## Documentation screenshots

The README images are captures of the compiled Svelte UI in Chromium, using
Playwright. A `ThreadingHTTPServer` binds only to `127.0.0.1` and serves the local
frontend and test bridge. That bridge delegates to the actual `DesktopService`
with an owned temporary data directory and simulated discovery/USB transport.
File pickers use temporary project copies; settings and exports stay inside
that directory. The screenshot browser blocks external network requests.

The use-case fixture is
[tank_mini_live_boost.json](../examples/tank_mini_live_boost.json), compiled into
a Chocolate configuration image by the real planner. The helper explicitly requests the preview,
never clicks the write button, and checks that there were zero write attempts.
The fake transport rejects every write request.

Each browser screenshot shows a fixture banner where the full window is
captured. Action-editor images are crops of the actual rendered modal. These
images document the UI, not a hardware write or a native WebView2 capture.

To reproduce them after building the frontend:

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
.\.venv\Scripts\python.exe -m pip install -r requirements-docs.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe docs/capture_screenshots.py
```

For integration checks, use `with PreviewServer() as preview` from
`docs.preview_server`, inject its `BRIDGE_SCRIPT` before navigation, and open
`preview.url`. `preview.service` is the isolated Python service;
`preview.example_project`, `preview.device`, and `preview.write_attempts` expose
the fixture and write-attempt counter. `preview.cancel_pickers = True` simulates
file-dialog cancellation without changing a project.
