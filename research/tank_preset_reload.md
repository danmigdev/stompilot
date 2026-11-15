# TANK Mini V09: reload the current saved preset

The stock V09 firmware exposes a path that can restore the saved version of the
currently selected preset without changing its index. The implementation in
Stompilot is experimental: the packet encoding and normal-mode firmware path
have been checked statically. On 2026-10-16, sent to a `TANK MINI_009` over its
own USB by `tank_check memory`, it restored the saved preset exactly, with the
preset index unchanged. The reload through the Chocolate Plus HOST port has not
been tested.

## Packet

```text
F0 00 32 09 49 00 00 00 02 01 00 00 00 12 00 00 00 7F 35 03 F7
```

This is one complete 21-byte SysEx frame. The decoded vendor command is `0x22`
and its nine-byte payload is:

```text
04 01 00 00 20 01 00 00 FF
```

| Field | Meaning |
| --- | --- |
| `04` | Memory-write descriptor type 4 |
| `01 00 00 20` | Address `0x20000001`, little endian |
| `01 00 00` | One-byte write length |
| `FF` | Replacement byte for the cached previous preset index |

The current preset index remains unchanged. Setting the cached previous index
to `0xFF` makes the normal main loop detect a difference and enter its existing
preset-load path. The packet does not contain a save-to-flash operation.

## Firmware evidence

The app binary examined has SHA-256:

```text
bc46bc604d42067449cdf5b8200f8ebbafd668b722243e3db1b033289e49bdea
```

The offsets below are file offsets in that V09 binary, rather than addresses
that should be copied into MIDI commands.

| File offset | Static observation |
| --- | --- |
| `0x6812`, `0x6818` | Initialization registers the type-4 memory read/write descriptor. |
| `0x79A2C` | Original descriptor location; its app read/write handlers are at `0xC3DE` and `0xC510`. |
| `0x14FBE`–`0x14FFA` | Command `0x22` dispatch reaches the descriptor write handler. |
| `0xC548` | The state-memory branch bounds offset plus length to 17 bytes and maps it to base `0x1C1322C`. The write at `0x20000001` targets state byte 1. |
| `0xBE62`–`0xBE7E` | The normal main loop compares current preset index `state[0]` with cached previous index `state[1]`. A difference calls the preset loader at `0x6634`. |
| `0x6634` | The preset loader copies 92 saved bytes from the preset bank at current-index times 92 into the live patch, then rebuilds the DSP in the normal path. |

When `state[4]` indicates a special mode, the normal preset-load path is deferred.
Tuner or other special modes can therefore delay the reload until normal mode
resumes. This behavior is a static inference from the inspected branch, not a
physical test result.

The supporting research artifacts are named `app-pi32v2-r3-disassembly.txt` and
`three-presets-results.json`; they are not redistributed in this repository.
Previous preset tests found that sending the same
Program Change repeatedly did not reload presets 1A, 1B, or 1C, while switching
away and back did. That result motivated the cached-index write; it does not
verify that this new packet performs a physical reload. The full 21-byte frame
is kept unchanged in the controller assignment.

## Project behavior

The action is device-wide, with type `reload_current_preset`; it is not an FX,
MOD, or other individual block action. It must be the only action in its
switch state. Restoring a preset replaces every live block state and parameter,
including Tremolo and any values changed by another switch.

The saved preset determines the returned Boost and Reverb settings. Unsaved
live edits are discarded, while the stored preset is retained. Stompilot
provides separate examples for explicit Boost OFF / Mix 5 commands and preset
reload. At startup, the user chooses a project file, reads the controller, or
creates an empty project.
