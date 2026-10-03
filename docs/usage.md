# Development and usage notes

The [README](../README.md) covers the application workflows and screenshots.
These notes describe controller write scope, device profiles, and source layout.

## Controller write scope

A project replaces the Advanced Custom short-press assignments for switches A-D
on its included pages, with both banks when the switch alternates. It activates
Advanced Custom mode, sets the active page count to the project's page count,
and selects the first page.

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
Only changed bytes are written, followed by a complete readback verification.
Failed acknowledgements or a different readback are reported as errors.

Reading and **Load into editor** never write. Importing does not retrieve a
connected TANK's active effect settings: it copies Chocolate MIDI assignments.
Unsupported active modes, gestures, or command types prevent editable import
rather than being silently converted or dropped.

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
`core.register_protocol(name, encoder, validator=None)`. The encoder receives
`(action, profile)` and returns `CompiledAction`. JSON profiles contain data
and do not execute code. Adding Python integrations requires rebuilding the
portable app.

Raw imported actions use `type: raw_midi` and `message_hex` without a device,
block, or model ID. They preserve CC/SysEx bytes without a semantic profile and
do not install new device profiles. Their validation covers complete CC or
SysEx messages, 7-bit data, and the controller's bank constraints; it does not
infer what an unknown destination device does with those messages.

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

The [solo-boost example](../examples/tank_mini_live_boost.json) uses Boost ON,
Hall Reverb Mix 20, and Dual Delay Mix 20 in one state. Its second state reloads
the saved current preset. This restores every saved effect state and parameter,
not a fixed list of OFF values. The
[reload evidence](../research/tank_preset_reload.md) records the experimental
packet and static V09 normal-mode path.

## Source layout

- `midi_configurator/app.py`: interface, startup choices, and action editors.
- `midi_configurator/core.py`: MIDI compilation and validation.
- `midi_configurator/chocolate.py`: controller image decoding and write planning.
- `midi_configurator/chocolate_transport.py`: native USB configuration protocol.
- `midi_configurator/controller_ui.py`: reads, import, backup, and write workflow.
- `midi_configurator/project.py`: empty projects and example helpers.
- `midi_configurator/runtime_paths.py`: bundled resources and writable storage.
- `midi_configurator/profiles/`: built-in profiles and evidence metadata.
- `midi_configurator/test_*.py`: offline compiler and simulated controller tests.
- `examples/`: semantic projects and the generic CC profile template.
- `docs/screenshots/`: application screenshots for the README.
- `requirements.txt`: runtime USB MIDI dependencies.
- `requirements-build.txt` and `Stompilot.spec`: executable build configuration.
- `Setup Stompilot.cmd`, `Build Stompilot.cmd`, and `Launch Stompilot.cmd`: source
  setup, executable build, and launch helpers.

## Offline checks

The regression suite checks known SysEx vectors, checksums, offsets and limits,
profile validation, raw MIDI roundtrips, native controller import, configuration
encoding, simulated USB responses, backups, and readback behavior. The GUI smoke
test checks local editor workflows without writing to a controller.

Run the commands listed in the [README](../README.md#run-or-build-from-source).
After source changes, rebuild the executable so released code and embedded
profiles match the source.
