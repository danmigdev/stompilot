"""Check the TANK Mini profile against a TANK Mini connected to this PC by USB.

    .venv\\Scripts\\python -m midi_configurator.tank_check show
    .venv\\Scripts\\python -m midi_configurator.tank_check memory
    .venv\\Scripts\\python -m midi_configurator.tank_check listen mod
    .venv\\Scripts\\python -m midi_configurator.tank_check relay

``memory`` sends every ON/OFF, model and parameter command of the profile, as
the exact bytes Stompilot writes to the Chocolate Plus, and reads the TANK's
live patch back after each one: only the bytes the command names may change.
Models and parameters are checked with their block off, so test values are not
heard. ``listen`` plays each model and alternates each parameter for the ear,
because memory shows what the TANK stored, not what it sounds like. ``relay``
forwards what the Chocolate Plus sends when a switch is pressed and shows what
changed on the TANK.

Only the live patch is written, plus the cached previous-preset byte in the
reload check. The saved presets in RAM and flash are compared before and
after, and the live patch is restored and verified. Nothing is saved.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Callable

from .chocolate_transport import _mido, _port_name, list_usb_devices
from .core import (PRESET_VOLUME, RELOAD_CURRENT_PRESET, SELECT_PRESET, TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE, ValidationError,
                   compile_action, decode_message, describe_decoded, format_hex, load_profiles,
                   packet, parse_packet, tank_live_write)
from .runtime_paths import data_directory, resource_dir


DEVICE_ID = "tank_mini_v09"
STATE = 0x20000000  # [current preset index, cached previous index, mode, ...]
STATE_SIZE = 4
PRESETS = 21  # 1A to 7C
BANK_SIZE = PRESETS * TANK_LIVE_PATCH_SIZE + 10
FLASH_BANK = 0x7003C000  # memory type 5
READ_CHUNK = 256
SETTLE = 0.05
# The firmware loads a model's defaults asynchronously; wait, then until stable.
MODEL_SETTLE = 0.4
RELOAD_SETTLE = 0.8
ACK_TIMEOUT = 0.25
LISTEN_INTERVAL = 1.2


class TankError(RuntimeError):
    """A readable TANK connection or check error."""


def enable_offset(block: dict) -> int:
    return 8 + block["wire_id"]


def model_offset(block: dict) -> int:
    return 14 + block["wire_id"]


def parameter_area(block: dict) -> range:
    return range(20 + 12 * block["wire_id"], 32 + 12 * block["wire_id"])


def parameter_offset(block: dict, parameter: dict) -> int:
    # The catalog's own offset, not the encoder's formula, so the two are compared.
    return parameter.get("patch_offset", parameter_area(block).start + 2 * parameter["index"])


def preset_name(index: int) -> str:
    return f"{index // 3 + 1}{'ABC'[index % 3]}" if 0 <= index < PRESETS else f"index {index}"


def _model(block: dict, wire_value: int) -> dict | None:
    return next((model for model in block.get("models", []) if model["wire_value"] == wire_value), None)


def _model_label(block: dict, wire_value: int) -> str:
    model = _model(block, wire_value)
    return model["label"] if model else f"model {wire_value}"


def _on_off(value: int) -> str:
    return {0: "OFF", 1: "ON"}.get(value, str(value))


def _word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little")


def _volume(live: bytes) -> int:
    """Bytes 0-1: the preset's level, a signed 16-bit value from 0 to 100."""
    return int.from_bytes(live[:2], "little", signed=True)


def _program_change(message: bytes) -> int | None:
    return message[1] if len(message) == 2 and message[0] & 0xF0 == 0xC0 else None


def describe_live(live: bytes, profile: dict) -> list[str]:
    lines = []
    for block in profile["blocks"]:
        model = _model(block, live[model_offset(block)])
        values = ", ".join(f"{parameter['label']} {_word(live, parameter_offset(block, parameter))}"
                           for parameter in (model or {}).get("parameters", []))
        lines.append(f"{block['label']:<4} {_on_off(live[enable_offset(block)]):<3}  "
                     f"{_model_label(block, live[model_offset(block)])}: {values}")
    return lines


