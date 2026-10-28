"""Chocolate Plus configuration codec for firmware 12 to 16; this module performs no I/O.

Firmware versions have different persistence behavior. The 23,646-byte layout is derived from MidiSuite's
FcpModel and confirmed against complete reads of a FootCtrlPlus_016 and a
FootCtrlPlus_013. Always edit a fresh device image so settings that Stompilot
does not expose remain intact.

Each bank's 128-byte SysEx area holds a length byte n, then n bytes that the
firmware sends exactly as stored, so they must be complete F0...F7 messages.
The firmware does not add F0. Earlier Stompilot releases stored the messages
from byte 0; F0 is never a valid length, so the firmware sent nothing for
those banks. They are still decoded so that a write can repair them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

try:
    from .core import SWITCHES, ValidationError, compile_config, decode_message, describe_decoded
except ImportError:
    from core import SWITCHES, ValidationError, compile_config, decode_message, describe_decoded


CONFIG_SIZE = 23646
MAX_DEVICE_PAGES = 8
ADVANCED_MODE = 3
ADVANCED_OFFSET = 93
SWITCH_SIZE = 417
MIDI_ROWS = 16
ROW_SIZE = 5
SYSEX_SIZE = 128
SYSEX_LIMIT = SYSEX_SIZE - 1
SYSEX_ROW = b"\x01\x00\x04\x00\x00"
# A row's MIDI type byte. MidiSuite fills unused rows with Program Change 0.
ROW_PC, ROW_CC, ROW_SYSEX = 0, 1, 4
ROW_TYPES = {"PC": ROW_PC, "CC": ROW_CC}
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
        sysex = block[sysex_offset:sysex_offset + SYSEX_SIZE]
        if _legacy_sysex(sysex):
            return "SysEx in the old Stompilot format; the controller sends nothing"
        length = sysex[0]
        if length > SYSEX_LIMIT:
            return f"SysEx (invalid length byte {length})"
        try:
            messages = _sysex_frames(sysex[1:1 + length], "SysEx")
        except ValidationError:
            return f"SysEx, {length} bytes, not complete F0...F7 messages"
        if not messages:
            return "SysEx (no complete message)"
        return f"SysEx, {length} bytes, {len(messages)} message(s)"
    if any(row[2] not in (ROW_PC, ROW_CC) for row in enabled):
        return f"{len(enabled)} MIDI row(s)"
    values = "; ".join(f"ch {row[1] + 1} CC {row[3]} = {row[4]}" if row[2] == ROW_CC else f"ch {row[1] + 1} PC {row[3]}"
                       for row in enabled)
    return f"{_rows_label(len(enabled), {row[2] for row in enabled})}: {values}"


def _rows_label(count: int, types: set) -> str:
    kind = "CC" if types == {ROW_CC} else "Program Change" if types == {ROW_PC} else "MIDI"
    return f"{count} {kind} row(s)"


def _active_banks(step_mode: int) -> tuple[tuple[str, int, int], ...]:
    """Return the banks a step mode uses, with their row and SysEx offsets."""
    banks = (("A", BANK_A_ROWS, BANK_A_SYSEX), ("B", BANK_B_ROWS, BANK_B_SYSEX))
    return banks if step_mode in (1, 2, 4) else banks[:1]


def _legacy_sysex(sysex: bytes) -> bool:
    """Byte 0 is F0: the messages start where the firmware expects the length."""
    return sysex[0] == 0xF0


def _legacy_bank(rows: bytes, sysex: bytes) -> bool:
    sysex_enabled = any(rows[index * ROW_SIZE] and rows[index * ROW_SIZE + 2] == 4
                        for index in range(MIDI_ROWS))
    return sysex_enabled and _legacy_sysex(sysex)


def legacy_sysex_banks(image: bytes) -> list[str]:
    """Name the active banks that an earlier Stompilot stored in the old format."""
    image = _image(image)
    result = []
    for page_index in range(min(image[PAGE_COUNT_OFFSET] + 1, MAX_DEVICE_PAGES)):
        for switch_id in SWITCHES:
            offset = switch_offset(page_index, switch_id)
            block = image[offset:offset + SWITCH_SIZE]
            for bank, rows, sysex in _active_banks(block[0]):
                if _legacy_bank(block[rows:rows + MIDI_ROWS * ROW_SIZE], block[sysex:sysex + SYSEX_SIZE]):
                    result.append(f"Page {page_index + 1}, switch {switch_id}, bank {bank}")
    return result


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


def _sysex_commands(block: bytes, profiles: Mapping[str, dict]) -> list[str]:
    """Describe what each active bank's SysEx messages do, using the device profiles."""
    lines = []
    for bank, rows, sysex in _active_banks(block[0]):
        try:
            messages = _decode_bank(block[rows:rows + MIDI_ROWS * ROW_SIZE], block[sysex:sysex + SYSEX_SIZE], "")
        except ValidationError:
            continue
        for message in messages:
            if message[0] != 0xF0:
                continue
            actions = decode_message(message, profiles)
            text = (describe_decoded(actions, profiles) if actions else
                    f"SysEx ({len(message)} bytes) that no installed device profile sends")
            lines.append(f"    {bank}: {text}")
    return lines


