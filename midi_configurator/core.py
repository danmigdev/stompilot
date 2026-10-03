"""Offline, declarative MIDI compiler. No MIDI ports, hardware access, or plugins run.

Profiles are plain JSON data using schema_version 1. Protocol implementations
are deliberately built in: a new profile cannot execute code or write arbitrary
TANK memory. Profile-generated TANK commands affect the live patch or temporary
preset-selection state; they never save or overwrite presets. Imported raw MIDI
assignments preserve controller bytes without inferring a target device.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
SWITCHES = ("A", "B", "C", "D")
MAX_PAGES = 16
MAX_SYSEX_BYTES = 128
PROTOCOLS = ("tank_v09", "midi_cc")
PROTOCOL_ENCODERS: dict[str, Callable[[dict, dict], CompiledAction]] = {}
PROFILE_VALIDATORS: dict[str, Callable[[dict], None]] = {}
RELOAD_CURRENT_PRESET = "reload_current_preset"


class ValidationError(ValueError):
    """A readable error in a profile, project, or MIDI assignment."""


def _object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: expected a JSON object")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ValidationError(f"{path}: expected a list")
    return value


def _text(value: Any, path: str, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path}: expected nonempty text")
    return value


def _integer(value: Any, path: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"{path}: expected an integer from {low} to {high}")
    return value


def _unique(items: list, path: str) -> None:
    seen = set()
    for index, item in enumerate(items):
        _object(item, f"{path}[{index}]")
        item_id = _text(item.get("id"), f"{path}[{index}].id")
        if item_id in seen:
            raise ValidationError(f"{path}: duplicate ID {item_id!r}")
        seen.add(item_id)


def _evidence(value: Any, path: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        _text(value, path)
        return
    evidence = _object(value, path)
    _text(evidence.get("status"), f"{path}.status")
    if "note" in evidence and not isinstance(evidence["note"], str):
        raise ValidationError(f"{path}.note: expected text")


def _validate_device_actions(profile: dict, path: str) -> None:
    capabilities = _list(profile.get("device_actions", []), f"{path}.device_actions")
    _unique(capabilities, f"{path}.device_actions")
    for capability in capabilities:
        action_id = capability["id"]
        action_path = f"{path}.device_actions.{action_id}"
        _text(capability.get("label"), f"{action_path}.label")
        _evidence(capability.get("evidence"), f"{action_path}.evidence")
        if "experimental" in capability and not isinstance(capability["experimental"], bool):
            raise ValidationError(f"{action_path}.experimental: expected true/false")
        for index, warning in enumerate(_list(capability.get("warnings", []), f"{action_path}.warnings")):
            _text(warning, f"{action_path}.warnings[{index}]")
        if action_id == RELOAD_CURRENT_PRESET:
            if profile["protocol"] != "tank_v09":
                raise ValidationError(f"{action_path}: Reload current preset is only available for tank_v09 profiles")
            if capability.get("experimental") is not True:
                raise ValidationError(f"{action_path}: Reload current preset must be marked experimental")
            evidence = capability.get("evidence")
            status = evidence.get("status") if isinstance(evidence, dict) else evidence
            if status != "firmware_static_only":
                raise ValidationError(f"{action_path}.evidence: Reload current preset requires firmware_static_only evidence")
        elif profile["protocol"] in PROTOCOLS:
            raise ValidationError(f"{action_path}: unsupported device action")


def validate_profile(profile: dict) -> dict:
    """Validate a plugin without modifying it; unknown metadata is retained."""
    _object(profile, "profile")
    if profile.get("schema_version") != SCHEMA_VERSION or isinstance(profile.get("schema_version"), bool):
        raise ValidationError("profile.schema_version: only version 1 is supported")
    profile_id = _text(profile.get("id"), "profile.id")
    _text(profile.get("name"), f"{profile_id}.name")
    protocol = profile.get("protocol")
    if protocol not in PROTOCOL_ENCODERS:
        raise ValidationError(f"{profile_id}.protocol: supported protocols: {', '.join(PROTOCOL_ENCODERS)}")
    _evidence(profile.get("evidence"), f"{profile_id}.evidence")
    if "channel" in profile:
        _integer(profile["channel"], f"{profile_id}.channel", 1, 16)
    _validate_device_actions(profile, profile_id)
    if protocol not in PROTOCOLS:
        validator = PROFILE_VALIDATORS.get(protocol)
        if validator is not None:
            validator(profile)
        return profile
    blocks = _list(profile.get("blocks"), f"{profile_id}.blocks")
    if not blocks:
        raise ValidationError(f"{profile_id}.blocks: add at least one block")
    _unique(blocks, f"{profile_id}.blocks")
    wire_ids = set()
    for block in blocks:
        path = f"{profile_id}.{block['id']}"
        _text(block.get("label"), f"{path}.label")
        _evidence(block.get("evidence"), f"{path}.evidence")
        if protocol == "tank_v09":
            wire_id = _integer(block.get("wire_id"), f"{path}.wire_id", 0, 5)
            if wire_id in wire_ids:
                raise ValidationError(f"{path}.wire_id: duplicate TANK block")
            wire_ids.add(wire_id)
        elif "enable" in block:
            enable = _object(block["enable"], f"{path}.enable")
            _integer(enable.get("cc"), f"{path}.enable.cc", 0, 127)
            _integer(enable.get("on", 127), f"{path}.enable.on", 0, 127)
            _integer(enable.get("off", 0), f"{path}.enable.off", 0, 127)
            _evidence(enable.get("evidence"), f"{path}.enable.evidence")
        models = _list(block.get("models", []), f"{path}.models")
        _unique(models, f"{path}.models")
        for model in models:
            model_path = f"{path}.{model['id']}"
            _text(model.get("label"), f"{model_path}.label")
            _evidence(model.get("evidence"), f"{model_path}.evidence")
            if protocol == "tank_v09":
                _integer(model.get("wire_value"), f"{model_path}.wire_value", 0, 255)
            elif "cc" in model:
                _integer(model["cc"], f"{model_path}.cc", 0, 127)
                _integer(model.get("wire_value"), f"{model_path}.wire_value", 0, 127)
            parameters = _list(model.get("parameters", []), f"{model_path}.parameters")
            _unique(parameters, f"{model_path}.parameters")
            parameter_indices = set()
            for parameter in parameters:
                param_path = f"{model_path}.{parameter['id']}"
                _text(parameter.get("label"), f"{param_path}.label")
                max_value = 100 if protocol == "tank_v09" else 127
                low = _integer(parameter.get("min", 0), f"{param_path}.min", 0, max_value)
                high = _integer(parameter.get("max", max_value), f"{param_path}.max", low, max_value)
                _integer(parameter.get("step", 1), f"{param_path}.step", 1, max(1, high - low))
                _evidence(parameter.get("evidence"), f"{param_path}.evidence")
                if protocol == "tank_v09":
                    index = _integer(parameter.get("index"), f"{param_path}.index", 0, 5)
                    if index in parameter_indices:
                        raise ValidationError(f"{param_path}.index: duplicate parameter index")
                    parameter_indices.add(index)
                else:
                    _integer(parameter.get("cc"), f"{param_path}.cc", 0, 127)
    return profile


def load_profiles(directory: str | Path) -> dict[str, dict]:
    """Read .json profiles in deterministic order, rejecting duplicate IDs."""
    profiles: dict[str, dict] = {}
    directory = Path(directory)
    if not directory.is_dir():
        raise ValidationError(f"Profile directory does not exist: {directory}")
    for path in sorted(directory.glob("*.json")):
        try:
            profile = validate_profile(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidationError(f"{path.name}: {exc}") from exc
        if profile["id"] in profiles:
            raise ValidationError(f"{path.name}: duplicate profile ID {profile['id']!r}")
        profiles[profile["id"]] = profile
    if not profiles:
        raise ValidationError(f"No JSON profiles in {directory}")
    return profiles


def pack7(data: bytes) -> bytes:
    """TANK bit-stream packing, including its final zero group at multiples of 7."""
    value = int.from_bytes(data, "little")
    return bytes((value >> shift) & 127 for shift in range(0, len(data) * 8 + 1, 7))


def unpack7(data: bytes) -> bytes:
    if any(byte > 127 for byte in data):
        raise ValidationError("TANK packet: SysEx data is not 7-bit")
    value = sum(byte << (7 * index) for index, byte in enumerate(data))
    try:
        return value.to_bytes(len(data) * 7 // 8, "little")
    except OverflowError as exc:
        raise ValidationError("TANK packet: trailing bits exceed the data size") from exc


def packet(opcode: int, payload: bytes = b"") -> bytes:
    _integer(opcode, "opcode", 0, 255)
    raw = b"\x00\x59" + bytes([opcode]) + len(payload).to_bytes(3, "little")
    raw += payload + bytes([255 - (sum(payload) & 255)])
    return b"\xf0" + pack7(raw) + b"\xf7"


def parse_packet(data: bytes) -> dict:
    """Decode and checksum a complete TANK SysEx (also useful to audit exports)."""
    if not data.startswith(b"\xf0") or not data.endswith(b"\xf7"):
        raise ValidationError("TANK packet: incomplete SysEx")
    raw = unpack7(data[1:-1])
    if len(raw) < 7 or raw[:2] != b"\x00\x59":
        raise ValidationError("TANK packet: invalid header")
    length = int.from_bytes(raw[3:6], "little")
    if len(raw) < length + 7:
        raise ValidationError("TANK packet: incomplete data")
    payload = raw[6:6 + length]
    if raw[6 + length] != 255 - (sum(payload) & 255):
        raise ValidationError("TANK packet: invalid checksum")
    if any(raw[length + 7:]):
        raise ValidationError("TANK packet: unexpected trailing data")
    return {"opcode": raw[2], "payload": payload, "raw": raw[:length + 7]}


def tank_live_write(offset: int, data: bytes) -> bytes:
    _integer(offset, "live patch offset", 0, 91)
    if not data or offset + len(data) > 92:
        raise ValidationError("Write exceeds the 92-byte TANK live patch")
    query = b"\x04" + (0x10000000 + offset).to_bytes(4, "little")
    query += len(data).to_bytes(3, "little") + data
    return packet(0x22, query)


def format_hex(message: bytes) -> str:
    return message.hex(" ").upper()


def _deduplicate(values) -> tuple:
    return tuple(dict.fromkeys(values))


def _status(*objects: dict) -> str:
    for obj in reversed(objects):
        evidence = obj.get("evidence")
        if isinstance(evidence, str):
            return evidence
        if isinstance(evidence, dict) and evidence.get("status"):
            return evidence["status"]
    return "unverified"


def _combined_status(statuses) -> str:
    unique = _deduplicate(statuses)
    if not unique:
        return "not_applicable"
    return unique[0] if len(unique) == 1 else "mixed"


@dataclass(frozen=True)
class CompiledAction:
    messages: tuple[bytes, ...]
    summary: str
    evidence_status: str
    warnings: tuple[str, ...] = ()

    @property
    def hex(self) -> str:
        return "\n".join(format_hex(message) for message in self.messages)

    @property
    def byte_count(self) -> int:
        return sum(map(len, self.messages))

    def to_dict(self) -> dict:
        return {"messages_hex": [format_hex(msg) for msg in self.messages],
                "summary": self.summary, "evidence_status": self.evidence_status,
                "warnings": list(self.warnings), "byte_count": self.byte_count}


@dataclass(frozen=True)
class CompiledState:
    messages: tuple[bytes, ...]
    sysex_hex: str
    cc_rows: tuple[dict, ...]
    summaries: tuple[str, ...]
    evidence_status: str
    warnings: tuple[str, ...] = ()

    @property
    def hex(self) -> str:
        return "\n".join(format_hex(message) for message in self.messages)

    @property
    def summary(self) -> str:
        return "; ".join(self.summaries)

    @property
    def byte_count(self) -> int:
        return sum(map(len, self.messages))

    def to_dict(self) -> dict:
        return {"messages_hex": [format_hex(msg) for msg in self.messages],
                "sysex_hex": self.sysex_hex, "cc_rows": list(self.cc_rows),
                "summaries": list(self.summaries), "summary": self.summary,
                "evidence_status": self.evidence_status,
                "warnings": list(self.warnings), "byte_count": self.byte_count}


def _find(items: list, item_id: Any, path: str) -> dict:
    _text(item_id, path)
    for item in items:
        if item["id"] == item_id:
            return item
    raise ValidationError(f"{path}: unknown ID {item_id!r}")


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValidationError("enable.value: use true/false or 1/0")


def _compile_reload_current_preset(action: dict, profile: dict) -> CompiledAction:
    if profile["protocol"] != "tank_v09":
        raise ValidationError("Reload current preset is only available for tank_v09 profiles")
    capability = next((item for item in profile.get("device_actions", [])
                       if item["id"] == RELOAD_CURRENT_PRESET), None)
    if capability is None:
        raise ValidationError("Reload current preset is unavailable in this profile; the device capability must be declared explicitly")
    # This action has no targets or tunable wire fields. Cosmetic metadata can
    # accompany it, but old effect assignments and raw write options are errors.
    forbidden = {"block_id", "model_id", "param_id", "value", "preset", "preset_id",
                 "preset_index", "preset_number", "index", "address", "offset",
                 "memory_type", "opcode", "length", "data"}
    extra = sorted(forbidden.intersection(action))
    if extra:
        raise ValidationError(f"Reload current preset does not accept target, value or address fields: {', '.join(extra)}")
    _integer(action.get("channel", profile.get("channel", 1)), "action.channel", 1, 16)
    # Fixed stock V09 operation: invalidate the cached previous preset index.
    # The control loop then reloads the saved record for the unchanged current
    # index. No arbitrary address or preset number is accepted from JSON.
    payload = b"\x04" + (0x20000001).to_bytes(4, "little") + b"\x01\x00\x00\xff"
    warnings = [
        "Experimental: supported by static analysis of stock TANK Mini firmware V09; not yet verified on the physical TANK Mini or through the Chocolate Plus.",
        "Restores the whole saved preset, including other effects' ON/OFF states, and discards all temporary edits.",
        "Designed for stock firmware V09 in normal mode; tuner or special modes may defer the reload.",
        "Does not save or overwrite any preset.",
    ]
    warnings.extend(capability.get("warnings", []))
    summary = f"{profile['name']}: {capability['label']} (experimental)"
    return CompiledAction((packet(0x22, payload),), summary, "firmware_static_only", _deduplicate(warnings))


def _compile_builtin_action(action: dict, profile: dict) -> CompiledAction:
    action_type = action.get("type")
    if action_type == RELOAD_CURRENT_PRESET:
        return _compile_reload_current_preset(action, profile)
    if action_type not in ("enable", "model", "parameter"):
        raise ValidationError("action.type: use enable, model, parameter or a supported device action")
    block = _find(profile["blocks"], action.get("block_id"), "action.block_id")
    channel = _integer(action.get("channel", profile.get("channel", 1)), "action.channel", 1, 16)
    tank = profile["protocol"] == "tank_v09"
    warnings: list[str] = []
    model: dict = {}
    parameter: dict = {}
    if action_type != "enable" or "model_id" in action:
        model = _find(block.get("models", []), action.get("model_id"), "action.model_id")
    prefix = f"{profile['name']} · {block['label']}"
    if action_type == "enable":
        value = _enabled(action.get("value"))
        summary = f"{prefix}: {'ON' if value else 'OFF'}"
        if tank:
            message = tank_live_write(8 + block["wire_id"], bytes([int(value)]))
            evidence_objects = (profile, block)
            if model:
                warnings.append(f"The preset must already have {block['label']} = {model['label']} selected; this message only changes ON/OFF.")
        else:
            enable = block.get("enable")
            if enable is None:
                raise ValidationError(f"{prefix}: ON/OFF command is unavailable in this profile")
            value = enable.get("on", 127) if value else enable.get("off", 0)
            message = bytes([0xB0 + channel - 1, enable["cc"], value])
            evidence_objects = (profile, block, enable)
    elif action_type == "model":
        summary = f"{prefix}: model {model['label']}"
        if tank:
            message = tank_live_write(14 + block["wire_id"], bytes([model["wire_value"]]))
            warnings.append("Changing the TANK model may load default values asynchronously; set parameters in a separate state after the change.")
        else:
            if "cc" not in model:
                raise ValidationError(f"{prefix}: MIDI model selection is unavailable in this profile")
            message = bytes([0xB0 + channel - 1, model["cc"], model["wire_value"]])
        evidence_objects = (profile, block, model)
    else:
        parameter = _find(model.get("parameters", []), action.get("param_id"), "action.param_id")
        low = parameter.get("min", 0)
        high = parameter.get("max", 100 if tank else 127)
        value = _integer(action.get("value"), "action.value", low, high)
        if (value - low) % parameter.get("step", 1):
            raise ValidationError(f"action.value: use a step of {parameter.get('step', 1)} starting at {low}")
        summary = f"{prefix} / {model['label']} / {parameter['label']} = {value}"
        if tank:
            offset = 20 + 12 * block["wire_id"] + 2 * parameter["index"]
            message = tank_live_write(offset, value.to_bytes(2, "little"))
            warnings.append(f"This parameter uses {block['label']} = {model['label']} already selected in the preset; this message does not change the model.")
        else:
            message = bytes([0xB0 + channel - 1, parameter["cc"], value])
        evidence_objects = (profile, block, model, parameter)
    status = _status(*evidence_objects)
    if status in ("unverified", "unknown", "pending"):
        warnings.append("Command has not yet been verified on the device: check the profile and test the values on your firmware.")
    for obj in evidence_objects:
        for warning in obj.get("warnings", []):
            if isinstance(warning, str):
                warnings.append(warning)
    return CompiledAction((message,), summary, status, _deduplicate(warnings))


def register_protocol(name: str, encoder: Callable[[dict, dict], CompiledAction],
                      validator: Callable[[dict], None] | None = None, *, replace: bool = False) -> None:
    """Register trusted Python code explicitly; JSON profiles never call this.

    An encoder accepts (action, validated_profile) and returns CompiledAction.
    A new protocol can therefore serve the existing visual editor without
    changing the editor. Built-in profiles use the shared declarative schema;
    custom protocols supply their own additional validation when necessary.
    """
    _text(name, "protocol")
    if not name.isidentifier() or not callable(encoder):
        raise ValidationError("The protocol requires a valid name and an encoder function")
    if name in PROTOCOL_ENCODERS and not replace:
        raise ValidationError(f"Protocol is already registered: {name}")
    if validator is not None and not callable(validator):
        raise ValidationError("The protocol validator must be a function")
    PROTOCOL_ENCODERS[name] = encoder
    if validator is not None:
        PROFILE_VALIDATORS[name] = validator
    else:
        PROFILE_VALIDATORS.pop(name, None)


def _compile_raw_midi(action: dict) -> CompiledAction:
    """Preserve one imported CC/SysEx message without inventing device metadata."""
    forbidden = {"device_id", "block_id", "model_id", "param_id", "channel", "value"}
    extra = sorted(forbidden.intersection(action))
    if extra:
        raise ValidationError(f"raw_midi does not accept device, effect or value fields: {', '.join(extra)}")
    message_hex = _text(action.get("message_hex"), "raw_midi.message_hex")
    try:
        message = bytes.fromhex(message_hex)
    except ValueError as exc:
        raise ValidationError("raw_midi.message_hex: expected hexadecimal MIDI bytes") from exc
    if len(message) == 3 and 0xB0 <= message[0] <= 0xBF and all(byte <= 127 for byte in message[1:]):
        channel = (message[0] & 15) + 1
        summary = f"Imported MIDI: Channel {channel} / CC {message[1]} = {message[2]}"
        return CompiledAction((message,), summary, "controller_read")
    if len(message) >= 2 and message[0] == 0xF0 and message[-1] == 0xF7 and all(byte <= 127 for byte in message[1:-1]):
        if len(message) > MAX_SYSEX_BYTES:
            raise ValidationError(f"raw_midi: SysEx contains {len(message)} bytes; the maximum is {MAX_SYSEX_BYTES}")
        summary = f"Imported MIDI: SysEx ({len(message)} bytes)"
        warning = "The target device and effect model cannot be inferred from imported SysEx; the original MIDI bytes are preserved."
        return CompiledAction((message,), summary, "controller_read", (warning,))
    raise ValidationError("raw_midi.message_hex: use one complete 3-byte CC or F0...F7 SysEx message with 7-bit data")


def compile_action(action: dict, profiles: Mapping[str, dict]) -> CompiledAction:
    """Compile a semantic assignment or preserve one imported raw MIDI message."""
    _object(action, "action")
    if action.get("type") == "raw_midi":
        return _compile_raw_midi(action)
    device_id = _text(action.get("device_id"), "action.device_id")
    if device_id not in profiles:
        raise ValidationError(f"Unknown device: {device_id!r}")
    profile = validate_profile(profiles[device_id])
    result = PROTOCOL_ENCODERS[profile["protocol"]](action, profile)
    if not isinstance(result, CompiledAction):
        raise ValidationError("The encoder must return CompiledAction")
    for message in result.messages:
        if not isinstance(message, bytes):
            raise ValidationError("The encoder must return messages as bytes")
        sysex = len(message) >= 2 and message[0] == 0xF0 and message[-1] == 0xF7
        if sysex and all(byte <= 127 for byte in message[1:-1]):
            continue
        if len(message) == 3 and (message[0] & 0xF0) == 0xB0 and all(byte <= 127 for byte in message[1:]):
            continue
        raise ValidationError("The encoder generated an invalid MIDI SysEx/CC message")
    return result


register_protocol("tank_v09", _compile_builtin_action)
register_protocol("midi_cc", _compile_builtin_action)


def compile_state(state: dict, profiles: Mapping[str, dict], max_sysex_bytes: int = MAX_SYSEX_BYTES) -> CompiledState:
    """Build a MidiSuite bank; CC rows and SysEx cannot share a bank here."""
    _object(state, "state")
    _integer(max_sysex_bytes, "SysEx limit", 1, MAX_SYSEX_BYTES)
    actions = _list(state.get("actions", []), "state.actions")
    if len(actions) > 128:
        raise ValidationError("state.actions: maximum of 128 actions")
    if len(actions) != 1 and any(isinstance(action, dict) and action.get("type") == RELOAD_CURRENT_PRESET
                                 for action in actions):
        raise ValidationError("TANK: Reload current preset must be the only action in a state because the reload is asynchronous. Put other commands in a separate state.")
    compiled = [compile_action(action, profiles) for action in actions]
    messages = tuple(message for action in compiled for message in action.messages)
    sysex_messages = [msg for msg in messages if msg.startswith(b"\xf0")]
    cc_messages = [msg for msg in messages if len(msg) == 3 and (msg[0] & 0xF0) == 0xB0]
    if sysex_messages and cc_messages:
        raise ValidationError("MidiSuite: a bank cannot mix CC and SysEx. Separate the commands or use explicit ON/OFF SysEx for TANK.")
    sysex_bytes = sum(map(len, sysex_messages))
    if sysex_bytes > max_sysex_bytes:
        raise ValidationError(f"MidiSuite: the bank contains {sysex_bytes} SysEx bytes; the limit is {max_sysex_bytes}. Split the actions across multiple states or switches.")
    warnings = [warning for action in compiled for warning in action.warnings]
    if len(sysex_messages) > 1:
        warnings.append("Sending multiple consecutive SysEx messages in the same Chocolate bank: this sequence requires testing on the physical controller.")
    # A model write followed by parameters can race the firmware's asynchronous load.
    for index, action in enumerate(actions):
        if action.get("type") != "model" or profiles[action["device_id"]]["protocol"] != "tank_v09":
            continue
        if any(later.get("device_id") == action["device_id"] and later.get("block_id") == action["block_id"]
               and later.get("type") == "parameter" for later in actions[index + 1:]):
            raise ValidationError("TANK: changing the model and parameters of the same block in one bank may overlap with asynchronous loading. Select the model in the preset or use a separate state.")
    cc_rows = tuple({"channel": (message[0] & 15) + 1, "type": "CC", "data1": message[1],
                     "data2": message[2], "hex": format_hex(message)} for message in cc_messages)
    return CompiledState(messages, "\n".join(format_hex(msg) for msg in sysex_messages), cc_rows,
                         tuple(action.summary for action in compiled),
                         _combined_status(action.evidence_status for action in compiled),
                         _deduplicate(warnings))


def validate_config(config: dict, profiles: Mapping[str, dict]) -> dict:
    """Validate semantic project and each bank's controller constraints."""
    _object(config, "project")
    if config.get("schema_version") != SCHEMA_VERSION or isinstance(config.get("schema_version"), bool):
        raise ValidationError("project.schema_version: only version 1 is supported")
    _text(config.get("name"), "project.name")
    controller = _object(config.get("controller", {"id": "chocolate_plus"}), "project.controller")
    if controller.get("id") != "chocolate_plus":
        raise ValidationError("project.controller.id: chocolate_plus is supported")
    max_bytes = _integer(controller.get("max_sysex_bytes", MAX_SYSEX_BYTES), "controller.max_sysex_bytes", 1, MAX_SYSEX_BYTES)
    pages = _list(config.get("pages"), "project.pages")
    if not 1 <= len(pages) <= MAX_PAGES:
        raise ValidationError(f"project.pages: use 1 to {MAX_PAGES} pages")
    for page_index, page in enumerate(pages):
        path = f"page {page_index + 1}"
        _object(page, path)
        _text(page.get("name"), f"{path}.name")
        switches = _object(page.get("switches"), f"{path}.switches")
        if any(key not in SWITCHES for key in switches):
            raise ValidationError(f"{path}: available switches are A, B, C and D")
        for switch_id, switch in switches.items():
            switch_path = f"{path} / switch {switch_id}"
            _object(switch, switch_path)
            mode = switch.get("mode")
            if mode not in ("single", "alternating"):
                raise ValidationError(f"{switch_path}: mode must be single or alternating")
            states = _list(switch.get("states"), f"{switch_path}.states")
            expected = 1 if mode == "single" else 2
            if len(states) != expected:
                raise ValidationError(f"{switch_path}: {mode} mode requires {expected} states")
            for state_index, state in enumerate(states):
                try:
                    compile_state(state, profiles, max_bytes)
                except ValidationError as exc:
                    raise ValidationError(f"{switch_path} / state {state_index + 1}: {exc}") from exc
    return config


def compile_config(config: dict, profiles: Mapping[str, dict]) -> dict:
    validate_config(config, profiles)
    limit = config.get("controller", {}).get("max_sysex_bytes", MAX_SYSEX_BYTES)
    pages = []
    for page in config["pages"]:
        switches = {}
        for switch_id in SWITCHES:
            switch = page["switches"].get(switch_id)
            if switch is None:
                continue
            states = []
            for state_index, state in enumerate(switch["states"]):
                result = compile_state(state, profiles, limit).to_dict()
                result["label"] = state.get("label", f"State {state_index + 1}")
                states.append(result)
            switches[switch_id] = {"label": switch.get("label", switch_id),
                                  "mode": switch["mode"], "states": states}
        pages.append({"name": page["name"], "switches": switches})
    return {"schema_version": SCHEMA_VERSION, "name": config["name"],
            "controller": config.get("controller", {"id": "chocolate_plus"}),
            "pages": pages}


def load_config(path: str | Path, profiles: Mapping[str, dict]) -> dict:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Unable to read the project: {exc}") from exc
    return validate_config(config, profiles)


def save_config(path: str | Path, config: dict, profiles: Mapping[str, dict]) -> None:
    validate_config(config, profiles)
    Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