def describe_changes(before: bytes, after: bytes, profile: dict) -> list[str]:
    """Name each changed live-patch field by the block and model in ``after``."""
    lines, named = [], {0, 1}
    if _volume(before) != _volume(after):
        lines.append(f"Preset volume: {_volume(before)} -> {_volume(after)}")
    for block in profile["blocks"]:
        enable, selected = enable_offset(block), model_offset(block)
        named |= {enable, selected}
        if before[enable] != after[enable]:
            lines.append(f"{block['label']}: {_on_off(before[enable])} -> {_on_off(after[enable])}"
                         f" ({_model_label(block, after[selected])})")
        if before[selected] != after[selected]:
            lines.append(f"{block['label']} model: {_model_label(block, before[selected])} -> "
                         f"{_model_label(block, after[selected])}")
        parameters = {p["index"]: p["label"] for p in (_model(block, after[selected]) or {}).get("parameters", [])}
        for slot, offset in enumerate(parameter_area(block)[::2]):
            named |= {offset, offset + 1}
            if _word(before, offset) != _word(after, offset):
                lines.append(f"{block['label']} / {parameters.get(slot, f'slot {slot + 1}')}: "
                             f"{_word(before, offset)} -> {_word(after, offset)}")
    lines += [f"byte {offset}: {old} -> {new}" for offset, (old, new) in enumerate(zip(before, after))
              if offset not in named and old != new]
    return lines


def expectation(action: dict, profile: dict, saved: bytes) -> tuple[dict[int, int], range | tuple]:
    """The live-patch bytes an action must set, and the bytes it may change freely.

    For a reload ``saved`` is the current preset's saved record; for a preset
    selection it is the selected preset's.
    """
    if action["type"] in (RELOAD_CURRENT_PRESET, SELECT_PRESET):
        return dict(enumerate(saved)), ()
    if action["type"] == PRESET_VOLUME:
        return {0: action["value"] & 255, 1: action["value"] >> 8}, ()
    block = next(block for block in profile["blocks"] if block["id"] == action["block_id"])
    if action["type"] == "enable":
        return {enable_offset(block): int(bool(action["value"]))}, ()
    model = next(model for model in block["models"] if model["id"] == action["model_id"])
    if action["type"] == "model":
        return {model_offset(block): model["wire_value"]}, parameter_area(block)
    parameter = next(p for p in model["parameters"] if p["id"] == action["param_id"])
    offset = parameter_offset(block, parameter)
    return {offset: action["value"] & 255, offset + 1: action["value"] >> 8}, ()


def check_values(parameter: dict, current: int) -> list[int]:
    """Minimum, maximum and middle, ordered so that the first write changes the value."""
    low, high, step = parameter["min"], parameter["max"], parameter.get("step", 1)
    middle = low + (high - low) // 2 // step * step
    values = list(dict.fromkeys((low, high, middle)))
    while values[0] == current and len(values) > 1:
        values.append(values.pop(0))
    return values


def listen_values(parameter: dict) -> tuple[int, int]:
    """A quarter and three quarters of the range: audible, without extreme levels or feedback."""
    low, high, step = parameter["min"], parameter["max"], parameter.get("step", 1)
    span = high - low
    return low + span // 4 // step * step, low + span * 3 // 4 // step * step


@dataclass(frozen=True)
class Snapshot:
    state: bytes
    live: bytes
    ram_bank: bytes
    flash_bank: bytes

    def saved_preset(self, index: int | None = None) -> bytes:
        index = self.state[0] if index is None else index
        return self.ram_bank[index * TANK_LIVE_PATCH_SIZE:(index + 1) * TANK_LIVE_PATCH_SIZE]


def _tank_port(name: str) -> bool:
    """Windows names the TANK's ports "USB-Midi"; Android may use its product name."""
    name = name.casefold()
    return "usb-midi" in name or "tank" in name


