# Stompilot

A visual MIDI configurator for Chocolate Plus, with a built-in TANK Mini profile
and support for future device profiles. Create switch assignments, edit a local
copy of the controller configuration, and apply it over USB when ready.

## Download and start

Download [Stompilot.exe from release v0.1.0](https://github.com/danmigdev/stompilot/releases/tag/v0.1.0)
and open it from a writable folder. The portable Windows app includes its Python
runtime and USB MIDI dependencies: no Python installation or setup script is
needed. In a source checkout, **Launch Stompilot.cmd** prefers the built executable.

The built-in TANK profile travels with the executable. Imported profiles are
stored in `profiles/` beside it, and controller backups in `.backups/`. Copy those
folders too when moving existing data. Save project files wherever you choose.

## Start from a file or a controller

The welcome screen offers **Open saved project**, **Read Chocolate Plus**, and
**Create empty project**. Examples are opened explicitly; startup does not load
or write a configuration automatically.

![Startup choices for a saved project, controller read, or empty project](docs/screenshots/startup.png)

**Open saved project** loads a Stompilot JSON file. **Read Chocolate Plus** opens
a read-only controller window: select the USB device, choose **Connect and read**,
inspect its assignments, then choose **Load into editor** for an editable local
copy. The editor's **Read controller** button opens the same workflow.

![Read-only Chocolate Plus connection and import window](docs/screenshots/controller-read.png)

The source banner shows the filename or controller read time, together with the
local modification state. **Save project** saves that local copy to a file.
**Write to controller** opens the programming preview; its final **Write project
to controller** button applies the displayed project. Reading and loading into
the editor never write to the controller.

The Chocolate stores MIDI assignments, not the TANK's current preset values.
Stompilot preserves imported MIDI bytes without guessing a device, effect, or
model. Firmware 16 Advanced Custom configurations with single or alternating
switches and CC or complete SysEx messages can be imported. Unsupported active
modes, gestures, or command types remain available for read-only inspection and
cannot be loaded into the editor.

## Use case: Tremolo and a solo boost

Open [tank_mini_live_boost.json](examples/tank_mini_live_boost.json) with **Open
saved project**. It contains one page with these assignments:

| Switch | State 1 / Bank A | State 2 / Bank B |
| --- | --- | --- |
| A: Tremolo | Tremolo ON | Tremolo OFF |
| B: Solo boost | Boost ON + Hall Reverb Mix 20 + Dual Delay Mix 20 | Reload the current saved preset (experimental) |
| C, D | No messages | — |

![Project editor showing Boost ON with Reverb and Delay Mix 20](docs/screenshots/project-editor.png)

Prepare the preset on the TANK: FX = Boost, MOD = Tremolo, REV = Hall, and DLY =
Dual. Reverb and Delay must already be enabled; the solo state changes their Mix
values and does not enable those blocks. Mix 20 is a control value on the 0-100
scale. Save the desired base sound on the TANK before using preset reload. To
return to Boost OFF, save the base preset with FX disabled while leaving Boost
selected as its effect model.

Switch B's first state contains three SysEx messages, totaling 65 bytes. Its
second state contains only the 21-byte reload command. Reload restores **all**
saved states and parameters, including Tremolo, and discards temporary changes.
The saved preset determines the returned Boost, Reverb, and Delay settings; the
second state does not hard-code Boost OFF or Mix 5.

![Second solo-boost state containing only Reload saved preset](docs/screenshots/reload-state.png)

Reload is experimental: its normal-mode stock V09 firmware path was checked
statically, but physical reload through the Chocolate Plus is still unverified.
Tuner or other special modes may defer it until normal mode resumes. It does
not save or overwrite a preset. See the [reload protocol evidence](research/tank_preset_reload.md).

## Edit switches and actions

Select a page and switch A-D, then choose **One sequence** or **Alternate two
states**. Each state has its own ordered action list. Add or edit an ON/OFF
action, an effect parameter, or the device-wide **Reload current preset** action.
The effect chosen in an action must already be selected in the TANK preset;
selecting it in Stompilot does not change the hardware's effect model.

| Effect ON/OFF | Parameter value | Preset reload |
| --- | --- | --- |
| ![Effect toggle action editor](docs/screenshots/effect-toggle-editor.png) | ![Effect parameter action editor](docs/screenshots/parameter-editor.png) | ![Experimental reload action editor](docs/screenshots/reload-action-editor.png) |

Review the MIDI preview after editing. Reload must be the only action in its
state. **Setup notes** explains required preset settings and commands that still
need a hardware test; it does not modify the project or controller.

![Setup notes for preset requirements and verification status](docs/screenshots/setup-notes.png)

## Edit assignments read from the Chocolate

Imported assignments appear as **Imported MIDI**. Their exact commands remain
available even when no matching device profile exists.

![Editor containing a local copy of imported controller assignments](docs/screenshots/imported-editor.png)

A CC command can be edited by channel, controller number, and value. A SysEx
command uses a hexadecimal editor, with complete-frame and 7-bit validation.
These editors preserve raw commands rather than labeling them as a particular
TANK effect.

| Imported CC | Imported SysEx |
| --- | --- |
| ![Imported CC editor with channel, controller number, and value](docs/screenshots/imported-cc-editor.png) | ![Imported SysEx hexadecimal editor](docs/screenshots/imported-sysex-editor.png) |

## Review and write over USB

1. Connect the Chocolate Plus by USB and close MidiSuite.
2. In the editor, choose **Write to controller**, select the USB device, and
   choose **Connect and read**.
3. Review the **Project to write** preview and the **Current controller
   configuration** tab.
4. Choose **Write project to controller** to apply the reviewed local project.

![Controller programming preview for the local project](docs/screenshots/controller-write-preview.png)

![Current controller configuration tab](docs/screenshots/controller-current.png)

Before writing, Stompilot reads the current configuration again and saves an
exact backup beside the app. It writes changed bytes and reads the result back
to verify it. A failed backup, changed controller configuration, failed
acknowledgement, or readback mismatch stops the operation or reports an error.
See [write scope and backup details](docs/usage.md#controller-write-scope).

Controller screenshots use simulated configuration data for documentation;
they do not demonstrate a physical write.

## Device profiles and future devices

**Device profiles** browses profile information, notes, and sources, and imports
or exports JSON definitions. It is not a visual profile-definition editor.
Export a profile, edit the definition in a text editor, then import it with a
new ID and name. TANK Mini stock V09 is the only bundled device profile.

![Device profiles window with TANK Mini information and JSON import/export](docs/screenshots/device-profiles.png)

For another device using CC, adapt the [generic CC profile template](examples/generic_cc_profile.json).
Other protocols can use a registered Python encoder. JSON profiles are data and
do not execute code. See [profile and encoder details](docs/usage.md#device-profile-extensions).

## Manual MidiSuite workflow

Use **Copy for MidiSuite** for the selected state, or **Export messages** for a
text summary of all assignments. In MidiSuite, choose the switch, mode, and Bank
A/B. Paste all SysEx lines into the same **SysEx (hex)** field, or add one CC row
per command, then save the controller configuration there. The exported text is
a human-readable summary, not a native MidiSuite import file.

## Supported scope and verification

- Chocolate Plus USB programming targets firmware 16 (`FootCtrlPlus_016`): up to
  8 pages, 16 CC rows per bank, and 128 SysEx bytes per bank. Bluetooth
  configuration is not implemented.
- A bank uses CC or SysEx. Multiple complete SysEx frames are concatenated within
  that bank's 128-byte limit; physical forwarding of consecutive frames still
  needs verification.
- USB discovery, identity, and configuration reading have been verified on a
  connected firmware 16 controller. The tested one-page configuration produced
  a zero-change write preview after import. Physical writing remains unverified;
  write, backup, and readback behavior is checked with simulated controllers.
- The TANK profile comes from stock V09 firmware analysis. MOD SysEx has been
  tested on the device; the solo parameter sequence and preset reload still
  need a complete physical test. Parameter display-unit conversions are not
  assumed.

More technical context is in [development and usage notes](docs/usage.md) and the
[Chocolate protocol notes](research/chocolate_protocol.md).

## Run or build from source

Source development requires Python 3.12 or later with Tkinter. **Setup
Stompilot.cmd** creates `.venv` and installs the USB MIDI dependencies. **Build
Stompilot.cmd** installs `requirements-build.txt` and builds `dist/Stompilot.exe`
using PyInstaller and `Stompilot.spec`. Python is needed to build, not to run the
released executable.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe midi_configurator/app.py
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean Stompilot.spec
```

Offline checks do not write to connected hardware:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s midi_configurator -t . -v
.\.venv\Scripts\python.exe midi_configurator/app.py --check
.\.venv\Scripts\python.exe midi_configurator/app.py --smoke-test
```
