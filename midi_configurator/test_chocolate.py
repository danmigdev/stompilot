"""Native Chocolate image regression tests; no MIDI ports are opened."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .chocolate import (
    ADVANCED_MODE, ADVANCED_OFFSET, BANK_A_ROWS, BANK_A_SYSEX, BANK_B_ROWS,
    BANK_B_SYSEX, CONFIG_SIZE, CURRENT_PAGE_OFFSET, PAGE_COUNT_OFFSET,
    SWITCH_SIZE, SYSEX_ROW, _decode_bank, changed_ranges, config_summary, decode_config,
    legacy_sysex_banks, plan_project, switch_offset,
)
from .core import ValidationError, load_config, load_profiles, save_config
from .project import new_project
from .test_core import (
    FX_OFF, FX_ON, RELOAD_CURRENT, REV_MIX_10, REV_MIX_5, cc_fixture, config_fixture,
    enabled, mix, reload_profile_fixture, tank_fixture,
)


def stored(message_hex):
    """A SysEx area as the controller reads it: length byte, sent bytes, zero padding."""
    data = bytes.fromhex(message_hex)
    return bytes([len(data)]) + data + bytes(127 - len(data))


def legacy_stored(message_hex):
    """A SysEx area as earlier Stompilot releases wrote it, starting with F0."""
    data = bytes.fromhex(message_hex)
    return data + bytes(128 - len(data))


def original_fixture():
    """Use nonzero unrelated bytes to expose accidental destructive writes."""
    image = bytearray((index * 17 + 7) % 256 for index in range(CONFIG_SIZE))
    image[0] = 2
    image[PAGE_COUNT_OFFSET] = 2
    image[CURRENT_PAGE_OFFSET] = 2
    for page in range(8):
        for switch_id in "ABCD":
            image[switch_offset(page, switch_id)] = 0
    return bytes(image)


def readable_fixture(page_count=1):
    """Keep unrelated settings and inactive banks nonzero without exposing them."""
    image = bytearray(CONFIG_SIZE)
    image[0] = ADVANCED_MODE
    image[1:ADVANCED_OFFSET] = bytes((index * 17) % 256 for index in range(1, ADVANCED_OFFSET))
    image[13437:PAGE_COUNT_OFFSET] = bytes((index * 19) % 256 for index in range(13437, PAGE_COUNT_OFFSET))
    image[PAGE_COUNT_OFFSET] = page_count - 1
    image[CURRENT_PAGE_OFFSET] = 0
    image[CURRENT_PAGE_OFFSET + 1:] = b"\x04\x05\x01\x01\x01"
    for page in range(8):
        for switch_id in "ABCD":
            offset = switch_offset(page, switch_id)
            image[offset] = 0 if page < page_count else 5
            # Single-mode Bank B is inactive, even when it holds unsupported MIDI.
            image[offset + BANK_B_ROWS:offset + BANK_B_ROWS + 5] = b"\x01\x00\x00\x20\x00"
            image[offset + BANK_B_SYSEX:offset + BANK_B_SYSEX + 128] = bytes([0xA5]) * 128
    offset = switch_offset(0, "A")
    rows = bytearray(b"\x00\xFE\xFD\xFC\xFB" * 16)
    rows[15:20] = b"\x01\x0F\x01\x40\x7F"
    rows[60:65] = b"\x01\x01\x01\x02\x00"
    image[offset + BANK_A_ROWS:offset + BANK_A_ROWS + 80] = rows
    image[offset + BANK_A_SYSEX:offset + BANK_A_SYSEX + 128] = bytes([0xA6]) * 128
    return bytes(image)


def with_sysex_banks(image, switch_id, *vectors):
    """Store one SysEx bank per vector on page 1; two vectors make the switch alternating."""
    image = bytearray(image)
    start = switch_offset(0, switch_id)
    image[start] = 0 if len(vectors) == 1 else 1
    for (rows, sysex), vector in zip(((BANK_A_ROWS, BANK_A_SYSEX), (BANK_B_ROWS, BANK_B_SYSEX)), vectors):
        image[start + rows:start + rows + 80] = SYSEX_ROW + bytes(75)
        image[start + sysex:start + sysex + 128] = stored(vector)
    return bytes(image)


def raw(message_hex):
    return {"type": "raw_midi", "message_hex": message_hex}


def earlier_import(image, page_count=1):
    """A project as Stompilot 0.3.3 and earlier imported it: raw actions plus native bank bytes."""
    pages = []
    for page_index in range(page_count):
        switches = {}
        for switch_id in "ABCD":
            start = switch_offset(page_index, switch_id)
            mode = image[start]
            states = []
            for bank, rows, sysex in (("A", BANK_A_ROWS, BANK_A_SYSEX), ("B", BANK_B_ROWS, BANK_B_SYSEX))[:mode + 1]:
                rows = image[start + rows:start + rows + 80]
                sysex = image[start + sysex:start + sysex + 128]
                messages = _decode_bank(rows, sysex, f"Page {page_index + 1}, switch {switch_id}, bank {bank}")
                states.append({"label": f"Bank {bank}",
                               "actions": [raw(message.hex(" ").upper()) for message in messages],
                               "native_bank": {"rows_hex": rows.hex(" ").upper(), "sysex_hex": sysex.hex(" ").upper()}})
            switches[switch_id] = {"label": f"Switch {switch_id}", "mode": ("single", "alternating")[mode],
                                   "states": states}
        pages.append({"name": f"Page {page_index + 1}", "switches": switches})
    return {"schema_version": 1, "name": "Chocolate Plus configuration",
            "controller": {"id": "chocolate_plus", "max_sysex_bytes": 127}, "pages": pages}


def two_reverb_profile():
    """REV Spring comes first and uses slot 1 for Level, where Hall has Mix."""
    profile = reload_profile_fixture()
    rev = next(block for block in profile["blocks"] if block["id"] == "rev")
    rev["models"].insert(0, {"id": "spring", "label": "Spring", "wire_value": 3,
                             "parameters": [{"id": "level", "label": "Level", "index": 1,
                                             "min": 0, "max": 100, "step": 1}]})
    return profile


def legacy_fixture():
    """Alternating A and single B, written in the old format that the controller never sent."""
    image = bytearray(readable_fixture())
    a, b = switch_offset(0, "A"), switch_offset(0, "B")
    image[a] = 1
    for offset, rows, sysex, vector in [
        (a, BANK_A_ROWS, BANK_A_SYSEX, FX_ON),
        (a, BANK_B_ROWS, BANK_B_SYSEX, FX_OFF),
        (b, BANK_A_ROWS, BANK_A_SYSEX, FX_ON + " " + REV_MIX_10),
    ]:
        image[offset + rows:offset + rows + 80] = SYSEX_ROW + bytes(75)
        image[offset + sysex:offset + sysex + 128] = legacy_stored(vector)
    return bytes(image)


class ChocolateCodecTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {"tank": tank_fixture(), "generic": cc_fixture()}
        self.project = config_fixture()
        self.original = original_fixture()

    def test_native_offsets_match_live_firmware_layout(self):
        self.assertEqual(CONFIG_SIZE, 23646)
        self.assertEqual([switch_offset(0, letter) for letter in "ABCD"], [93, 510, 927, 1344])
        self.assertEqual(switch_offset(7, "D") + SWITCH_SIZE, 13437)
        self.assertEqual(PAGE_COUNT_OFFSET, 23639)
        self.assertEqual(CURRENT_PAGE_OFFSET, 23640)
        for page, letter in [(-1, "A"), (8, "A"), (True, "A"), (0, "E")]:
            with self.subTest(page=page, letter=letter), self.assertRaises(ValidationError):
                switch_offset(page, letter)

    def test_boost_and_reverb_encode_both_banks_with_exact_vectors(self):
        plan = plan_project(self.project, self.profiles, self.original)
        block = plan.image[switch_offset(0, "B"):switch_offset(0, "B") + SWITCH_SIZE]
        self.assertEqual(block[0], 1)
        for rows_offset, sysex_offset, vector in [
            (BANK_A_ROWS, BANK_A_SYSEX, FX_ON + " " + REV_MIX_10),
            (BANK_B_ROWS, BANK_B_SYSEX, FX_OFF + " " + REV_MIX_5),
        ]:
            data = bytes.fromhex(vector)
            self.assertEqual(len(data), 43)
            self.assertEqual(block[rows_offset:rows_offset + 80], b"\x01\x00\x04\x00\x00" + bytes(75))
            self.assertEqual(block[sysex_offset:sysex_offset + 128], stored(vector))
            self.assertEqual(block[sysex_offset], 43)
        self.assertTrue(any("consecutive" in warning for warning in plan.warnings))
        self.assertTrue(any("switch B" in summary for summary in plan.summaries))
        self.assertTrue(any("ON / Mix 10" in summary and "43 SysEx bytes" in summary and "Mix = 10" in summary for summary in plan.summaries))
        self.assertTrue(any("OFF / Mix 5" in summary and "Mix = 5" in summary for summary in plan.summaries))

    def test_reload_saved_preset_replaces_only_boost_bank_b_with_exact_native_frame(self):
        profiles = load_profiles(Path(__file__).with_name("profiles"))
        project = new_project()
        previous = plan_project(project, profiles, self.original)
        state = project["pages"][0]["switches"]["B"]["states"][1]
        state["label"] = "Reload saved preset"
        state["actions"] = [{"device_id": "tank_mini_v09", "type": "reload_current_preset"}]
        plan = plan_project(project, profiles, self.original)
        start = switch_offset(0, "B")
        reload_frame = bytes.fromhex(
            "F0 00 32 09 49 00 00 00 02 01 00 00 00 12 00 00 00 7F 35 03 F7")
        self.assertEqual(len(reload_frame), 21)
        self.assertEqual(plan.image[start], 1)
        self.assertEqual(plan.image[start + BANK_B_ROWS:start + BANK_B_ROWS + 80],
                         b"\x01\x00\x04\x00\x00" + bytes(75))
        self.assertEqual(plan.image[start + BANK_B_SYSEX:start + BANK_B_SYSEX + 128],
                         bytes([21]) + reload_frame + bytes(106))
        boost_and_mix = bytes.fromhex(FX_ON + " " + REV_MIX_10)
        self.assertEqual(len(boost_and_mix), 43)
        self.assertEqual(plan.image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80],
                         b"\x01\x00\x04\x00\x00" + bytes(75))
        self.assertEqual(plan.image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128],
                         bytes([43]) + boost_and_mix + bytes(84))
        bank_b_offsets = set(range(start + BANK_B_ROWS, start + BANK_B_ROWS + 80))
        bank_b_offsets.update(range(start + BANK_B_SYSEX, start + BANK_B_SYSEX + 128))
        changed = {index for index, (old, new) in enumerate(zip(previous.image, plan.image))
                   if old != new}
        self.assertTrue(changed)
        self.assertTrue(changed.issubset(bank_b_offsets))
        self.assertEqual(decode_config(plan.image)["pages"][0]["switches"]["B"]["bank_b"],
                         "SysEx, 21 bytes, 1 message(s)")
        self.assertTrue(any("experimental" in warning.lower() for warning in plan.warnings))
        self.assertTrue(any("whole saved preset" in warning.lower() and
                            "temporary edits" in warning.lower() for warning in plan.warnings))
        self.assertTrue(any("Reload current preset" in summary and "21 SysEx bytes" in summary
                            for summary in plan.summaries))

    def test_sysex_bank_matches_the_layout_verified_on_the_controller(self):
        # FootCtrlPlus_013, 2026-10-15: with this Bank A, button A sends a valid MOD-on frame.
        verified = bytes.fromhex("15 F0 00 32 09 49 00 00 00 02 0A 00 00 00 11 00 00 00 01 3E 03 F7") + bytes(106)
        profiles = load_profiles(Path(__file__).with_name("profiles"))
        plan = plan_project(new_project(), profiles, self.original)
        start = switch_offset(0, "A")
        self.assertEqual(plan.image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80],
                         b"\x01\x00\x04\x00\x00" + bytes(75))
        self.assertEqual(plan.image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128], verified)
        self.assertEqual(decode_config(plan.image)["pages"][0]["switches"]["A"]["bank_a"],
                         "SysEx, 21 bytes, 1 message(s)")

    def test_largest_bank_is_127_bytes_after_the_length_byte(self):
        project = deepcopy(self.project)
        largest = (b"\xF0" + bytes(125) + b"\xF7").hex(" ")
        project["pages"][0]["switches"] = {"D": {"mode": "single", "states": [
            {"actions": [{"type": "raw_midi", "message_hex": largest}]}]}}
        plan = plan_project(project, self.profiles, self.original)
        start = switch_offset(0, "D")
        self.assertEqual(plan.image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128], stored(largest))
        self.assertEqual(plan.image[start + BANK_A_SYSEX], 127)
        project["pages"][0]["switches"]["D"]["states"][0]["actions"] = [
            {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(61) + b"\xF7").hex(" ")},
            {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(63) + b"\xF7").hex(" ")}]
        with self.assertRaisesRegex(ValidationError, "128 SysEx bytes; the limit is 127"):
            plan_project(project, self.profiles, self.original)

    def test_preserves_every_byte_outside_selected_banks_and_three_globals(self):
        self.project["pages"][0]["switches"] = {"B": self.project["pages"][0]["switches"]["B"]}
        plan = plan_project(self.project, self.profiles, self.original)
        start = switch_offset(0, "B")
        allowed = set(range(start, start + SWITCH_SIZE)) | {0, PAGE_COUNT_OFFSET, CURRENT_PAGE_OFFSET}
        changed = {index for index, pair in enumerate(zip(self.original, plan.image)) if pair[0] != pair[1]}
        self.assertTrue(changed.issubset(allowed))
        self.assertGreater(len(changed), 100)
        self.assertEqual(plan.image[:ADVANCED_OFFSET][1:], self.original[:ADVANCED_OFFSET][1:])
        self.assertEqual(plan.image[13437:PAGE_COUNT_OFFSET], self.original[13437:PAGE_COUNT_OFFSET])
        self.assertEqual(plan.image[CURRENT_PAGE_OFFSET + 1:], self.original[CURRENT_PAGE_OFFSET + 1:])
        self.assertEqual(plan.image[0], ADVANCED_MODE)
        self.assertEqual(plan.image[PAGE_COUNT_OFFSET], 0)
        self.assertEqual(plan.image[CURRENT_PAGE_OFFSET], 0)

    def test_single_mode_preserves_inactive_bank_b_and_warns_about_gesture_change(self):
        project = deepcopy(self.project)
        project["pages"][0]["switches"] = {"A": {"mode": "single", "states": [{"actions": [enabled(True)]}]}}
        original = bytearray(self.original)
        start = switch_offset(0, "A")
        original[start] = 4
        plan = plan_project(project, self.profiles, original)
        self.assertEqual(original[start], 4)
        self.assertEqual(plan.image[start], 0)
        self.assertEqual(plan.image[start + BANK_B_ROWS:start + BANK_B_ROWS + 80], original[start + BANK_B_ROWS:start + BANK_B_ROWS + 80])
        self.assertEqual(plan.image[start + BANK_B_SYSEX:start + SWITCH_SIZE], original[start + BANK_B_SYSEX:start + SWITCH_SIZE])
        self.assertTrue(any("existing short/long press behavior" in warning for warning in plan.warnings))

    def test_empty_single_bank_clears_rows_and_sysex_but_preserves_bank_b(self):
        project = deepcopy(self.project)
        project["pages"][0]["switches"] = {"D": {"mode": "single", "states": [{"actions": []}]}}
        start = switch_offset(0, "D")
        image = plan_project(project, self.profiles, self.original).image
        self.assertEqual(image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80], bytes(80))
        self.assertEqual(image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128], bytes(128))
        self.assertEqual(image[start + BANK_B_ROWS:start + BANK_B_ROWS + 80], self.original[start + BANK_B_ROWS:start + BANK_B_ROWS + 80])

    def test_sixteen_cc_rows_encode_channel_and_fields_and_clear_old_sysex(self):
        action = {"device_id": "generic", "block_id": "delay", "type": "enable", "value": True, "channel": 16}
        project = deepcopy(self.project)
        project["pages"][0]["switches"] = {"C": {"mode": "single", "states": [{"actions": [action] * 16}]}}
        start = switch_offset(0, "C")
        plan = plan_project(project, self.profiles, self.original)
        self.assertEqual(plan.image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80], b"\x01\x0f\x01\x1a\x7f" * 16)
        self.assertEqual(plan.image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128], bytes(128))
        self.assertIn("ch 16 CC 26 = 127", config_summary(plan.image))
        project["pages"][0]["switches"]["C"]["states"][0]["actions"].append(action)
        with self.assertRaisesRegex(ValidationError, "at most 16 MIDI rows"):
            plan_project(project, self.profiles, self.original)

    def test_eight_pages_use_zero_based_page_count_and_ninth_is_rejected(self):
        self.project["pages"] *= 8
        plan = plan_project(self.project, self.profiles, self.original)
        self.assertEqual(plan.image[PAGE_COUNT_OFFSET], 7)
        self.assertEqual(plan.image[switch_offset(7, "B")], 1)
        self.project["pages"].append(deepcopy(self.project["pages"][0]))
        with self.assertRaisesRegex(ValidationError, "at most 8 advanced pages"):
            plan_project(self.project, self.profiles, self.original)

    def test_oversized_sysex_rejected_before_image_is_modified(self):
        state = self.project["pages"][0]["switches"]["A"]["states"][0]
        state["actions"] = [enabled(True)] * 7
        with self.assertRaisesRegex(ValidationError, "limit is 127"):
            plan_project(self.project, self.profiles, self.original)

    def test_bad_length_and_unknown_global_or_selected_step_mode_rejected(self):
        for image in [bytes(23645), bytes(23647), "x" * CONFIG_SIZE]:
            with self.subTest(length=len(image)), self.assertRaises(ValidationError):
                plan_project(self.project, self.profiles, image)
        for offset, value in [(0, 255), (PAGE_COUNT_OFFSET, 8), (CURRENT_PAGE_OFFSET, 8), (switch_offset(0, "B"), 5)]:
            image = bytearray(self.original)
            image[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ValidationError):
                plan_project(self.project, self.profiles, image)

    def test_change_spans_exactly_reconstruct_image_and_skip_unchanged_bytes(self):
        plan = plan_project(self.project, self.profiles, self.original)
        image = bytearray(self.original)
        previous_end = -1
        for item in plan.ranges:
            self.assertGreater(item.offset, previous_end)
            self.assertEqual(item.before, self.original[item.offset:item.offset + item.length])
            self.assertTrue(all(old != new for old, new in zip(item.before, item.after)))
            image[item.offset:item.offset + item.length] = item.after
            previous_end = item.offset + item.length
        self.assertEqual(bytes(image), plan.image)
        self.assertEqual(plan.changed_bytes, sum(old != new for old, new in zip(self.original, plan.image)))
        self.assertEqual(changed_ranges(plan.image, plan.image), ())

    def test_decoded_summary_shows_physical_letters_modes_and_actual_bank_sizes(self):
        image = plan_project(self.project, self.profiles, self.original).image
        snapshot = decode_config(image)
        self.assertEqual(snapshot["advanced_page_count"], 1)
        self.assertEqual(snapshot["current_advanced_page"], 1)
        self.assertEqual(snapshot["pages"][0]["switches"]["B"]["bank_a"], "SysEx, 43 bytes, 2 message(s)")
        self.assertEqual(snapshot["pages"][0]["switches"]["C"]["bank_a"], "empty")
        summary = config_summary(image)
        self.assertIn("Working mode: Advanced Custom", summary)
        self.assertIn("B: alternating; A: SysEx, 43 bytes, 2 message(s); B: SysEx, 43 bytes, 2 message(s)", summary)
        self.assertNotIn("Page 2", summary)


class ChocolateReadTests(unittest.TestCase):
    """Reading only describes the controller; it never becomes a project."""

    def setUp(self):
        self.image = readable_fixture()

    def test_read_summary_lists_cc_rows_and_names_tank_commands_only_with_profiles(self):
        profiles = {"tank": reload_profile_fixture()}
        image = with_sysex_banks(self.image, "B", FX_ON + " " + REV_MIX_10, "F0 41 01 02 7F F7")
        image = with_sysex_banks(image, "C", RELOAD_CURRENT)
        summary = config_summary(image, profiles)
        self.assertIn("  A: single; A: 2 CC row(s): ch 16 CC 64 = 127; ch 2 CC 2 = 0\n"
                      "  B: alternating; A: SysEx, 43 bytes, 2 message(s); B: SysEx, 6 bytes, 1 message(s)\n"
                      "    A: TANK Mini V09 · FX: ON\n"
                      "    A: TANK Mini V09 · REV / Mix = 10\n"
                      "    B: SysEx (6 bytes) that no installed device profile sends\n"
                      "  C: single; A: SysEx, 21 bytes, 1 message(s)\n"
                      "    A: TANK Mini V09: Reload current preset (experimental)\n", summary)
        self.assertIn("not the device they are for", summary)
        plain = config_summary(image)
        self.assertNotIn("TANK Mini", plain)
        self.assertNotIn("not the device they are for", plain)

    def test_a_parameter_command_names_every_model_with_that_slot(self):
        profiles = {"tank": two_reverb_profile()}
        image = with_sysex_banks(self.image, "B", FX_ON + " " + REV_MIX_10)
        self.assertIn("    A: TANK Mini V09 · REV / Level or Mix = 10 (depends on the preset's model)\n",
                      config_summary(image, profiles))

    def test_invalid_sysex_banks_are_summarized_without_naming_commands(self):
        profiles = {"tank": reload_profile_fixture()}
        start = switch_offset(0, "D")
        for area, text in [
            (stored("F0 01"), "SysEx, 2 bytes, not complete F0...F7 messages"),
            # Typed without F0 in MidiSuite: the controller sends 00 32 09 ... as plain bytes.
            (stored(FX_ON[3:]), "SysEx, 20 bytes, not complete F0...F7 messages"),
            (bytes([200]) + bytes(127), "SysEx (invalid length byte 200)"),
            (bytes(128), "SysEx (no complete message)"),
        ]:
            image = bytearray(with_sysex_banks(self.image, "D", FX_ON))
            image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128] = area
            with self.subTest(text=text):
                summary = config_summary(bytes(image), profiles)
                self.assertTrue(summary.endswith(f"\n  D: single; A: {text}"), summary)
                self.assertNotIn("TANK Mini", summary)

    def test_non_advanced_modes_and_bad_image_lengths(self):
        for mode in (0, 2, 12, 255):
            image = bytearray(self.image)
            image[0] = mode
            with self.subTest(mode=mode):
                self.assertIn(f"Working mode: mode {mode}\n", config_summary(image))
        for image in (bytes(CONFIG_SIZE - 1), bytes(CONFIG_SIZE + 1), "x" * CONFIG_SIZE):
            with self.subTest(length=len(image)), self.assertRaises(ValidationError):
                config_summary(image)

    def test_old_stompilot_banks_are_flagged_until_a_project_rewrites_them(self):
        image = legacy_fixture()
        banks = ["Page 1, switch A, bank A", "Page 1, switch A, bank B", "Page 1, switch B, bank A"]
        self.assertEqual(legacy_sysex_banks(image), banks)
        switches = decode_config(image)["pages"][0]["switches"]
        self.assertIn("old Stompilot format", switches["A"]["bank_b"])
        self.assertIn("old Stompilot format", switches["B"]["bank_a"])
        self.assertIn("3 SysEx bank(s) were written by an earlier Stompilot", config_summary(image))
        plan = plan_project(config_fixture(), {"tank": tank_fixture(), "generic": cc_fixture()}, image)
        self.assertEqual(sum("old Stompilot format" in line for line in plan.summaries), 3)
        self.assertEqual(legacy_sysex_banks(plan.image), [])
        self.assertNotIn("earlier Stompilot", config_summary(plan.image))

    def test_only_active_banks_are_reported_in_the_old_format(self):
        image = bytearray(legacy_fixture())
        image[switch_offset(0, "A")] = 0
        self.assertEqual(legacy_sysex_banks(image), ["Page 1, switch A, bank A", "Page 1, switch B, bank A"])
        start = switch_offset(1, "C")
        image[start + BANK_A_ROWS:start + BANK_A_ROWS + 5] = SYSEX_ROW
        image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128] = legacy_stored(FX_ON)
        self.assertEqual(len(legacy_sysex_banks(image)), 2)
        image[PAGE_COUNT_OFFSET] = 1
        image[switch_offset(1, "C")] = 0
        self.assertIn("Page 2, switch C, bank A", legacy_sysex_banks(image))


class NativeBankTests(unittest.TestCase):
    """The rules that the stored bytes of a bank meet before a write reuses them."""

    def decode(self, rows, area=bytes(128)):
        return _decode_bank(rows + bytes(80 - len(rows)), area, "Page 1, switch D, bank A")

    def test_program_change_rows_decode_as_two_byte_messages(self):
        # MidiSuite's type 0; a FootCtrlPlus_013 sent C0 04 for this row on 2026-10-16.
        self.assertEqual(self.decode(bytes.fromhex("01 00 00 04 00 01 0F 01 07 64")),
                         (bytes.fromhex("C0 04"), bytes.fromhex("BF 07 64")))
        with self.assertRaisesRegex(ValidationError, "row 1: Program Change number"):
            self.decode(bytes.fromhex("01 00 00 80 00"))

    def test_unsupported_enabled_midi_types_name_the_row(self):
        for midi_type in (2, 3, 5, 255):
            with self.subTest(midi_type=midi_type), \
                    self.assertRaisesRegex(ValidationError, "Page 1, switch D, bank A, row 3: MIDI type"):
                self.decode(bytes(10) + bytes([1, 0, midi_type, 0, 0]))

    def test_invalid_enabled_flags_channels_and_cc_fields(self):
        for row, reason in [
            (b"\x02\x00\x01\x00\x00", "enabled flag"),
            (b"\x01\x10\x01\x00\x00", "channel"),
            (b"\x01\x00\x01\x80\x00", "number and value"),
            (b"\x01\x00\x01\x00\x80", "number and value"),
        ]:
            with self.subTest(row=row), self.assertRaisesRegex(ValidationError, reason):
                self.decode(row)

    def test_sysex_requires_one_marker_a_valid_length_and_complete_frames(self):
        for rows, area, reason in [
            (SYSEX_ROW + b"\x01\x00\x01\x00\x00", stored("F0 01 F7"), "exactly one"),
            (SYSEX_ROW * 2, stored("F0 01 F7"), "exactly one"),
            (SYSEX_ROW, stored("F0 01"), "no F7 terminator"),
            (SYSEX_ROW, stored("F0 80 F7"), "7-bit data"),
            (SYSEX_ROW, stored("F0 01 F7 00 05"), "data byte 4 is not the start"),
            (SYSEX_ROW, bytes(128), "no complete message"),
            (SYSEX_ROW, bytes([200]) + bytes(127), "length byte 200 exceeds the 127-byte limit"),
            (SYSEX_ROW, stored(FX_ON[3:]), "starts with 00, not F0"),
            (SYSEX_ROW, legacy_stored("F0 01"), "no F7 terminator"),
            (SYSEX_ROW, legacy_stored("F0 01 F7 00 05"), "data byte 4 is not the start"),
        ]:
            with self.subTest(reason=reason, area=area[:6].hex(" ")), self.assertRaisesRegex(ValidationError, reason):
                self.decode(rows, area)

    def test_only_the_bytes_within_the_length_are_sent(self):
        self.assertEqual(self.decode(SYSEX_ROW, bytes.fromhex("03 F0 01 F7 F0 02 F7") + bytes(121)),
                         (bytes.fromhex("F0 01 F7"),))
        data = b"\xF0" + bytes(125) + b"\xF7"
        self.assertEqual(self.decode(SYSEX_ROW, stored(data.hex(" "))), (data,))


class EarlierImportTests(unittest.TestCase):
    """Projects that Stompilot 0.3.3 and earlier imported from the controller still write correctly."""

    def setUp(self):
        self.image = readable_fixture()

    def test_unchanged_banks_write_back_every_byte_including_row_gaps_and_unused_buffers(self):
        image = bytearray(self.image)
        start = switch_offset(0, "B")
        image[start] = 1
        for rows_offset, sysex_offset, vector, row_index in [
            (BANK_A_ROWS, BANK_A_SYSEX, FX_ON + " " + REV_MIX_10, 5),
            (BANK_B_ROWS, BANK_B_SYSEX, RELOAD_CURRENT, 15),
        ]:
            rows = bytearray(b"\x00\xFE\xFD\xFC\xFB" * 16)
            rows[row_index * 5:row_index * 5 + 5] = b"\x01\x03\x04\xFE\xFF"
            image[start + rows_offset:start + rows_offset + 80] = rows
            image[start + sysex_offset:start + sysex_offset + 128] = stored(vector)
        image = bytes(image)
        project = earlier_import(image)
        switches = project["pages"][0]["switches"]
        self.assertEqual(switches["A"]["states"][0]["actions"], [raw("BF 40 7F"), raw("B1 02 00")])
        self.assertEqual([state["actions"] for state in switches["B"]["states"]],
                         [[raw(FX_ON), raw(REV_MIX_10)], [raw(RELOAD_CURRENT)]])
        plan = plan_project(project, {}, image)
        self.assertEqual(plan.image, image)
        self.assertEqual(plan.ranges, ())

    def test_editing_a_raw_cc_reencodes_only_its_bank(self):
        project = earlier_import(self.image)
        project["pages"][0]["switches"]["A"]["states"][0]["actions"][0]["message_hex"] = "BF 40 01"
        plan = plan_project(project, {}, self.image)
        start = switch_offset(0, "A")
        self.assertEqual(plan.image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80],
                         b"\x01\x0F\x01\x40\x01\x01\x01\x01\x02\x00" + bytes(70))
        self.assertEqual(plan.image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128], bytes(128))
        allowed = set(range(start + BANK_A_ROWS, start + BANK_A_ROWS + 80))
        allowed.update(range(start + BANK_A_SYSEX, start + BANK_A_SYSEX + 128))
        changed = {index for index, pair in enumerate(zip(self.image, plan.image)) if pair[0] != pair[1]}
        self.assertTrue(changed)
        self.assertTrue(changed.issubset(allowed))

    def test_saved_json_with_eight_pages_keeps_native_metadata(self):
        image = readable_fixture(8)
        project = earlier_import(image, 8)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "read-controller.json"
            save_config(path, project, {})
            loaded = load_config(path, {})
        self.assertEqual(loaded, project)
        self.assertEqual(plan_project(loaded, {}, image).image, image)

    def test_old_format_native_banks_are_rewritten_with_a_length_byte(self):
        image = legacy_fixture()
        project = earlier_import(image)
        switch_a = project["pages"][0]["switches"]["A"]
        self.assertEqual([state["actions"] for state in switch_a["states"]], [[raw(FX_ON)], [raw(FX_OFF)]])
        # The stored native bytes keep the old format; writing must not reuse them.
        self.assertTrue(switch_a["states"][0]["native_bank"]["sysex_hex"].startswith("F0"))
        plan = plan_project(project, {}, image)
        a, b = switch_offset(0, "A"), switch_offset(0, "B")
        areas = [(a + BANK_A_SYSEX, FX_ON), (a + BANK_B_SYSEX, FX_OFF), (b + BANK_A_SYSEX, FX_ON + " " + REV_MIX_10)]
        allowed = set()
        for offset, vector in areas:
            self.assertEqual(plan.image[offset:offset + 128], stored(vector))
            allowed.update(range(offset, offset + 128))
        changed = {index for index, pair in enumerate(zip(image, plan.image)) if pair[0] != pair[1]}
        self.assertTrue(changed.issubset(allowed))
        self.assertEqual(sum("old Stompilot format" in line for line in plan.summaries), 3)
        self.assertEqual(plan_project(earlier_import(plan.image), {}, plan.image).ranges, ())

    def test_native_metadata_is_validated_before_reuse_or_reencoding(self):
        project = earlier_import(self.image)
        for metadata, reason in [
            (None, "must be an object"),
            ({"rows_hex": "00", "sysex_hex": "00 " * 128}, "exactly 80 bytes"),
            ({"rows_hex": "00 " * 80, "sysex_hex": "not hex"}, "hexadecimal text"),
            ({"rows_hex": "00 " * 80, "sysex_hex": "00"}, "exactly 128 bytes"),
            ({"rows_hex": "01 00 02 00 00 " + "00 " * 75, "sysex_hex": "00 " * 128}, "MIDI type 2"),
        ]:
            for edited in (False, True):
                changed = deepcopy(project)
                changed_state = changed["pages"][0]["switches"]["A"]["states"][0]
                changed_state["native_bank"] = metadata
                if edited:
                    changed_state["actions"][0]["message_hex"] = "BF 40 01"
                with self.subTest(metadata=metadata, edited=edited), self.assertRaisesRegex(ValidationError, reason):
                    plan_project(changed, {}, self.image)


if __name__ == "__main__":
    unittest.main()