class UsbTank:
    """One TANK Mini on its own USB MIDI ports. Call close() when done."""

    def __init__(self, midi, input_name: str, output_name: str, *, timeout: float = 1.0,
                 ack_timeout: float = ACK_TIMEOUT):
        self.midi = midi
        self.timeout = timeout
        self.ack_timeout = ack_timeout
        self.input = midi.open_input(input_name)
        try:
            self.output = midi.open_output(output_name)
        except Exception:
            self.input.close()
            raise

    @classmethod
    def connect(cls, midi=None, **options) -> "UsbTank":
        midi = midi or _mido()
        inputs = [name for name in midi.get_input_names() if _tank_port(name)]
        outputs = [name for name in midi.get_output_names() if _tank_port(name)]
        pairs = [(i, o) for i in inputs for o in outputs if _port_name(i) == _port_name(o)]
        if len(pairs) != 1:
            raise TankError(f"Connect exactly one TANK Mini by USB (found {len(pairs)} USB-Midi ports) "
                            "and close other MIDI editors.")
        try:
            return cls(midi, *pairs[0], **options)
        except Exception as error:
            raise TankError(f"Cannot open the TANK Mini's MIDI ports: {error}. Close other MIDI editors.") from error

    def close(self):
        for port in (self.output, self.input):
            try:
                port.close()
            except Exception:
                pass

    def _drain(self):
        for _ in self.input.iter_pending():
            pass

    def transmit(self, message: bytes):
        self.output.send(self.midi.Message.from_bytes(list(message)))

    def _wait(self, opcode: int, prefix: bytes = b"", timeout: float | None = None) -> dict | None:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for message in self.input.iter_pending():
                if message.type != "sysex":
                    continue
                try:
                    response = parse_packet(bytes(message.bytes()))
                except ValidationError:
                    continue
                if response["opcode"] == opcode and response["payload"].startswith(prefix):
                    return response
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def identify(self) -> str:
        self._drain()
        self.transmit(packet(0x11))
        response = self._wait(0x11)
        if response is None:
            raise TankError("The TANK Mini did not answer. Check the USB cable and close other MIDI editors.")
        return response["payload"][:16].split(b"\0", 1)[0].decode("ascii", "replace")

    def read(self, address: int, length: int, memory_type: int = 4) -> bytes:
        data = bytearray()
        while len(data) < length:
            count = min(READ_CHUNK, length - len(data))
            query = bytes([memory_type]) + (address + len(data)).to_bytes(4, "little") + count.to_bytes(3, "little")
            self._drain()
            self.transmit(packet(0x23, query))
            response = self._wait(0x23, query)
            if response is None or len(response["payload"]) != 8 + count:
                raise TankError(f"The TANK Mini did not return {count} bytes at 0x{address + len(data):08X}.")
            data += response["payload"][8:]
        return bytes(data)

    def send(self, message: bytes) -> bytes | None:
        """Send one message as the Chocolate Plus would; return a SysEx's acknowledgement, if any."""
        self._drain()
        self.transmit(message)
        if message[0] != 0xF0:
            return None
        response = self._wait(0x00, timeout=self.ack_timeout)
        return None if response is None else response["payload"]

    def snapshot(self) -> Snapshot:
        return Snapshot(self.read(STATE, STATE_SIZE), self.read(TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE),
                        self.read(0, BANK_SIZE), self.read(FLASH_BANK, BANK_SIZE, 5))


def open_tank(profile: dict, midi=None, **options) -> tuple[UsbTank, str]:
    tank = UsbTank.connect(midi, **options)
    try:
        name = tank.identify()
        if name != profile["firmware"]:
            raise TankError(f"The connected TANK reports {name!r}; this profile is for {profile['firmware']}.")
    except Exception:
        tank.close()
        raise
    return tank, name