def config_summary(image: bytes, profiles: Mapping[str, dict] | None = None) -> str:
    """Summarize the active pages; with profiles, also describe each SysEx command."""
    snapshot = decode_config(image)
    mode = snapshot["working_mode"]
    description = "Advanced Custom" if mode == ADVANCED_MODE else f"mode {mode}"
    count = snapshot["advanced_page_count"]
    lines = [f"Working mode: {description}",
             f"Advanced pages: {count}; current page: {snapshot['current_advanced_page']}"]
    described = False
    for page in snapshot["pages"][:min(count, MAX_DEVICE_PAGES)]:
        lines.append(f"Page {page['number']}")
        for switch_id, switch in page["switches"].items():
            banks = f"A: {switch['bank_a']}"
            if switch["step_mode"] in (1, 2, 4):
                banks += f"; B: {switch['bank_b']}"
            lines.append(f"  {switch_id}: {switch['mode']}; {banks}")
            if profiles:
                offset = switch_offset(page["number"] - 1, switch_id)
                commands = _sysex_commands(image[offset:offset + SWITCH_SIZE], profiles)
                described = described or bool(commands)
                lines.extend(commands)
    if described:
        lines.extend(["", "The controller stores only MIDI bytes, not the device they are for. A command "
                      "is named after the installed device profile that sends exactly those bytes; "
                      "CC commands are not named, because any device could use them."])
    legacy = legacy_sysex_banks(image)
    if legacy:
        lines.extend(["", f"{len(legacy)} SysEx bank(s) were written by an earlier Stompilot with F0 "
                      "where the controller expects the length byte, so the controller sends nothing "
                      "for them until they are rewritten: " + "; ".join(legacy) + "."])
    return "\n".join(lines)


def _decode_bank(rows: bytes, sysex: bytes, path: str) -> tuple[bytes, ...]:
    """Return the messages of a bank's supported active rows, without interpreting them."""
    if len(rows) != MIDI_ROWS * ROW_SIZE or len(sysex) != SYSEX_SIZE:
        raise ValidationError(f"{path}: native bank requires 80 row bytes and 128 SysEx bytes")
    enabled = []
    for index in range(MIDI_ROWS):
        row = rows[index * ROW_SIZE:(index + 1) * ROW_SIZE]
        if row[0] not in (0, 1):
            raise ValidationError(f"{path}, row {index + 1}: unsupported enabled flag {row[0]}")
        if not row[0]:
            continue
        if row[2] not in (ROW_PC, ROW_CC, ROW_SYSEX):
            raise ValidationError(f"{path}, row {index + 1}: MIDI type {row[2]} is not supported; only Program Change, CC and SysEx are")
        if row[1] > 15:
            raise ValidationError(f"{path}, row {index + 1}: MIDI channel must be from 1 to 16")
        if row[2] == ROW_CC and (row[3] > 127 or row[4] > 127):
            raise ValidationError(f"{path}, row {index + 1}: CC number and value must be from 0 to 127")
        if row[2] == ROW_PC and row[3] > 127:
            raise ValidationError(f"{path}, row {index + 1}: Program Change number must be from 0 to 127")
        enabled.append(row)
    if not enabled:
        return ()
    markers = sum(row[2] == ROW_SYSEX for row in enabled)
    if not markers:
        return tuple(bytes([0xB0 + row[1], row[3], row[4]]) if row[2] == ROW_CC else bytes([0xC0 + row[1], row[3]])
                     for row in enabled)
    if markers != 1 or len(enabled) != 1:
        raise ValidationError(f"{path}: a SysEx bank must have exactly one enabled SysEx row and no other enabled MIDI rows")
    if _legacy_sysex(sysex):
        # The old format stored the messages from byte 0, followed by zero padding.
        data = sysex.rstrip(b"\x00")
    else:
        if sysex[0] > SYSEX_LIMIT:
            raise ValidationError(f"{path}: SysEx length byte {sysex[0]} exceeds the {SYSEX_LIMIT}-byte limit")
        data = sysex[1:1 + sysex[0]]
        if data and data[0] != 0xF0:
            raise ValidationError(f"{path}: SysEx data starts with {data[0]:02X}, not F0. The controller sends the stored bytes unchanged and does not add F0, so each message must include its own F0 and F7")
    messages = _sysex_frames(data, path)
    if not messages:
        raise ValidationError(f"{path}: the enabled SysEx row has no complete message")
    return messages


