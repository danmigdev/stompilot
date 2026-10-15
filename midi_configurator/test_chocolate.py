"""Native Chocolate image regression tests; no MIDI ports are opened."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .chocolate import (
    ADVANCED_MODE, ADVANCED_OFFSET, BANK_A_ROWS, BANK_A_SYSEX, BANK_B_ROWS,
    BANK_B_SYSEX, CONFIG_SIZE, CURRENT_PAGE_OFFSET, PAGE_COUNT_OFFSET,
    SWITCH_SIZE, SYSEX_ROW, changed_ranges, config_summary, decode_config,
    import_controller_project, legacy_sysex_banks, plan_project, switch_offset,
)
from .core import ValidationError, compile_config, load_config, load_profiles, save_config
from .project import new_project
from .test_core import (
    FX_OFF, FX_ON, REV_MIX_10, REV_MIX_5, cc_fixture, config_fixture,
    enabled, tank_fixture,
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
        with self.assertRaisesRegex(ValidationError, "at most 16 CC rows"):
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


class ChocolateImportTests(unittest.TestCase):
    def setUp(self):
        self.image = readable_fixture()

    def test_cc_import_keeps_channels_order_and_neutral_labels_without_profiles(self):
        project = import_controller_project(self.image, {})
        self.assertEqual(project["name"], "Chocolate Plus configuration")
        self.assertEqual(len(project["pages"]), 1)
        switch = project["pages"][0]["switches"]["A"]
        self.assertEqual((switch["label"], switch["mode"]), ("Switch A", "single"))
        state = switch["states"][0]
        self.assertEqual(state["label"], "Bank A")
        self.assertEqual(state["actions"], [
            {"type": "raw_midi", "message_hex": "BF 40 7F"},
            {"type": "raw_midi", "message_hex": "B1 02 00"},
        ])
        compiled = compile_config(project, {})
        self.assertEqual(compiled["pages"][0]["switches"]["A"]["states"][0]["messages_hex"],
                         ["BF 40 7F", "B1 02 00"])
        self.assertEqual(project["pages"][0]["switches"]["D"]["states"][0]["actions"], [])

    def test_unchanged_import_preserves_every_byte_including_row_gaps_and_unused_buffers(self):
        project = import_controller_project(self.image, {})
        plan = plan_project(project, {}, self.image)
        self.assertEqual(plan.image, self.image)
        self.assertEqual(plan.ranges, ())
        self.assertEqual(plan.changed_bytes, 0)
        self.assertEqual(self.image, readable_fixture())
        state = project["pages"][0]["switches"]["A"]["states"][0]
        self.assertEqual(len(bytes.fromhex(state["native_bank"]["rows_hex"])), 80)
        self.assertEqual(len(bytes.fromhex(state["native_bank"]["sysex_hex"])), 128)

    def test_editing_imported_cc_reencodes_only_its_active_bank(self):
        project = import_controller_project(self.image, {})
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

    def test_alternating_sysex_import_keeps_each_frame_raw_and_round_trips(self):
        image = bytearray(self.image)
        start = switch_offset(0, "B")
        image[start] = 1
        reload_frame = "F0 00 32 09 49 00 00 00 02 01 00 00 00 12 00 00 00 7F 35 03 F7"
        for rows_offset, sysex_offset, vector, row_index in [
            (BANK_A_ROWS, BANK_A_SYSEX, FX_ON + " " + REV_MIX_10, 5),
            (BANK_B_ROWS, BANK_B_SYSEX, reload_frame, 15),
        ]:
            rows = bytearray(b"\x00\xFE\xFD\xFC\xFB" * 16)
            rows[row_index * 5:row_index * 5 + 5] = b"\x01\x03\x04\xFE\xFF"
            image[start + rows_offset:start + rows_offset + 80] = rows
            image[start + sysex_offset:start + sysex_offset + 128] = stored(vector)
        project = import_controller_project(image, {"tank": tank_fixture()})
        switch = project["pages"][0]["switches"]["B"]
        self.assertEqual(switch["mode"], "alternating")
        self.assertEqual([state["label"] for state in switch["states"]], ["Bank A", "Bank B"])
        self.assertEqual(switch["states"][0]["actions"], [
            {"type": "raw_midi", "message_hex": FX_ON},
            {"type": "raw_midi", "message_hex": REV_MIX_10},
        ])
        self.assertEqual(switch["states"][1]["actions"], [{"type": "raw_midi", "message_hex": reload_frame}])
        self.assertEqual(plan_project(project, {}, image).image, bytes(image))

    def test_all_eight_active_pages_import_and_saved_json_retains_native_metadata(self):
        image = readable_fixture(8)
        project = import_controller_project(image, {})
        self.assertEqual(len(project["pages"]), 8)
        self.assertEqual(project["pages"][-1]["name"], "Page 8")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "read-controller.json"
            save_config(path, project, {})
            loaded = load_config(path, {})
        self.assertEqual(loaded, project)
        self.assertEqual(plan_project(loaded, {}, image).image, image)

    def test_non_advanced_mode_stays_inspectable_but_cannot_be_imported(self):
        for mode in (0, 2, 12, 255):
            image = bytearray(self.image)
            image[0] = mode
            with self.subTest(mode=mode):
                self.assertIn(f"mode {mode}", config_summary(image))
                with self.assertRaisesRegex(ValidationError, "Advanced Custom mode.*read-only"):
                    import_controller_project(image, {})

    def test_unsupported_active_step_modes_include_page_and_switch(self):
        for mode in (2, 3, 4, 5):
            image = bytearray(readable_fixture(2))
            image[switch_offset(1, "C")] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValidationError, "Page 2, switch C:.*step mode cannot be imported"):
                import_controller_project(image, {})

    def test_unsupported_enabled_midi_types_include_bank_and_row(self):
        for midi_type in (0, 2, 3, 5, 255):
            image = bytearray(self.image)
            offset = switch_offset(0, "C") + BANK_A_ROWS + 10
            image[offset:offset + 5] = bytes([1, 0, midi_type, 0, 0])
            with self.subTest(midi_type=midi_type), self.assertRaisesRegex(ValidationError, "Page 1, switch C, bank a, row 3: MIDI type"):
                import_controller_project(image, {})

    def test_invalid_enabled_flags_channels_and_cc_fields_fail_before_import(self):
        for row, reason in [
            (b"\x02\x00\x01\x00\x00", "enabled flag"),
            (b"\x01\x10\x01\x00\x00", "channel"),
            (b"\x01\x00\x01\x80\x00", "number and value"),
            (b"\x01\x00\x01\x00\x80", "number and value"),
        ]:
            image = bytearray(self.image)
            offset = switch_offset(0, "D") + BANK_A_ROWS
            image[offset:offset + 5] = row
            with self.subTest(row=row), self.assertRaisesRegex(ValidationError, reason):
                import_controller_project(image, {})

    def import_bank_d(self, rows, area):
        image = bytearray(self.image)
        start = switch_offset(0, "D")
        image[start + BANK_A_ROWS:start + BANK_A_ROWS + 80] = rows + bytes(80 - len(rows))
        image[start + BANK_A_SYSEX:start + BANK_A_SYSEX + 128] = area
        return bytes(image), import_controller_project(bytes(image), {})

    def test_sysex_requires_one_marker_a_valid_length_and_complete_frames(self):
        for rows, area, reason in [
            (SYSEX_ROW + b"\x01\x00\x01\x00\x00", stored("F0 01 F7"), "exactly one"),
            (SYSEX_ROW * 2, stored("F0 01 F7"), "exactly one"),
            (SYSEX_ROW, stored("F0 01"), "no F7 terminator"),
            (SYSEX_ROW, stored("F0 80 F7"), "7-bit data"),
            (SYSEX_ROW, stored("F0 01 F7 00 05"), "data byte 4 is not the start"),
            (SYSEX_ROW, bytes(128), "no complete message"),
            (SYSEX_ROW, bytes([200]) + bytes(127), "length byte 200 exceeds the 127-byte limit"),
            # Typed without F0 in MidiSuite: the controller sends 00 32 09 ... as plain bytes.
            (SYSEX_ROW, stored(FX_ON[3:]), "starts with 00, not F0"),
            (SYSEX_ROW, legacy_stored("F0 01"), "no F7 terminator"),
            (SYSEX_ROW, legacy_stored("F0 01 F7 00 05"), "data byte 4 is not the start"),
        ]:
            with self.subTest(reason=reason, area=area[:6].hex(" ")), self.assertRaisesRegex(ValidationError, reason):
                self.import_bank_d(rows, area)

    def test_bytes_after_the_length_are_not_sent_and_are_not_imported(self):
        area = bytes.fromhex("03 F0 01 F7 F0 02 F7") + bytes(121)
        image, project = self.import_bank_d(SYSEX_ROW, area)
        state = project["pages"][0]["switches"]["D"]["states"][0]
        self.assertEqual(state["actions"], [{"type": "raw_midi", "message_hex": "F0 01 F7"}])
        self.assertEqual(plan_project(project, {}, image).image, image)

    def test_full_127_byte_sysex_bank_imports_without_losing_the_terminator(self):
        data = (b"\xF0" + bytes(125) + b"\xF7").hex(" ")
        image, project = self.import_bank_d(SYSEX_ROW, stored(data))
        message = project["pages"][0]["switches"]["D"]["states"][0]["actions"][0]["message_hex"]
        self.assertEqual(bytes.fromhex(message), bytes.fromhex(data))
        self.assertEqual(plan_project(project, {}, image).image, image)

    def test_old_stompilot_banks_are_flagged_imported_and_rewritten_with_a_length_byte(self):
        image = legacy_fixture()
        banks = ["Page 1, switch A, bank A", "Page 1, switch A, bank B", "Page 1, switch B, bank A"]
        self.assertEqual(legacy_sysex_banks(image), banks)
        switches = decode_config(image)["pages"][0]["switches"]
        self.assertIn("old Stompilot format", switches["A"]["bank_b"])
        self.assertIn("old Stompilot format", switches["B"]["bank_a"])
        self.assertIn("3 SysEx bank(s) were written by an earlier Stompilot", config_summary(image))
        project = import_controller_project(image, {})
        switch_a = project["pages"][0]["switches"]["A"]
        self.assertEqual([state["actions"] for state in switch_a["states"]], [
            [{"type": "raw_midi", "message_hex": FX_ON}], [{"type": "raw_midi", "message_hex": FX_OFF}]])
        self.assertEqual(project["pages"][0]["switches"]["B"]["states"][0]["actions"], [
            {"type": "raw_midi", "message_hex": FX_ON}, {"type": "raw_midi", "message_hex": REV_MIX_10}])
        # The imported native bytes keep the old format; writing must not reuse them.
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
        self.assertEqual(legacy_sysex_banks(plan.image), [])
        self.assertNotIn("earlier Stompilot", config_summary(plan.image))
        reread = import_controller_project(plan.image, {})
        self.assertEqual(plan_project(reread, {}, plan.image).ranges, ())

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

    def test_old_format_bank_too_long_for_the_length_byte_cannot_be_imported(self):
        data = (b"\xF0" + bytes(126) + b"\xF7").hex(" ")
        with self.assertRaisesRegex(ValidationError, "switch D.*128 bytes; the maximum is 127"):
            self.import_bank_d(SYSEX_ROW, legacy_stored(data))

    def test_native_metadata_is_validated_before_reuse_or_reencoding(self):
        project = import_controller_project(self.image, {})
        state = project["pages"][0]["switches"]["A"]["states"][0]
        for metadata, reason in [
            (None, "must be an object"),
            ({"rows_hex": "00", "sysex_hex": "00 " * 128}, "exactly 80 bytes"),
            ({"rows_hex": "00 " * 80, "sysex_hex": "not hex"}, "hexadecimal text"),
            ({"rows_hex": "00 " * 80, "sysex_hex": "00"}, "exactly 128 bytes"),
            ({"rows_hex": "01 00 00 00 00 " + "00 " * 75, "sysex_hex": "00 " * 128}, "MIDI type 0"),
        ]:
            for edited in (False, True):
                changed = deepcopy(project)
                changed_state = changed["pages"][0]["switches"]["A"]["states"][0]
                changed_state["native_bank"] = metadata
                if edited:
                    changed_state["actions"][0]["message_hex"] = "BF 40 01"
                with self.subTest(metadata=metadata, edited=edited), self.assertRaisesRegex(ValidationError, reason):
                    plan_project(changed, {}, self.image)
        self.assertIsInstance(state["native_bank"], dict)

    def test_bad_image_lengths_and_out_of_range_footer_values_are_rejected(self):
        for image in (bytes(CONFIG_SIZE - 1), bytes(CONFIG_SIZE + 1), "x" * CONFIG_SIZE):
            with self.subTest(length=len(image)), self.assertRaises(ValidationError):
                import_controller_project(image, {})
        for offset in (PAGE_COUNT_OFFSET, CURRENT_PAGE_OFFSET):
            image = bytearray(self.image)
            image[offset] = 8
            with self.subTest(offset=offset), self.assertRaisesRegex(ValidationError, "advanced page settings"):
                import_controller_project(image, {})


if __name__ == "__main__":
    unittest.main()