class Checker:
    def __init__(self, tank: UsbTank, profiles: dict, *, out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep):
        self.tank = tank
        self.profiles = profiles
        self.profile = profiles[DEVICE_ID]
        self.out = out
        self.sleep = sleep
        self.results: list[dict] = []
        self.notes: list[str] = []

    def live(self) -> bytes:
        return self.tank.read(TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE)

    def _settled(self, wait: float) -> bytes:
        self.sleep(wait)
        live = self.live()
        for _ in range(20):
            self.sleep(0.1)
            again = self.live()
            if again == live:
                break
            live = again
        return live

    def _write(self, offset: int, data: bytes):
        self.tank.send(tank_live_write(offset, data))
        self.sleep(SETTLE)

    def command(self, action: dict):
        return compile_action({"device_id": DEVICE_ID, **action}, self.profiles)

    def check(self, label: str, action: dict, saved: bytes = b"", *, messages=None, expected=None,
              settle: float = SETTLE) -> tuple[dict, bytes]:
        """Send an action (or given messages) and require exactly its expected live-patch change."""
        compiled = None if messages is not None else self.command(action)
        messages = messages if messages is not None else compiled.messages
        if expected is None:
            expected, free = expectation(action, self.profile, saved)
        else:
            free = ()
        before = self.live()
        acks = [self.tank.send(message) for message in messages]
        if free or settle > SETTLE:
            after = self._settled(settle)
        else:
            self.sleep(settle)
            after = self.live()
        wanted = bytearray(before)
        for offset, value in expected.items():
            wanted[offset] = value
        for offset in free:
            wanted[offset] = after[offset]
        result = {"check": label, "summary": compiled.summary if compiled else label,
                  "messages_hex": [format_hex(message) for message in messages],
                  "ack": [None if ack is None else ack.hex() for ack in acks],
                  "expected": describe_changes(before, bytes(wanted), self.profile),
                  "actual": describe_changes(before, after, self.profile), "ok": after == bytes(wanted)}
        self.results.append(result)
        if not result["ok"]:
            self.out(f"FAIL {label}\n     expected: {'; '.join(result['expected']) or 'no change'}"
                     f"\n     actual:   {'; '.join(result['actual']) or 'no change'}")
        return result, after

    def restore(self, original: bytes) -> bool:
        """Put the live patch back: per block OFF, model, parameters, then its own state."""
        live = self.live()
        for block in self.profile["blocks"]:
            enable, selected, area = enable_offset(block), model_offset(block), parameter_area(block)
            if all(live[i] == original[i] for i in (enable, selected, *area)):
                continue
            self._write(enable, b"\x00")
            if live[selected] != original[selected]:
                self._write(selected, original[selected:selected + 1])
                self._settled(MODEL_SETTLE)
            self._write(area.start, original[area.start:area.stop])
            self._write(enable, original[enable:enable + 1])
        live = self.live()
        for offset, (actual, wanted) in enumerate(zip(live, original)):
            if actual != wanted:
                self._write(offset, bytes([wanted]))
        return self.live() == original

    # memory: every command of the profile, checked by readback.

    def memory(self) -> dict:
        start = self.tank.snapshot()
        self.out(f"Preset {preset_name(start.state[0])}. Checking every TANK command by reading the live patch back.")
        try:
            for block in self.profile["blocks"]:
                self._block(block, start)
            capabilities = {item["id"]: item for item in self.profile.get("device_actions", [])}
            if PRESET_VOLUME in capabilities:
                self._preset_volume()
            if SELECT_PRESET in capabilities:
                self._select_preset(start, capabilities[SELECT_PRESET]["presets"])
            if RELOAD_CURRENT_PRESET in capabilities:
                self._reload(start)
        finally:
            restored = self.restore(start.live)
        end = self.tank.snapshot()
        integrity = {"live_patch_restored": restored and end.live == start.live,
                     "preset_unchanged": end.state[0] == start.state[0],
                     "saved_presets_in_ram_unchanged": end.ram_bank == start.ram_bank,
                     "saved_presets_in_flash_unchanged": end.flash_bank == start.flash_bank}
        failed = [result["check"] for result in self.results if not result["ok"]]
        self.out(f"\n{len(self.results)} checks: {len(self.results) - len(failed)} passed, {len(failed)} failed.")
        for note in self.notes:
            self.out(f"note {note}")
        for name, ok in integrity.items():
            self.out(f"{'ok  ' if ok else 'FAIL'} {name.replace('_', ' ')}")
        return {"preset": preset_name(start.state[0]), "ok": not failed and all(integrity.values()),
                "failed": failed, "notes": self.notes, "integrity": integrity, "checks": self.results}

    def _block(self, block: dict, start: Snapshot):
        label, enable = block["label"], enable_offset(block)
        original = self.live()
        ok = len(self.results)
        toggle = block.get("toggle")
        if toggle:
            # The stock CC handler is the firmware's own definition of this block's switch.
            for _ in range(2):
                current = self.live()[enable]
                self.check(f"{label}: stock CC {toggle['cc']} toggle", {"type": "toggle", "block_id": block["id"]},
                           expected={enable: 0 if current else 1})
        state = bool(self.live()[enable])
        for value in (not state, state, False):
            self.check(f"{label}: {_on_off(int(value))}", {"type": "enable", "block_id": block["id"], "value": value})
        counted = 0
        for model in block.get("models", []):
            action = {"type": "model", "block_id": block["id"], "model_id": model["id"]}
            result, live = self.check(f"{label}: model {model['label']}", action, settle=MODEL_SETTLE)
            differences = [f"{p['label']} {p['default']} in the profile, {_word(live, parameter_offset(block, p))} loaded"
                           for p in model["parameters"] if _word(live, parameter_offset(block, p)) != p["default"]]
            result["defaults_match"] = not differences
            if differences:
                self.notes.append(f"{label} / {model['label']} defaults: {'; '.join(differences)}")
            for parameter in model["parameters"]:
                for value in check_values(parameter, _word(live, parameter_offset(block, parameter))):
                    _, live = self.check(f"{label} / {model['label']} / {parameter['label']} = {value}",
                                         {**action, "type": "parameter", "param_id": parameter["id"], "value": value})
                    counted += 1
        restored = self.restore(original)
        passed = all(result["ok"] for result in self.results[ok:])
        self.out(f"{'ok  ' if passed else 'FAIL'} {label}: ON/OFF, {len(block.get('models', []))} models, "
                 f"{counted} parameter writes{'' if restored else '; live patch NOT restored'}")

    def _preset_volume(self):
        current = _volume(self.live())
        first = len(self.results)
        for value in (current - 40 if current >= 40 else current + 40, current):
            self.check(f"Preset volume = {value}", {"type": PRESET_VOLUME, "value": value})
        passed = all(result["ok"] for result in self.results[first:])
        self.out(f"{'ok  ' if passed else 'FAIL'} Preset volume: {current}, {current - 40 if current >= 40 else current + 40} and back")

    def _select_preset(self, start: Snapshot, presets: list[str]):
        """Select the next preset and come back; each must load exactly as saved."""
        index = start.state[0]
        other = index + 1 if index + 1 < len(presets) else index - 1
        first = len(self.results)
        for target in (other, index):
            result, _ = self.check(f"Select preset {presets[target]}", {"type": SELECT_PRESET, "value": target},
                                   start.saved_preset(target), settle=RELOAD_SETTLE)
            selected = self.tank.read(STATE, STATE_SIZE)[0]
            if selected != target:
                result["ok"] = False
                self.out(f"FAIL Select preset {presets[target]}: the TANK is on {preset_name(selected)}")
        passed = all(result["ok"] for result in self.results[first:])
        self.out(f"{'ok  ' if passed else 'FAIL'} Select preset: {presets[other]} and back to {presets[index]}, "
                 "each loaded exactly as saved")

    def _reload(self, start: Snapshot):
        # Make one unsaved change that the reload must undo, on a block that is off when possible.
        live = self.live()
        block = next((b for b in self.profile["blocks"] if not live[enable_offset(b)] and _model(b, live[model_offset(b)])),
                     self.profile["blocks"][0])
        model = _model(block, live[model_offset(block)]) or block["models"][0]
        parameter = model["parameters"][0]
        current = _word(live, parameter_offset(block, parameter))
        value = parameter["min"] if current != parameter["min"] else parameter["max"]
        self._write(parameter_offset(block, parameter), value.to_bytes(2, "little"))
        result, after = self.check("Reload current preset", {"type": RELOAD_CURRENT_PRESET},
                                   start.saved_preset(), settle=RELOAD_SETTLE)
        state = self.tank.read(STATE, STATE_SIZE)
        if state[0] != start.state[0]:
            result["ok"] = False
            self.out(f"FAIL Reload current preset changed the preset to {preset_name(state[0])}")
        result["marker"] = f"{block['label']} / {parameter['label']} set to {value} before the reload"
        self.out(f"{'ok  ' if result['ok'] else 'FAIL'} Reload current preset: live patch "
                 f"{'equals' if after == start.saved_preset() else 'differs from'} saved preset {preset_name(start.state[0])}")

    # listen: the same commands, for the ear.

    def listen(self, block_id: str | None, model_ids: list[str], ask: Callable[[str], str] = input,
               interval: float = LISTEN_INTERVAL) -> dict:
        blocks = [b for b in self.profile["blocks"] if block_id in (None, b["id"])]
        if not blocks:
            raise TankError(f"Unknown block {block_id!r}; use {', '.join(b['id'] for b in self.profile['blocks'])}.")
        original = self.live()
        heard = []
        self.out("Turn the volume down to a comfortable level and play while each model is on.")
        try:
            for block in blocks:
                for model in [m for m in block["models"] if not model_ids or m["id"] in model_ids]:
                    record = {"block": block["label"], "model": model["label"], "parameters": {}}
                    heard.append(record)
                    answer = record["heard"] = self._ask(
                        ask, f"{block['label']} / {model['label']} is ON. Do you hear it? [y/n/r=repeat/s=skip/q=quit] ",
                        lambda: self._play_model(block, model))
                    for parameter in model["parameters"] if answer in ("y", "n") else ():
                        low, high = listen_values(parameter)
                        answer = record["parameters"][parameter["label"]] = self._ask(
                            ask, f"{block['label']} / {model['label']} / {parameter['label']} alternates {low} and {high}. "
                                 "Did the sound change? [y/n/r/s/q] ",
                            lambda: self._alternate(block, model, parameter, interval))
                        if answer in ("s", "q"):
                            break
                    self.tank.send(self.command({"type": "enable", "block_id": block["id"], "value": False}).messages[0])
                    if answer == "q":
                        return self._heard(heard)
        finally:
            self.restore(original)
        return self._heard(heard)

    def _heard(self, heard: list[dict]) -> dict:
        missing = [f"{r['block']} / {r['model']}" for r in heard if r["heard"] == "n"]
        missing += [f"{r['block']} / {r['model']} / {name}" for r in heard
                    for name, answer in r["parameters"].items() if answer == "n"]
        self.out("Not heard: " + (", ".join(missing) if missing else "none"))
        return {"ok": not missing, "not_heard": missing, "heard": heard}

    def _play_model(self, block: dict, model: dict):
        for action in ({"type": "enable", "value": False}, {"type": "model", "model_id": model["id"]}):
            self.tank.send(self.command({"block_id": block["id"], **action}).messages[0])
        self._settled(MODEL_SETTLE)
        self.tank.send(self.command({"type": "enable", "block_id": block["id"], "value": True}).messages[0])

    def _alternate(self, block: dict, model: dict, parameter: dict, interval: float):
        offset = parameter_offset(block, parameter)
        loaded = _word(self.live(), offset)
        for value in listen_values(parameter) * 3:
            self.tank.send(self.command({"type": "parameter", "block_id": block["id"], "model_id": model["id"],
                                         "param_id": parameter["id"], "value": value}).messages[0])
            self.sleep(interval)
        # The loaded value can lie outside the profile's range (CAB Hcut loads 180).
        self._write(offset, loaded.to_bytes(2, "little"))

    @staticmethod
    def _ask(ask: Callable[[str], str], prompt: str, play: Callable[[], None]) -> str:
        play()
        while True:
            answer = ask(prompt).strip().casefold()[:1]
            if answer == "r":
                play()
            elif answer in ("y", "n", "s", "q"):
                return answer

    # relay: what the Chocolate Plus sends, applied to the TANK.

    def relay(self, chocolate, *, keep: bool = False, stop: Callable[[], bool] = lambda: False) -> dict:
        original = self.live()
        preset = self.tank.read(STATE, STATE_SIZE)[0]
        presses = []
        self.out("Press the Chocolate Plus switches; each press is forwarded to the TANK. Ctrl+C stops.")
        try:
            while not stop():
                burst = [bytes(message.bytes()) for message in chocolate.iter_pending()]
                if not burst:
                    self.sleep(0.01)
                    continue
                self.sleep(0.05)  # A bank's frames arrive back to back.
                burst += [bytes(message.bytes()) for message in chocolate.iter_pending()]
                presses.append(self._forward(burst))
        except KeyboardInterrupt:
            pass
        finally:
            current = self.tank.read(STATE, STATE_SIZE)[0]
            if current != preset:
                # The old live patch belongs to another preset; selecting a preset already reloaded it.
                self.out(f"The TANK is now on preset {preset_name(current)}; its live patch is left as loaded.")
            elif not keep:
                self.out("Restoring the live patch." if self.restore(original) else "The live patch was NOT restored.")
        return {"ok": all(press["ok"] is not False for press in presses), "presses": presses}

    def _forward(self, burst: list[bytes]) -> dict:
        index = self.tank.read(STATE, STATE_SIZE)[0]
        selected = [_program_change(m) for m in burst if _program_change(m) is not None]
        target = selected[-1] if selected else index
        saved = self.tank.read(target * TANK_LIVE_PATCH_SIZE, TANK_LIVE_PATCH_SIZE)
        before = self.live()
        for message in burst:
            self.tank.transmit(message)
        after = self._settled(RELOAD_SETTLE)
        now = self.tank.read(STATE, STATE_SIZE)[0]
        decoded, wanted, ok = [], bytearray(before), now == target
        for message in burst:
            if _program_change(message) is not None:
                # A Program Change is not named by a profile (any device could use it), but the TANK must load it.
                decoded.append(f"Program Change {message[1]} (TANK preset {preset_name(message[1])})")
                wanted[:] = saved
                continue
            actions = decode_message(message, {DEVICE_ID: self.profile})
            decoded.append(describe_decoded(actions, self.profiles) if actions else f"not a {self.profile['name']} command: {format_hex(message)}")
            if not actions:
                ok = None if ok is True else ok
                continue
            expected, free = expectation(actions[0], self.profile, saved)
            for offset, value in expected.items():
                wanted[offset] = value
            for offset in free:
                wanted[offset] = after[offset]
        if ok is True:
            ok = after == bytes(wanted)
        changes = describe_changes(before, after, self.profile)
        if now != index:
            changes = [f"Preset {preset_name(index)} -> {preset_name(now)}"] + (
                ["loaded exactly as saved"] if after == saved else changes)
        self.out(f"\nReceived: {'; '.join(decoded)}\nTANK:     {'; '.join(changes) or 'no change'}"
                 f"\n{'as expected' if ok else 'NOT as expected' if ok is False else 'not checked'}")
        return {"received_hex": [format_hex(m) for m in burst], "received": decoded, "changes": changes, "ok": ok}