def _sysex_frames(data: bytes, path: str) -> tuple[bytes, ...]:
    """Split the bytes a bank sends into complete F0...F7 messages."""
    messages = []
    position = 0
    while position < len(data):
        if data[position] != 0xF0:
            raise ValidationError(f"{path}: SysEx data byte {position + 1} is not the start of a complete F0...F7 message")
        end = data.find(b"\xf7", position + 1)
        if end < 0:
            raise ValidationError(f"{path}: SysEx message at data byte {position + 1} has no F7 terminator")
        message = data[position:end + 1]
        if any(byte > 127 for byte in message[1:-1]):
            raise ValidationError(f"{path}: SysEx message at data byte {position + 1} contains invalid 7-bit data")
        messages.append(message)
        position = end + 1
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


def _encode_bank(state: dict, path: str, source_state: dict) -> tuple[bytes, bytes]:
    if "native_bank" in source_state:
        native_rows, native_sysex = _native_bank(source_state["native_bank"], path)
        native_messages = _decode_bank(native_rows, native_sysex, path)
        messages = tuple(bytes.fromhex(message) for message in state["messages_hex"])
        # An unchanged bank in the old format is still re-encoded, which repairs it.
        if messages == native_messages and not _legacy_bank(native_rows, native_sysex):
            return native_rows, native_sysex
    rows = bytearray(MIDI_ROWS * ROW_SIZE)
    sysex = bytearray(SYSEX_SIZE)
    midi_rows = state["midi_rows"]
    if len(midi_rows) > MIDI_ROWS:
        raise ValidationError(f"{path}: Chocolate Plus supports at most {MIDI_ROWS} MIDI rows (CC or Program Change) per bank")
    if midi_rows:
        for index, row in enumerate(midi_rows):
            start = index * ROW_SIZE
            rows[start:start + ROW_SIZE] = bytes([1, row["channel"] - 1, ROW_TYPES[row["type"]], row["data1"], row["data2"]])
    elif state["messages_hex"]:
        data = b"".join(bytes.fromhex(message) for message in state["messages_hex"])
        if len(data) > SYSEX_LIMIT:
            raise ValidationError(f"{path}: Chocolate Plus SysEx bank exceeds {SYSEX_LIMIT} bytes")
        rows[:ROW_SIZE] = SYSEX_ROW
        sysex[0] = len(data)
        sysex[1:1 + len(data)] = data
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
        raise ValidationError("Chocolate Plus advanced page settings do not match the layout of firmware 12 to 16")
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
                repaired = _legacy_bank(original[offset + row_offset:offset + row_offset + len(rows)],
                                        original[offset + sysex_offset:offset + sysex_offset + len(sysex)])
                result[offset + row_offset:offset + row_offset + len(rows)] = rows
                result[offset + sysex_offset:offset + sysex_offset + len(sysex)] = sysex
                warnings.extend(state["warnings"])
                midi_rows = state["midi_rows"]
                count = len(state["messages_hex"])
                details = (_rows_label(len(midi_rows), {ROW_TYPES[row["type"]] for row in midi_rows}) if midi_rows
                           else f"{state['byte_count']} SysEx bytes, {count} message(s)")
                if count == 0:
                    details = "empty"
                assignments = "; ".join(state["summaries"])
                line = f"  Bank {'AB'[state_index]} ({state['label']}): {details}"
                if assignments:
                    line += f"; {assignments}"
                if repaired:
                    line += "; replaces SysEx in the old Stompilot format, which the controller does not send"
                summaries.append(line)
    result[0] = ADVANCED_MODE
    result[PAGE_COUNT_OFFSET] = len(pages) - 1
    result[CURRENT_PAGE_OFFSET] = 0
    summaries.append(f"Activate Advanced Custom mode with {len(pages)} page(s), starting on page 1")
    image = bytes(result)
    return WritePlan(image, summaries, list(dict.fromkeys(warnings)), changed_ranges(original, image))
