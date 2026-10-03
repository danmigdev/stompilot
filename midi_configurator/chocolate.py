"""Chocolate Plus firmware 16 configuration codec; this module performs no I/O.

The 23,646-byte layout is derived from MidiSuite's FcpModel and confirmed
against a complete read of a FootCtrlPlus_016. Always edit a fresh device
image so settings that Stompilot does not expose remain intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

try:
    from .core import SWITCHES, ValidationError, compile_config, validate_config
except ImportError:
    from core import SWITCHES, ValidationError, compile_config, validate_config


CONFIG_SIZE = 23646
MAX_DEVICE_PAGES = 8
ADVANCED_MODE = 3
ADVANCED_OFFSET = 93
SWITCH_SIZE = 417
MIDI_ROWS = 16
ROW_SIZE = 5
SYSEX_SIZE = 128
BANK_A_ROWS = 1
BANK_B_ROWS = 81
BANK_A_SYSEX = 161
BANK_B_SYSEX = 289
PAGE_COUNT_OFFSET = 23639
CURRENT_PAGE_OFFSET = 23640

STEP_MODES = {0: "single", 1: "alternating", 2: "press/release",
              3: "long on", 4: "short/long press"}


@dataclass(frozen=True)
class ChangedRange:
    offset: int
    before: bytes
    after: bytes

    @property
    def length(self) -> int:
        return len(self.after)


@dataclass(frozen=True)
class WritePlan:
    image: bytes
    summaries: list[str]
    warnings: list[str]
    ranges: tuple[ChangedRange, ...]

    @property
    def changed_bytes(self) -> int:
        return sum(item.length for item in self.ranges)


def _image(image: bytes) -> bytes:
    if not isinstance(image, (bytes, bytearray)) or len(image) != CONFIG_SIZE:
        raise ValidationError(f"Chocolate Plus configuration must contain exactly {CONFIG_SIZE} bytes")
    return bytes(image)


def switch_offset(page_index: int, switch_id: str) -> int:
    """Return an advanced switch offset; page_index uses zero-based indexing."""
    if isinstance(page_index, bool) or not isinstance(page_index, int) or not 0 <= page_index < MAX_DEVICE_PAGES:
        raise ValidationError("Chocolate Plus page index must be from 0 to 7")
    if switch_id not in SWITCHES:
        raise ValidationError("Chocolate Plus switch must be A, B, C or D")
    return ADVANCED_OFFSET + (page_index * len(SWITCHES) + SWITCHES.index(switch_id)) * SWITCH_SIZE


def changed_ranges(before: bytes, after: bytes) -> tuple[ChangedRange, ...]:
    """Find minimal contiguous changed spans without including unchanged bytes."""
    before, after = _image(before), _image(after)
    ranges = []
    start = None
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new and start is None:
            start = index
        elif old == new and start is not None:
            ranges.append(ChangedRange(start, before[start:index], after[start:index]))
            start = None
    if start is not None:
        ranges.append(ChangedRange(start, before[start:], after[start:]))
    return tuple(ranges)


def _bank_summary(block: bytes, row_offset: int, sysex_offset: int) -> str:
    rows = [block[row_offset + index * ROW_SIZE:row_offset + (index + 1) * ROW_SIZE]
            for index in range(MIDI_ROWS)]
    enabled = [row for row in rows if row[0]]
    if not enabled:
        return "empty"
    if any(row[2] == 4 for row in enabled):
        data = block[sysex_offset:sysex_offset + SYSEX_SIZE]
        end = data.rfind(b"\xf7")
        if end < 0:
            return "SysEx (no complete message)"
        return f"SysEx, {end + 1} bytes, {data[:end + 1].count(bytes([0xF0]))} message(s)"
    cc_count = sum(row[2] == 1 for row in enabled)
    if cc_count == len(enabled):
        values = "; ".join(f"ch {row[1] + 1} CC {row[3]} = {row[4]}" for row in enabled)
        return f"{cc_count} CC row(s): {values}"
    return f"{len(enabled)} MIDI row(s)"


def decode_config(image: bytes) -> dict:
    """Inspect native advanced banks without interpreting target-device parameters."""
    image = _image(image)
    pages = []
    for page_index in range(MAX_DEVICE_PAGES):
        switches = {}
        for switch_id in SWITCHES:
            offset = switch_offset(page_index, switch_id)
            block = image[offset:offset + SWITCH_SIZE]
            switches[switch_id] = {
                "step_mode": block[0], "mode": STEP_MODES.get(block[0], f"unknown ({block[0]})"),
                "bank_a": _bank_summary(block, BANK_A_ROWS, BANK_A_SYSEX),
                "bank_b": _bank_summary(block, BANK_B_ROWS, BANK_B_SYSEX),
            }
        pages.append({"number": page_index + 1, "switches": switches})
    return {"working_mode": image[0], "advanced_page_count": image[PAGE_COUNT_OFFSET] + 1,
            "current_advanced_page": image[CURRENT_PAGE_OFFSET] + 1, "pages": pages}


def config_summary(image: bytes) -> str:
    snapshot = decode_config(image)
    mode = snapshot["working_mode"]
    description = "Advanced Custom" if mode == ADVANCED_MODE else f"mode {mode}"
    count = snapshot["advanced_page_count"]
    lines = [f"Working mode: {description}",
             f"Advanced pages: {count}; current page: {snapshot['current_advanced_page']}"]
    for page in snapshot["pages"][:min(count, MAX_DEVICE_PAGES)]:
        lines.append(f"Page {page['number']}")
        for switch_id, switch in page["switches"].items():
            banks = f"A: {switch['bank_a']}"
            if switch["step_mode"] in (1, 2, 4):
                banks += f"; B: {switch['bank_b']}"
            lines.append(f"  {switch_id}: {switch['mode']}; {banks}")
    return "\n".join(lines)


def _decode_bank(rows: bytes, sysex: bytes, path: str) -> tuple[bytes, ...]:
    """Decode supported active rows without guessing a device or effect model."""
    if len(rows) != MIDI_ROWS * ROW_SIZE or len(sysex) != SYSEX_SIZE:
        raise ValidationError(f"{path}: native bank requires 80 row bytes and 128 SysEx bytes")
    enabled = []
    for index in range(MIDI_ROWS):
        row = rows[index * ROW_SIZE:(index + 1) * ROW_SIZE]
        if row[0] not in (0, 1):
            raise ValidationError(f"{path}, row {index + 1}: unsupported enabled flag {row[0]}")
        if not row[0]:
            continue
        if row[2] not in (1, 4):
            raise ValidationError(f"{path}, row {index + 1}: MIDI type {row[2]} cannot be imported for editing; only CC and SysEx are supported")
        if row[1] > 15:
            raise ValidationError(f"{path}, row {index + 1}: MIDI channel must be from 1 to 16")
        if row[2] == 1 and (row[3] > 127 or row[4] > 127):
            raise ValidationError(f"{path}, row {index + 1}: CC number and value must be from 0 to 127")
        enabled.append(row)
    if not enabled:
        return ()
    markers = sum(row[2] == 4 for row in enabled)
    if not markers:
        return tuple(bytes([0xB0 + row[1], row[3], row[4]]) for row in enabled)
    if markers != 1 or len(enabled) != 1:
        raise ValidationError(f"{path}: a SysEx bank must have exactly one enabled SysEx row and no other enabled MIDI rows")
    messages = []
    position = 0
    while position < SYSEX_SIZE:
        if sysex[position] == 0 and not any(sysex[position:]):
            break
        if sysex[position] != 0xF0:
            raise ValidationError(f"{path}: SysEx byte {position + 1} is not the start of a complete F0...F7 message; only zero padding may follow messages")
        end = sysex.find(b"\xf7", position + 1)
        if end < 0:
            raise ValidationError(f"{path}: SysEx message at byte {position + 1} has no F7 terminator")
        message = sysex[position:end + 1]
        if any(byte > 127 for byte in message[1:-1]):
            raise ValidationError(f"{path}: SysEx message at byte {position + 1} contains invalid 7-bit data")
        messages.append(message)
        position = end + 1
    if not messages:
        raise ValidationError(f"{path}: the enabled SysEx row has no complete message")
    return tuple(messages)


def _native_bank(value: dict, path: str) -> tuple[bytes, bytes]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: native_bank must be an object")
    result = []
    for field, length in (("rows_hex", MIDI_ROWS * ROW_SIZE), ("sysex_hex", SYSEX_SIZE)):
        text = value.get(field)
        if not isinstance(text, str):
            raise ValidationError(f"{path}: native_bank.{field} must be hexadecimal text")
        try:
            data = bytes.fromhex(text)
        except ValueError as exc:
            raise ValidationError(f"{path}: native_bank.{field} must be hexadecimal text") from exc
        if len(data) != length:
            raise ValidationError(f"{path}: native_bank.{field} must contain exactly {length} bytes")
        result.append(data)
    return result[0], result[1]


def import_controller_project(image: bytes, profiles: Mapping[str, dict]) -> dict:
    """Create an editable project from active, supported native advanced banks.

    Imported actions contain original MIDI bytes rather than inferred target
    devices or effect models. Each state also keeps its native bank bytes so
    unchanged commands retain disabled rows and unused SysEx storage.
    Unsupported modes or active MIDI types fail explicitly; callers can still
    inspect the image with config_summary without replacing its configuration.
    """
    image = _image(image)
    if image[0] != ADVANCED_MODE:
        raise ValidationError(f"Chocolate Plus working mode {image[0]} cannot be imported for editing: Advanced Custom mode ({ADVANCED_MODE}) is required. The configuration is available for read-only inspection.")
    if image[PAGE_COUNT_OFFSET] >= MAX_DEVICE_PAGES or image[CURRENT_PAGE_OFFSET] >= MAX_DEVICE_PAGES:
        raise ValidationError("Chocolate Plus advanced page settings do not match the supported firmware 16 layout")
    pages = []
    for page_index in range(image[PAGE_COUNT_OFFSET] + 1):
        switches = {}
        for switch_id in SWITCHES:
            offset = switch_offset(page_index, switch_id)
            block = image[offset:offset + SWITCH_SIZE]
            path = f"Page {page_index + 1}, switch {switch_id}"
            step_mode = block[0]
            if step_mode not in (0, 1):
                description = STEP_MODES.get(step_mode, f"unknown ({step_mode})")
                raise ValidationError(f"{path}: {description} step mode cannot be imported for editing; single and alternating modes are supported")
            states = []
            for bank_index in range(1 if step_mode == 0 else 2):
                row_offset = BANK_A_ROWS if bank_index == 0 else BANK_B_ROWS
                sysex_offset = BANK_A_SYSEX if bank_index == 0 else BANK_B_SYSEX
                rows = block[row_offset:row_offset + MIDI_ROWS * ROW_SIZE]
                sysex = block[sysex_offset:sysex_offset + SYSEX_SIZE]
                bank_name = f"Bank {'AB'[bank_index]}"
                messages = _decode_bank(rows, sysex, f"{path}, {bank_name.lower()}")
                states.append({"label": bank_name,
                               "actions": [{"type": "raw_midi", "message_hex": message.hex(" ").upper()}
                                           for message in messages],
                               "native_bank": {"rows_hex": rows.hex(" ").upper(),
                                               "sysex_hex": sysex.hex(" ").upper()}})
            switches[switch_id] = {"label": f"Switch {switch_id}",
                                   "mode": STEP_MODES[step_mode], "states": states}
        pages.append({"name": f"Page {page_index + 1}", "switches": switches})
    project = {"schema_version": 1, "name": "Chocolate Plus configuration",
               "controller": {"id": "chocolate_plus", "max_sysex_bytes": SYSEX_SIZE},
               "pages": pages}
    return validate_config(project, profiles)


def _encode_bank(state: dict, path: str, source_state: dict) -> tuple[bytes, bytes]:
    if "native_bank" in source_state:
        native_rows, native_sysex = _native_bank(source_state["native_bank"], path)
        native_messages = _decode_bank(native_rows, native_sysex, path)
        messages = tuple(bytes.fromhex(message) for message in state["messages_hex"])
        if messages == native_messages:
            return native_rows, native_sysex
    rows = bytearray(MIDI_ROWS * ROW_SIZE)
    sysex = bytearray(SYSEX_SIZE)
    cc_rows = state["cc_rows"]
    if len(cc_rows) > MIDI_ROWS:
        raise ValidationError(f"{path}: Chocolate Plus supports at most {MIDI_ROWS} CC rows per bank")
    if cc_rows:
        for index, row in enumerate(cc_rows):
            start = index * ROW_SIZE
            rows[start:start + ROW_SIZE] = bytes([1, row["channel"] - 1, 1, row["data1"], row["data2"]])
    elif state["messages_hex"]:
        data = b"".join(bytes.fromhex(message) for message in state["messages_hex"])
        if len(data) > SYSEX_SIZE:
            raise ValidationError(f"{path}: Chocolate Plus SysEx bank exceeds {SYSEX_SIZE} bytes")
        rows[:ROW_SIZE] = b"\x01\x00\x04\x00\x00"
        sysex[:len(data)] = data
    return bytes(rows), bytes(sysex)


def plan_project(project: dict, profiles: Mapping[str, dict], original_image: bytes) -> WritePlan:
    """Replace selected advanced switches, then activate the project's pages.

    Single mode uses Bank A and preserves the inactive Bank B. Alternating
    mode replaces both banks. A switch has one shared step mode, so changing
    its mode also replaces any previous press/release or long-press behavior.
    Unassigned switches, unused pages and all other configuration regions
    remain identical to the original image.
    """
    original = _image(original_image)
    if original[0] > 12:
        raise ValidationError(f"Unsupported Chocolate Plus working mode: {original[0]}")
    if original[PAGE_COUNT_OFFSET] >= MAX_DEVICE_PAGES or original[CURRENT_PAGE_OFFSET] >= MAX_DEVICE_PAGES:
        raise ValidationError("Chocolate Plus advanced page settings do not match the supported firmware 16 layout")
    compiled = compile_config(project, profiles)
    pages = compiled["pages"]
    if len(pages) > MAX_DEVICE_PAGES:
        raise ValidationError(f"Direct Chocolate Plus configuration supports at most {MAX_DEVICE_PAGES} advanced pages")
    result = bytearray(original)
    summaries, warnings = [], []
    for page_index, page in enumerate(pages):
        for switch_id, switch in page["switches"].items():
            offset = switch_offset(page_index, switch_id)
            previous_mode = original[offset]
            if previous_mode not in STEP_MODES:
                raise ValidationError(f"Page {page_index + 1}, switch {switch_id}: unsupported native step mode {previous_mode}")
            path = f"Page {page_index + 1}, switch {switch_id}"
            if previous_mode in (2, 3, 4):
                warnings.append(f"{path}: existing {STEP_MODES[previous_mode]} behavior will be replaced by {switch['mode']} mode.")
            result[offset] = 0 if switch["mode"] == "single" else 1
            summaries.append(f"{path} ({switch['label']}): {switch['mode']}, {len(switch['states'])} bank(s)")
            for state_index, state in enumerate(switch["states"]):
                source_state = project["pages"][page_index]["switches"][switch_id]["states"][state_index]
                rows, sysex = _encode_bank(state, f"{path}, bank {'AB'[state_index]}", source_state)
                row_offset = BANK_A_ROWS if state_index == 0 else BANK_B_ROWS
                sysex_offset = BANK_A_SYSEX if state_index == 0 else BANK_B_SYSEX
                result[offset + row_offset:offset + row_offset + len(rows)] = rows
                result[offset + sysex_offset:offset + sysex_offset + len(sysex)] = sysex
                warnings.extend(state["warnings"])
                cc_count = len(state["cc_rows"])
                count = len(state["messages_hex"])
                details = f"{cc_count} CC row(s)" if cc_count else f"{state['byte_count']} SysEx bytes, {count} message(s)"
                if count == 0:
                    details = "empty"
                assignments = "; ".join(state["summaries"])
                line = f"  Bank {'AB'[state_index]} ({state['label']}): {details}"
                if assignments:
                    line += f"; {assignments}"
                summaries.append(line)
    result[0] = ADVANCED_MODE
    result[PAGE_COUNT_OFFSET] = len(pages) - 1
    result[CURRENT_PAGE_OFFSET] = 0
    summaries.append(f"Activate Advanced Custom mode with {len(pages)} page(s), starting on page 1")
    image = bytes(result)
    return WritePlan(image, summaries, list(dict.fromkeys(warnings)), changed_ranges(original, image))