def show(tank: UsbTank, name: str, profile: dict, out: Callable[[str], None] = print) -> dict:
    state = tank.read(STATE, STATE_SIZE)
    live = tank.read(TANK_LIVE_PATCH, TANK_LIVE_PATCH_SIZE)
    saved = tank.read(state[0] * TANK_LIVE_PATCH_SIZE, TANK_LIVE_PATCH_SIZE)
    blocks = describe_live(live, profile)
    unsaved = describe_changes(saved, live, profile)
    out(f"{name}, preset {preset_name(state[0])}")
    out(f"  Volume {_volume(live)}")
    for line in blocks:
        out(f"  {line}")
    out("Unsaved changes: " + ("none" if not unsaved else "\n  " + "\n  ".join(unsaved)))
    return {"ok": True, "preset": preset_name(state[0]), "volume": _volume(live), "blocks": blocks, "unsaved": unsaved,
            "live_hex": live.hex(), "state_hex": state.hex()}


def main(argv=None, midi=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m midi_configurator.tank_check",
                                     description="Check the TANK Mini profile on a TANK Mini connected by USB.")
    parser.add_argument("--report", type=Path, help="JSON report path (default: research/tank-checks/)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="show the live patch and its unsaved changes")
    commands.add_parser("memory", help="check every command by reading the live patch back")
    listen = commands.add_parser("listen", help="play each model and parameter to check them by ear")
    listen.add_argument("block", nargs="?", help="fx, amp, mod, dly, rev or cab (default: all)")
    listen.add_argument("models", nargs="*", help="model IDs (default: all of the block)")
    relay = commands.add_parser("relay", help="forward Chocolate Plus presses to the TANK and show the result")
    relay.add_argument("--keep", action="store_true", help="keep the TANK's changes when stopping")
    args = parser.parse_args(argv)

    profiles = load_profiles(resource_dir() / "profiles")
    chocolate = None
    try:
        tank, name = open_tank(profiles[DEVICE_ID], midi)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        checker = Checker(tank, profiles)
        if args.command == "show":
            report = show(tank, name, checker.profile)
        elif args.command == "memory":
            report = checker.memory()
        elif args.command == "listen":
            report = checker.listen(args.block, args.models)
        else:
            devices = list_usb_devices()
            if len(devices) != 1:
                raise TankError("Connect exactly one Chocolate Plus by USB (switch on U) and close MidiSuite and Stompilot.")
            try:
                chocolate = (midi or _mido()).open_input(devices[0].input_name)
            except Exception as error:
                raise TankError(f"Cannot open the Chocolate Plus: {error}. Close MidiSuite and Stompilot.") from error
            report = checker.relay(chocolate, keep=args.keep)
    except TankError as error:
        print(error, file=sys.stderr)
        return 2
    finally:
        if chocolate is not None:
            chocolate.close()
        tank.close()

    now = datetime.now(timezone.utc)
    path = args.report or data_directory() / "research" / "tank-checks" / f"{now:%Y%m%dT%H%M%SZ}-{args.command}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"time_utc": now.isoformat(), "device": name, "profile": DEVICE_ID,
                                "command": args.command, **report}, indent=2), encoding="utf-8")
    print(f"Report: {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
