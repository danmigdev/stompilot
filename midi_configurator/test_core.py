"""Offline regression tests: real packet vectors and controller constraints."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from .core import (
    CompiledAction, PROFILE_VALIDATORS, PROTOCOL_DECODERS, PROTOCOL_ENCODERS,
    TANK_LIVE_PATCH, ValidationError, compile_action, compile_config, compile_state,
    decode_message, describe_decoded, load_config, load_profiles, pack7, packet,
    parse_packet, register_protocol, save_config, sysex_limit, validate_config,
    tank_live_write, validate_profile,
)
from .project import normalize_project


FX_ON = "F0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 01 42 03 F7"
FX_OFF = "F0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 00 44 03 F7"
REV_MIX_10 = "F0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 0A 00 64 04 F7"
REV_MIX_5 = "F0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 05 00 78 04 F7"
RELOAD_CURRENT = "F0 00 32 09 49 00 00 00 02 01 00 00 00 12 00 00 00 7F 35 03 F7"


def tank_fixture():
    return {"schema_version": 1, "id": "tank", "name": "TANK Mini V09",
            "protocol": "tank_v09", "evidence": {"status": "firmware_static"},
            "blocks": [
                {"id": "fx", "label": "FX", "wire_id": 0,
                 "models": [{"id": "boost", "label": "Boost", "wire_value": 1,
                             "parameters": [{"id": "gain", "label": "Gain", "index": 1,
                                             "min": 0, "max": 100, "step": 1}]}]},
                {"id": "mod", "label": "MOD", "wire_id": 2,
                 "evidence": {"status": "device_checked"},
                 "models": [{"id": "tremolo", "label": "Tremolo", "wire_value": 2,
                             "parameters": [{"id": "depth", "label": "Depth", "index": 1,
                                             "min": 0, "max": 100, "step": 1}]}]},
                {"id": "rev", "label": "REV", "wire_id": 4,
                 "models": [{"id": "hall", "label": "Hall", "wire_value": 1,
                             "parameters": [{"id": "mix", "label": "Mix", "index": 1,
                                             "min": 0, "max": 100, "step": 1,
                                             "evidence": {"status": "static_roundtrip"}}]}]}]}


def cc_fixture():
    return {"schema_version": 1, "id": "generic", "name": "Generic CC",
            "protocol": "midi_cc", "evidence": {"status": "documented"},
            "blocks": [{"id": "delay", "label": "Delay",
                        "enable": {"cc": 26, "on": 127, "off": 0},
                        "models": [{"id": "current", "label": "Current effect",
                                    "parameters": [{"id": "mix", "label": "Mix", "cc": 12,
                                                    "min": 0, "max": 127, "step": 1}]}]}]}


def enabled(value, block="fx", device="tank"):
    return {"device_id": device, "block_id": block, "type": "enable", "value": value}


def mix(value):
    return {"device_id": "tank", "block_id": "rev", "type": "parameter",
            "model_id": "hall", "param_id": "mix", "value": value}


def config_fixture():
    return {"schema_version": 1, "name": "Boost + reverb",
            "controller": {"id": "chocolate_plus", "max_sysex_bytes": 127},
            "pages": [{"name": "Page 1", "switches": {
                "A": {"label": "Tremolo", "mode": "alternating",
                      "states": [{"label": "ON", "actions": [enabled(True, "mod")]},
                                 {"label": "OFF", "actions": [enabled(False, "mod")]}]},
                "B": {"label": "Boost", "mode": "alternating",
                      "states": [{"label": "ON / Mix 10", "actions": [enabled(True), mix(10)]},
                                 {"label": "OFF / Mix 5", "actions": [enabled(False), mix(5)]}]},
                "C": {"label": "C", "mode": "single", "states": [{"actions": []}]},
                "D": {"label": "D", "mode": "single", "states": [{"actions": []}]}}}]}


def reload_profile_fixture():
    profile = tank_fixture()
    profile["device_actions"] = [{
        "id": "reload_current_preset", "label": "Reload current preset", "experimental": True,
        "evidence": {"status": "firmware_static_only", "note": "Stock V09 code path; not physically tested."},
        "warnings": ["Profile-specific reload note."],
    }]
    return profile


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {"tank": tank_fixture(), "generic": cc_fixture()}

    def test_known_fx_and_reverb_vectors_match_previous_configuration(self):
        for action, expected in [(enabled(True), FX_ON), (enabled(False), FX_OFF),
                                 (mix(10), REV_MIX_10), (mix(5), REV_MIX_5)]:
            with self.subTest(expected=expected):
                self.assertEqual(compile_action(action, self.profiles).hex, expected)

    def test_boost_bank_has_two_complete_messages_and_43_bytes(self):
        state = compile_state({"actions": [enabled(True), mix(10)]}, self.profiles)
        self.assertEqual(state.sysex_hex, FX_ON + "\n" + REV_MIX_10)
        self.assertEqual(state.byte_count, 43)
        self.assertEqual(len(state.messages), 2)
        self.assertEqual(state.midi_rows, ())
        self.assertTrue(any("consecutive" in warning for warning in state.warnings))
        self.assertEqual(state.evidence_status, "mixed")

    def test_tank_assignments_only_write_requested_live_bytes(self):
        for action, offset, data in [(enabled(True), 8, b"\x01"),
                                    (mix(5), 70, b"\x05\x00")]:
            with self.subTest(offset=offset):
                decoded = parse_packet(compile_action(action, self.profiles).messages[0])
                payload = decoded["payload"]
                self.assertEqual(decoded["opcode"], 0x22)
                self.assertEqual(payload[0], 4)
                self.assertEqual(int.from_bytes(payload[1:5], "little"), 0x10000000 + offset)
                self.assertEqual(int.from_bytes(payload[5:8], "little"), len(data))
                self.assertEqual(payload[8:], data)

    def test_reverb_accepts_raw_endpoints_and_rejects_out_of_range_and_strings(self):
        for value in (0, 100):
            payload = parse_packet(compile_action(mix(value), self.profiles).messages[0])["payload"]
            self.assertEqual(payload[8:], bytes([value, 0]))
        for value in (-1, 101, "10", True, 5.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                compile_action(mix(value), self.profiles)

    def test_profile_display_range_does_not_transform_raw_value(self):
        parameter = self.profiles["tank"]["blocks"][2]["models"][0]["parameters"][0]
        parameter["display_range"] = {"min": 0, "max": 60}
        self.assertEqual(compile_action(mix(10), self.profiles).hex, REV_MIX_10)

    def test_enable_requires_explicit_state_and_does_not_change_model(self):
        for value, expected in [(True, FX_ON), (1, FX_ON), (False, FX_OFF), (0, FX_OFF)]:
            action = enabled(value)
            action["model_id"] = "boost"
            result = compile_action(action, self.profiles)
            self.assertEqual(result.hex, expected)
            self.assertTrue(any("Boost" in warning for warning in result.warnings))
        for value in (127, "on", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                compile_action(enabled(value), self.profiles)

    def test_unknown_device_block_model_parameter_rejected(self):
        for field, value in [("device_id", "unknown"), ("block_id", "unknown"),
                             ("model_id", "unknown"), ("param_id", "unknown")]:
            action = mix(10)
            action[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                compile_action(action, self.profiles)

    def test_tank_model_writes_only_model_byte_and_reports_asynchronous_load(self):
        action = {"device_id": "tank", "block_id": "mod", "type": "model", "model_id": "tremolo"}
        result = compile_action(action, self.profiles)
        payload = parse_packet(result.messages[0])["payload"]
        self.assertEqual(int.from_bytes(payload[1:5], "little"), 0x10000010)
        self.assertEqual(payload[8:], b"\x02")
        self.assertTrue(any("asynchronously" in warning for warning in result.warnings))

    def test_model_then_same_block_parameter_cannot_race_in_same_bank(self):
        action = {"device_id": "tank", "block_id": "rev", "type": "model", "model_id": "hall"}
        with self.assertRaisesRegex(ValidationError, "asynchronous"):
            compile_state({"actions": [action, mix(10)]}, self.profiles)

    def test_sysex_bank_limit_counts_all_frames_and_cannot_be_increased(self):
        self.assertEqual(compile_state({"actions": [mix(5)] * 5}, self.profiles).byte_count, 110)
        with self.assertRaisesRegex(ValidationError, "132 SysEx bytes"):
            compile_state({"actions": [mix(5)] * 6}, self.profiles)
        for limit in (128, 256):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                compile_state({"actions": []}, self.profiles, max_sysex_bytes=limit)
        with self.assertRaisesRegex(ValidationError, "limit is 42"):
            compile_state({"actions": [enabled(True), mix(10)]}, self.profiles, max_sysex_bytes=42)

    def test_projects_saved_with_the_old_128_byte_limit_load_with_the_real_127_limit(self):
        config = config_fixture()
        config["controller"]["max_sysex_bytes"] = 128
        self.assertIs(validate_config(config, self.profiles), config)
        self.assertEqual(sysex_limit(config), 127)
        self.assertEqual(normalize_project(config)["controller"]["max_sysex_bytes"], 127)
        frames = [{"type": "raw_midi", "message_hex": (b"\xF0" + bytes(61) + b"\xF7").hex(" ")},
                  {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(63) + b"\xF7").hex(" ")}]
        config["pages"][0]["switches"]["C"] = {"mode": "single", "states": [{"actions": frames}]}
        with self.assertRaisesRegex(ValidationError, "128 SysEx bytes; the limit is 127"):
            validate_config(config, self.profiles)
        config = config_fixture()
        config["controller"]["max_sysex_bytes"] = 129
        with self.assertRaisesRegex(ValidationError, "max_sysex_bytes"):
            validate_config(config, self.profiles)
        del config["controller"]["max_sysex_bytes"]
        self.assertEqual(sysex_limit(config), 127)

    def test_generic_cc_state_is_rows_with_channel_and_explicit_values(self):
        on = enabled(True, "delay", "generic")
        on["channel"] = 16
        off = enabled(False, "delay", "generic")
        state = compile_state({"actions": [on, off]}, self.profiles)
        self.assertEqual(state.messages, (b"\xBF\x1A\x7F", b"\xB0\x1A\x00"))
        self.assertEqual(state.sysex_hex, "")
        self.assertEqual(state.midi_rows[0], {"channel": 16, "type": "CC", "data1": 26,
                                            "data2": 127, "hex": "BF 1A 7F"})

    def test_generic_profile_default_channel_and_parameter_boundaries(self):
        self.profiles["generic"]["channel"] = 4
        action = {"device_id": "generic", "block_id": "delay", "type": "parameter",
                  "model_id": "current", "param_id": "mix", "value": 127}
        self.assertEqual(compile_action(action, self.profiles).hex, "B3 0C 7F")
        for channel in (0, 17, True, "1"):
            with self.subTest(channel=channel), self.assertRaises(ValidationError):
                compile_action(dict(action, channel=channel), self.profiles)
        with self.assertRaises(ValidationError):
            compile_action(dict(action, value=128), self.profiles)

    def test_cc_and_sysex_cannot_silently_share_one_bank(self):
        with self.assertRaisesRegex(ValidationError, "cannot mix CC or Program Change with SysEx"):
            compile_state({"actions": [mix(10), enabled(True, "delay", "generic")]}, self.profiles)

    def test_parameter_step_is_validated(self):
        self.profiles["tank"]["blocks"][2]["models"][0]["parameters"][0]["step"] = 5
        self.assertEqual(compile_action(mix(10), self.profiles).hex, REV_MIX_10)
        with self.assertRaisesRegex(ValidationError, "step of 5"):
            compile_action(mix(11), self.profiles)

    def test_checksum_failure_and_truncated_sysex_are_rejected(self):
        message = bytearray(bytes.fromhex(REV_MIX_10))
        message[17] ^= 1
        with self.assertRaisesRegex(ValidationError, "checksum"):
            parse_packet(bytes(message))
        with self.assertRaises(ValidationError):
            parse_packet(bytes.fromhex(REV_MIX_10)[:-1])
        with self.assertRaises(ValidationError):
            parse_packet(b"\xF0\x7F\xF7")

    def test_packet_roundtrip_at_vendor_seven_bit_group_boundaries(self):
        for length in (0, 1, 2, 7, 14, 92):
            payload = bytes((index * 59) % 256 for index in range(length))
            with self.subTest(length=length):
                self.assertEqual(parse_packet(packet(0x22, payload))["payload"], payload)

    def test_project_json_roundtrip_and_export_preserve_pair_order(self):
        config = config_fixture()
        config["name"] = "Café configuration and quantity"
        before = deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            save_config(path, config, self.profiles)
            self.assertEqual(load_config(path, self.profiles), config)
            export = compile_config(config, self.profiles)
            state = export["pages"][0]["switches"]["B"]["states"][0]
            self.assertEqual(state["messages_hex"], [FX_ON, REV_MIX_10])
            self.assertEqual(state["byte_count"], 43)
            json.dumps(export)
        self.assertEqual(config, before)

    def test_project_switch_state_count_and_pages_are_validated(self):
        for mode, states in [("alternating", [{"actions": []}]), ("single", [{"actions": []}] * 2),
                             ("unsupported", [])]:
            config = config_fixture()
            config["pages"][0]["switches"]["A"] = {"mode": mode, "states": states}
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                validate_config(config, self.profiles)
        for count in (0, 17):
            config = config_fixture()
            config["pages"] *= count
            with self.subTest(pages=count), self.assertRaises(ValidationError):
                validate_config(config, self.profiles)
        config = config_fixture()
        config["pages"][0]["switches"]["E"] = {"mode": "single", "states": [{"actions": []}]}
        with self.assertRaises(ValidationError):
            validate_config(config, self.profiles)

    def test_unsafe_profile_addresses_and_duplicate_ids_are_rejected(self):
        for field, value in [("wire_id", 6), ("wire_id", -1)]:
            profile = tank_fixture()
            profile["blocks"][0][field] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_profile(profile)
        profile = tank_fixture()
        profile["blocks"][2]["models"][0]["parameters"][0]["index"] = 6
        with self.assertRaises(ValidationError):
            validate_profile(profile)
        profile = tank_fixture()
        profile["blocks"].append(deepcopy(profile["blocks"][0]))
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_profile(profile)

    def test_profiles_cannot_load_executable_protocol_names(self):
        profile = tank_fixture()
        profile["protocol"] = "__import__('os').system('never')"
        with self.assertRaises(ValidationError):
            validate_profile(profile)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "first.json").write_text(json.dumps(tank_fixture()), encoding="utf-8")
            (directory / "duplicate.json").write_text(json.dumps(tank_fixture()), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate"):
                load_profiles(directory)

    def test_trusted_encoder_registry_extends_core_without_changing_ui(self):
        called = []
        def validate(profile):
            called.append(profile["id"])
        def encode(action, profile):
            return CompiledAction((b"\xB0\x07\x40",), "Registered protocol", "documented")
        try:
            register_protocol("test_driver", encode, validate)
            profile = {"schema_version": 1, "id": "extension", "name": "Extension",
                       "protocol": "test_driver", "blocks": []}
            result = compile_action({"device_id": "extension", "type": "enable"}, {"extension": profile})
            self.assertEqual(result.hex, "B0 07 40")
            self.assertEqual(called, ["extension"])
            with self.assertRaises(ValidationError):
                register_protocol("test_driver", encode)
            register_protocol("test_driver", lambda action, profile: CompiledAction((b"\xB0\x07\xFF",), "bad", "unknown"), replace=True)
            with self.assertRaisesRegex(ValidationError, "invalid"):
                compile_action({"device_id": "extension"}, {"extension": profile})
        finally:
            PROTOCOL_ENCODERS.pop("test_driver", None)
            PROFILE_VALIDATORS.pop("test_driver", None)

    def test_bundled_profiles_validate_and_tank_production_ids_match_vectors(self):
        profiles = load_profiles(Path(__file__).parent / "profiles")
        self.assertIn("tank_mini_v09", profiles)
        action = {"device_id": "tank_mini_v09", "block_id": "rev", "type": "parameter",
                  "model_id": "1", "param_id": "mix", "value": 10}
        self.assertEqual(compile_action(action, profiles).hex, REV_MIX_10)
        for profile in profiles.values():
            for block in profile["blocks"]:
                if profile["protocol"] == "tank_v09" or "enable" in block:
                    compile_action({"device_id": profile["id"], "block_id": block["id"],
                                    "type": "enable", "value": True}, profiles)
                for model in block.get("models", []):
                    for parameter in model.get("parameters", []):
                        result = compile_action({"device_id": profile["id"], "block_id": block["id"],
                                                 "model_id": model["id"], "param_id": parameter["id"],
                                                 "type": "parameter", "value": parameter.get("min", 0)}, profiles)
                        self.assertEqual(len(result.messages), 1)


class ImportedMidiTests(unittest.TestCase):
    def test_cc_messages_preserve_channels_and_values_without_device_profiles(self):
        for message_hex, channel, controller, value in [("B0 02 00", 1, 2, 0),
                                                        ("bf\t7f\n00", 16, 127, 0),
                                                        ("B7 00 7F", 8, 0, 127)]:
            action = {"type": "raw_midi", "message_hex": message_hex}
            before = deepcopy(action)
            with self.subTest(message_hex=message_hex):
                result = compile_action(action, {})
                self.assertEqual(result.messages, (bytes.fromhex(message_hex),))
                self.assertEqual(result.summary, f"Imported MIDI: Channel {channel} / CC {controller} = {value}")
                self.assertEqual(result.evidence_status, "controller_read")
                self.assertEqual(result.warnings, ())
                self.assertEqual(action, before)

    def test_complete_sysex_preserves_exact_bytes_without_inferring_a_tank_action(self):
        for message_hex in (REV_MIX_10, RELOAD_CURRENT, "F0 41 01 02 7F F7"):
            action = {"type": "raw_midi", "message_hex": message_hex,
                      "evidence": {"status": "device_checked"}}
            with self.subTest(message_hex=message_hex):
                result = compile_action(action, {})
                self.assertEqual(result.messages, (bytes.fromhex(message_hex),))
                self.assertEqual(result.hex, message_hex)
                self.assertEqual(result.summary, f"Imported MIDI: SysEx ({len(bytes.fromhex(message_hex))} bytes)")
                self.assertEqual(result.evidence_status, "controller_read")
                self.assertTrue(any("device and effect model cannot be inferred" in warning for warning in result.warnings))
                self.assertNotIn("device_id", action)

    def test_raw_midi_rejects_semantic_targets_and_values_even_when_empty(self):
        action = {"type": "raw_midi", "message_hex": "B0 02 00"}
        for field in ("device_id", "block_id", "model_id", "param_id", "channel", "value"):
            for value in (None, "", 1):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValidationError, "does not accept"):
                    compile_action(dict(action, **{field: value}), {"tank": tank_fixture()})

    def test_raw_midi_rejects_bad_hex_non_cc_incomplete_frames_and_embedded_status_bytes(self):
        invalid = [None, 1, [], "", " ", "not hex", "B", "B0 02", "B0 02 00 01",
                   "B0 80 00", "BF 02 80", "C0 01", "90 3C 7F", "02 00",
                   "F0 41 00", "41 00 F7", "F0 41 80 F7", "F0 F0 01 F7",
                   "F0 01 F7 F7", "F0 01 F7 F0 02 F7"]
        for message_hex in invalid:
            with self.subTest(message_hex=message_hex), self.assertRaises(ValidationError):
                compile_action({"type": "raw_midi", "message_hex": message_hex}, {})

    def test_raw_sysex_limits_apply_to_each_message_and_complete_bank(self):
        # The controller's 128-byte area also holds the length byte.
        maximum = {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(125) + b"\xF7").hex(" ")}
        oversized = {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(126) + b"\xF7").hex(" ")}
        self.assertEqual(compile_state({"actions": [maximum]}, {}).byte_count, 127)
        with self.assertRaisesRegex(ValidationError, "128 bytes.*maximum is 127"):
            compile_action(oversized, {})
        with self.assertRaisesRegex(ValidationError, "limit is 126"):
            compile_state({"actions": [maximum]}, {}, max_sysex_bytes=126)
        frame = {"type": "raw_midi", "message_hex": (b"\xF0" + bytes(63) + b"\xF7").hex(" ")}
        with self.assertRaisesRegex(ValidationError, "130 SysEx bytes"):
            compile_state({"actions": [frame, frame]}, {})

    def test_raw_and_semantic_messages_reuse_bank_classification_and_preserve_order(self):
        profiles = {"tank": tank_fixture(), "generic": cc_fixture()}
        cc = {"type": "raw_midi", "message_hex": "BF 02 00"}
        sysex = {"type": "raw_midi", "message_hex": REV_MIX_10}
        state = compile_state({"actions": [cc, enabled(False, "delay", "generic")]}, profiles)
        self.assertEqual(state.messages, (b"\xBF\x02\x00", b"\xB0\x1A\x00"))
        self.assertEqual(state.midi_rows[0], {"channel": 16, "type": "CC", "data1": 2,
                                          "data2": 0, "hex": "BF 02 00"})
        self.assertEqual(state.evidence_status, "mixed")
        state = compile_state({"actions": [enabled(True), sysex]}, profiles)
        self.assertEqual(state.sysex_hex, FX_ON + "\n" + REV_MIX_10)
        self.assertEqual(state.byte_count, 43)
        self.assertEqual(state.midi_rows, ())
        for actions in ([cc, sysex], [cc, mix(10)], [sysex, enabled(True, "delay", "generic")]):
            with self.subTest(actions=actions), self.assertRaisesRegex(ValidationError, "cannot mix CC or Program Change with SysEx"):
                compile_state({"actions": actions}, profiles)

    def test_raw_action_after_model_write_does_not_require_invented_target_fields(self):
        profiles = {"tank": tank_fixture()}
        model = {"device_id": "tank", "block_id": "rev", "type": "model", "model_id": "hall"}
        raw = {"type": "raw_midi", "message_hex": "F0 41 01 F7"}
        state = compile_state({"actions": [model, raw]}, profiles)
        self.assertEqual(state.messages[-1], b"\xF0\x41\x01\xF7")
        with self.assertRaisesRegex(ValidationError, "asynchronous"):
            compile_state({"actions": [model, raw, mix(10)]}, profiles)

    def test_imported_project_json_and_export_retain_exact_messages_without_profiles(self):
        config = {"schema_version": 1, "name": "Imported controller",
                  "controller": {"id": "chocolate_plus", "max_sysex_bytes": 127},
                  "pages": [{"name": "Page 1", "switches": {
                      "A": {"label": "Imported CC", "mode": "alternating", "states": [
                          {"label": "Bank A", "actions": [{"type": "raw_midi", "message_hex": "B0 02 00"}]},
                          {"label": "Bank B", "actions": [{"type": "raw_midi", "message_hex": "BF 00 7F"}]}]},
                      "B": {"label": "Imported SysEx", "mode": "single", "states": [
                          {"actions": [{"type": "raw_midi", "message_hex": FX_ON},
                                       {"type": "raw_midi", "message_hex": REV_MIX_10}]}]}}}]}
        before = deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imported.json"
            save_config(path, config, {})
            loaded = load_config(path, {})
            self.assertEqual(loaded, before)
            exported = compile_config(loaded, {})
            switches = exported["pages"][0]["switches"]
            self.assertEqual(switches["A"]["states"][0]["messages_hex"], ["B0 02 00"])
            self.assertEqual(switches["A"]["states"][1]["messages_hex"], ["BF 00 7F"])
            self.assertEqual(switches["B"]["states"][0]["messages_hex"], [FX_ON, REV_MIX_10])
            self.assertEqual(switches["B"]["states"][0]["evidence_status"], "controller_read")
            json.dumps(exported)
        self.assertEqual(config, before)


class GenericMidiTests(unittest.TestCase):
    def test_commands_compile_without_profiles_and_keep_their_label(self):
        for message, description in (("BF 7F 00", "Channel 16 / CC 127 = 0"),
                                     ("C0 00", "Channel 1 / Program Change 0"),
                                     ("CF 7F", "Channel 16 / Program Change 127"),
                                     ("F0 41 00 7F F7", "SysEx (5 bytes)")):
            action = {"type": "midi", "message_hex": message, "label": "My pedal",
                      "evidence": {"status": "device_checked"}}
            before = deepcopy(action)
            with self.subTest(message=message):
                result = compile_action(action, {})
                self.assertEqual(result.messages, (bytes.fromhex(message),))
                self.assertEqual(result.summary, "My pedal: " + description)
                self.assertEqual(result.evidence_status, "user_defined")
                self.assertEqual(action, before)

    def test_invalid_frames_and_conflicting_semantic_fields_are_rejected(self):
        for message in ("", "C", "C0", "C0 80", "C0 01 02", "B0 01 80", "90 3C 7F",
                        "F0 41 80 F7", "F0 01", "F0 01 F7 F0 02 F7"):
            with self.subTest(message=message), self.assertRaises(ValidationError):
                compile_action({"type": "midi", "message_hex": message}, {})
        for field in ("device_id", "block_id", "model_id", "param_id", "channel", "value"):
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "does not accept"):
                compile_action({"type": "midi", "message_hex": "C0 00", field: None}, {})
        for label in (None, "", 12, []):
            with self.subTest(label=label), self.assertRaisesRegex(ValidationError, "midi.label"):
                compile_action({"type": "midi", "message_hex": "C0 00", "label": label}, {})

    def test_cc_and_program_change_keep_channel_type_and_order(self):
        commands = [{"type": "midi", "message_hex": message} for message in ("B3 00 02", "C3 7F", "BF 20 01")]
        result = compile_state({"actions": commands}, {})
        self.assertEqual(result.midi_rows, (
            {"channel": 4, "type": "CC", "data1": 0, "data2": 2, "hex": "B3 00 02"},
            {"channel": 4, "type": "PC", "data1": 127, "data2": 0, "hex": "C3 7F"},
            {"channel": 16, "type": "CC", "data1": 32, "data2": 1, "hex": "BF 20 01"}))
        self.assertEqual(result.evidence_status, "user_defined")

    def test_generic_commands_obey_sysex_bank_limits(self):
        sysex = {"type": "midi", "message_hex": (b"\xF0" + bytes(125) + b"\xF7").hex()}
        self.assertEqual(compile_state({"actions": [sysex]}, {}).byte_count, 127)
        with self.assertRaisesRegex(ValidationError, "cannot mix"):
            compile_state({"actions": [sysex, {"type": "midi", "message_hex": "C0 01"}]}, {})
        with self.assertRaisesRegex(ValidationError, "254 SysEx bytes"):
            compile_state({"actions": [sysex, sysex]}, {})
        with self.assertRaisesRegex(ValidationError, "maximum is 127"):
            compile_action({"type": "midi", "message_hex": (b"\xF0" + bytes(126) + b"\xF7").hex()}, {})

    def test_named_generic_project_round_trips_without_a_profile(self):
        config = {"schema_version": 1, "name": "My MIDI device", "pages": [{"name": "Page 1", "switches": {
            "A": {"mode": "single", "states": [{"actions": [
                {"type": "midi", "message_hex": "C1 03", "label": "Clean sound"}]}]}}}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generic.json"
            save_config(path, config, {})
            self.assertEqual(load_config(path, {}), config)
            result = compile_config(load_config(path, {}), {})
            state = result["pages"][0]["switches"]["A"]["states"][0]
            self.assertEqual(state["messages_hex"], ["C1 03"])
            self.assertEqual(state["summaries"], ["Clean sound: Channel 2 / Program Change 3"])


class ReloadPresetTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {"tank": reload_profile_fixture(), "generic": cc_fixture()}
        self.action = {"device_id": "tank", "type": "reload_current_preset"}

    def test_fixed_frame_writes_only_cached_index_and_contains_no_preset_number(self):
        result = compile_action(self.action, self.profiles)
        self.assertEqual(result.hex, RELOAD_CURRENT)
        self.assertEqual(result.byte_count, 21)
        self.assertEqual(len(result.messages), 1)
        decoded = parse_packet(result.messages[0])
        self.assertEqual(decoded["opcode"], 0x22)
        self.assertEqual(decoded["payload"], bytes.fromhex("04 01 00 00 20 01 00 00 FF"))
        self.assertEqual(decoded["raw"][-1], 0xDA)
        self.assertEqual(self.action, {"device_id": "tank", "type": "reload_current_preset"})

    def test_experimental_status_and_whole_preset_warnings_cannot_be_hidden(self):
        result = compile_action(dict(self.action, experimental=False, evidence={"status": "device_checked"}), self.profiles)
        self.assertEqual(result.evidence_status, "firmware_static_only")
        self.assertIn("Reload current preset (experimental)", result.summary)
        self.assertTrue(any("discards all temporary edits" in warning and "whole saved preset" in warning for warning in result.warnings))
        self.assertTrue(any("not yet verified" in warning for warning in result.warnings))
        self.assertTrue(any("V09" in warning and "normal mode" in warning and "tuner" in warning for warning in result.warnings))
        self.assertTrue(any("Does not save or overwrite" in warning for warning in result.warnings))
        self.assertIn("Profile-specific reload note.", result.warnings)

    def test_reload_requires_explicit_tank_capability_and_generic_cc_remains_unchanged(self):
        profiles = {"tank": tank_fixture(), "generic": cc_fixture()}
        with self.assertRaisesRegex(ValidationError, "capability must be declared"):
            compile_action(self.action, profiles)
        with self.assertRaisesRegex(ValidationError, "only available for tank_v09"):
            compile_action(dict(self.action, device_id="generic"), profiles)
        self.assertEqual(compile_action(enabled(True, "delay", "generic"), profiles).hex, "B0 1A 7F")

    def test_reload_is_independent_of_effect_values_models_channels_and_labels(self):
        profile = self.profiles["tank"]
        for block in profile["blocks"]:
            block["models"] = []
            block["label"] = "Different preset content"
        profile["name"] = "Another saved patch"
        for channel in range(1, 17):
            profile["channel"] = channel
            with self.subTest(channel=channel):
                self.assertEqual(compile_action(self.action, self.profiles).hex, RELOAD_CURRENT)
                self.assertEqual(compile_action(dict(self.action, channel=17 - channel), self.profiles).hex, RELOAD_CURRENT)
        for channel in (0, 17, True, "1"):
            with self.subTest(invalid_channel=channel), self.assertRaises(ValidationError):
                compile_action(dict(self.action, channel=channel), self.profiles)

    def test_reload_rejects_effect_targets_preset_numbers_and_raw_write_options(self):
        for field in ("block_id", "model_id", "param_id", "value", "preset", "preset_id",
                      "preset_index", "preset_number", "index", "address", "offset",
                      "memory_type", "opcode", "length", "data"):
            for value in (None, 0, "arbitrary"):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValidationError, "does not accept"):
                    compile_action(dict(self.action, **{field: value}), self.profiles)
        with self.assertRaisesRegex(ValidationError, "integer from 0 to 91"):
            tank_live_write(0x20000001, b"\xff")

    def test_reload_must_be_the_only_action_in_its_state(self):
        ordinary_actions = [enabled(False), mix(5), enabled(True, "delay", "generic"), self.action]
        for action in ordinary_actions:
            for actions in ([self.action, action], [action, self.action]):
                with self.subTest(other=action), self.assertRaisesRegex(ValidationError, "only action.*separate state"):
                    compile_state({"actions": actions}, self.profiles)
        state = compile_state({"actions": [self.action]}, self.profiles, max_sysex_bytes=21)
        self.assertEqual(state.sysex_hex, RELOAD_CURRENT)
        self.assertEqual(state.midi_rows, ())
        self.assertEqual(state.evidence_status, "firmware_static_only")
        with self.assertRaisesRegex(ValidationError, "limit is 20"):
            compile_state({"actions": [self.action]}, self.profiles, max_sysex_bytes=20)

    def test_capability_schema_rejects_wrong_protocol_unknown_ids_duplicates_and_bad_metadata(self):
        valid = reload_profile_fixture()
        self.assertIs(validate_profile(valid), valid)
        invalid = []
        for key, value in [("id", "unknown_device_action"), ("label", ""),
                           ("experimental", False), ("experimental", "true"),
                           ("evidence", {"status": "device_checked"}), ("evidence", None),
                           ("evidence", {"status": "firmware_static_only", "note": 1}),
                           ("warnings", "a warning"), ("warnings", [42])]:
            profile = deepcopy(valid)
            profile["device_actions"][0][key] = value
            invalid.append(profile)
        for value in (None, {}, "reload_current_preset", ["reload_current_preset"]):
            profile = deepcopy(valid)
            profile["device_actions"] = value
            invalid.append(profile)
        duplicate = deepcopy(valid)
        duplicate["device_actions"].append(deepcopy(duplicate["device_actions"][0]))
        invalid.append(duplicate)
        cc = cc_fixture()
        cc["device_actions"] = deepcopy(valid["device_actions"])
        invalid.append(cc)
        for index, profile in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValidationError):
                validate_profile(profile)

    def test_boost_on_reload_off_project_json_and_export_preserve_state_order(self):
        config = config_fixture()
        off = config["pages"][0]["switches"]["B"]["states"][1]
        off["label"] = "Restore saved preset"
        off["actions"] = [dict(self.action, note="No hardcoded OFF values")]
        before = deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boost-reload.json"
            save_config(path, config, self.profiles)
            self.assertEqual(load_config(path, self.profiles), before)
            exported = compile_config(config, self.profiles)
            on_export, off_export = exported["pages"][0]["switches"]["B"]["states"]
            self.assertEqual(on_export["messages_hex"], [FX_ON, REV_MIX_10])
            self.assertEqual(off_export["messages_hex"], [RELOAD_CURRENT])
            self.assertEqual(off_export["label"], "Restore saved preset")
            self.assertEqual(off_export["evidence_status"], "firmware_static_only")
        self.assertEqual(config, before)
        off["actions"].append(mix(5))
        with self.assertRaisesRegex(ValidationError, "switch B / state 2.*only action"):
            validate_config(config, self.profiles)

    def test_bundled_tank_capability_compiles_the_same_fixed_operation(self):
        profiles = load_profiles(Path(__file__).parent / "profiles")
        result = compile_action({"device_id": "tank_mini_v09", "type": "reload_current_preset"}, profiles)
        self.assertEqual(result.hex, RELOAD_CURRENT)
        self.assertEqual(result.evidence_status, "firmware_static_only")


def tank_write(address, data):
    return packet(0x22, b"\x04" + address.to_bytes(4, "little") + len(data).to_bytes(3, "little") + data)


class DecodeTests(unittest.TestCase):
    """A SysEx read from the controller is named only when a profile sends exactly those bytes."""

    def setUp(self):
        self.profiles = {"tank": reload_profile_fixture(), "generic": cc_fixture()}

    def test_tank_writes_decode_to_the_actions_that_produce_them(self):
        for action in (enabled(True), enabled(False), enabled(True, "mod"), mix(10), mix(5),
                       {"device_id": "tank", "type": "reload_current_preset"}):
            with self.subTest(action=action):
                message = compile_action(action, self.profiles).messages[0]
                self.assertEqual(decode_message(message, self.profiles), [action])

    def test_every_bundled_tank_action_decodes_to_actions_with_the_same_bytes(self):
        profiles = load_profiles(Path(__file__).parent / "profiles")
        device = "tank_mini_v09"
        actions = [{"device_id": device, "type": "reload_current_preset"}]
        for block in profiles[device]["blocks"]:
            actions += [{"device_id": device, "type": "enable", "block_id": block["id"], "value": value}
                        for value in (True, False)]
            for model in block["models"]:
                actions.append({"device_id": device, "type": "model", "block_id": block["id"],
                                "model_id": model["id"]})
                actions += [{"device_id": device, "type": "parameter", "block_id": block["id"],
                             "model_id": model["id"], "param_id": parameter["id"], "value": parameter.get("max", 100)}
                            for parameter in model["parameters"]]
        self.assertGreater(len(actions), 300)
        for action in actions:
            with self.subTest(action=action):
                message = compile_action(action, profiles).messages[0]
                decoded = decode_message(message, profiles)
                self.assertIn(action, decoded)
                for candidate in decoded:
                    self.assertEqual(compile_action(candidate, profiles).messages, (message,))
                if action["type"] != "parameter":
                    self.assertEqual(decoded, [action])

    def test_parameter_writes_name_the_block_and_slot_but_not_the_model(self):
        profiles = load_profiles(Path(__file__).parent / "profiles")
        depth = {"device_id": "tank_mini_v09", "type": "parameter", "block_id": "mod",
                 "model_id": "0", "param_id": "depth", "value": 30}
        decoded = decode_message(compile_action(depth, profiles).messages[0], profiles)
        mod = next(block for block in profiles["tank_mini_v09"]["blocks"] if block["id"] == "mod")
        self.assertEqual([action["model_id"] for action in decoded], [model["id"] for model in mod["models"]])
        self.assertEqual(describe_decoded(decoded, profiles),
                         "TANK Mini - stock firmware V09 · MOD / Depth, Dry or Min = 30 (depends on the preset's model)")
        decoded = decode_message(bytes.fromhex(REV_MIX_10), profiles)
        self.assertEqual(len(decoded), 6)
        self.assertEqual(describe_decoded(decoded, profiles), "TANK Mini - stock firmware V09 · REV / Mix = 10")
        decoded = decode_message(bytes.fromhex(FX_ON), profiles)
        self.assertEqual(describe_decoded(decoded, profiles), "TANK Mini - stock firmware V09 · FX: ON")

    def test_messages_that_no_profile_sends_are_not_decoded(self):
        raw = b"\x00\x59\x22" + (9).to_bytes(3, "little") + bytes.fromhex("04 08 00 00 10 01 00 00 01")
        bad_checksum = b"\xf0" + pack7(raw + bytes([(255 - sum(raw[6:])) & 255 ^ 1])) + b"\xf7"
        for name, message in [
            ("another manufacturer", bytes.fromhex("F0 41 01 02 7F F7")),
            ("wrong checksum", bad_checksum),
            ("read instead of write", packet(0x23, bytes.fromhex("04 08 00 00 10 01 00 00"))),
            ("unused live patch byte", tank_write(TANK_LIVE_PATCH + 2, b"\x01")),
            ("enable value 2", tank_write(TANK_LIVE_PATCH + 8, b"\x02")),
            ("block missing from the profile", tank_write(TANK_LIVE_PATCH + 9, b"\x01")),
            ("value above the maximum", tank_write(TANK_LIVE_PATCH + 70, (101).to_bytes(2, "little"))),
            ("one-byte parameter write", tank_write(TANK_LIVE_PATCH + 70, b"\x0a")),
            ("other memory address", tank_write(0x30000000, b"\x01")),
            ("CC from a CC profile", bytes.fromhex("B0 1A 7F")),
        ]:
            with self.subTest(name):
                self.assertEqual(decode_message(message, self.profiles), [])
        self.assertEqual(decode_message(bytes.fromhex(FX_ON), {"generic": cc_fixture()}), [])
        self.assertEqual(decode_message(bytes.fromhex(RELOAD_CURRENT), {"tank": tank_fixture()}), [])

    def test_registered_decoders_keep_only_actions_with_the_same_bytes(self):
        def encode(action, profile):
            return CompiledAction((bytes([0xF0, 0x7D, action["value"], 0xF7]),), "Registered", "documented")
        def propose(message, profile):
            return [{"type": "set", "value": 0}, {"type": "set", "value": message[2]}]
        profiles = {"extension": {"schema_version": 1, "id": "extension", "name": "Extension",
                                  "protocol": "test_decoder"}}
        try:
            register_protocol("test_decoder", encode, lambda profile: None, decoder=propose)
            self.assertEqual(decode_message(bytes.fromhex("F0 7D 05 F7"), profiles),
                             [{"device_id": "extension", "type": "set", "value": 5}])
            with self.assertRaisesRegex(ValidationError, "decoder"):
                register_protocol("test_decoder", encode, decoder="propose", replace=True)
            register_protocol("test_decoder", encode, replace=True)
            self.assertEqual(decode_message(bytes.fromhex("F0 7D 05 F7"), profiles), [])
        finally:
            PROTOCOL_ENCODERS.pop("test_decoder", None)
            PROFILE_VALIDATORS.pop("test_decoder", None)
            PROTOCOL_DECODERS.pop("test_decoder", None)



def preset_capabilities(protocol="tank_v09"):
    presets = {"id": "select_preset", "label": "Select preset", "presets": ["1A", "1B", "2B"],
               "evidence": {"status": "device_checked"}}
    volume = {"id": "preset_volume", "label": "Preset volume", "evidence": {"status": "device_checked"}}
    return [presets, volume] if protocol == "tank_v09" else [presets]


class PresetActionTests(unittest.TestCase):
    def setUp(self):
        tank, generic = tank_fixture(), cc_fixture()
        tank["device_actions"] = preset_capabilities()
        generic["device_actions"] = preset_capabilities("midi_cc")
        self.profiles = {"tank": tank, "generic": generic}

    def compile(self, **action):
        return compile_action(action, self.profiles)

    def test_select_preset_is_a_program_change_for_any_profile_that_lists_presets(self):
        result = self.compile(device_id="tank", type="select_preset", value=2)
        self.assertEqual(result.hex, "C0 02")
        self.assertEqual(result.summary, "TANK Mini V09: Select preset 2B")
        self.assertEqual(result.evidence_status, "device_checked")
        self.assertIn("unsaved changes on the device are discarded", result.warnings[0])
        self.assertEqual(self.compile(device_id="generic", type="select_preset", value=0, channel=16).hex, "CF 00")
        with self.assertRaisesRegex(ValidationError, "action.value: expected an integer from 0 to 2"):
            self.compile(device_id="tank", type="select_preset", value=3)

    def test_preset_volume_writes_the_live_patch_level_on_tank_profiles_only(self):
        # The bytes a TANK MINI_009 acknowledged and applied for volume 43 on 2026-10-16.
        self.assertEqual(self.compile(device_id="tank", type="preset_volume", value=43).hex,
                         "F0 00 32 09 51 00 00 00 02 00 00 00 00 21 00 00 00 2B 00 78 05 F7")
        result = self.compile(device_id="tank", type="preset_volume", value=100)
        self.assertEqual(result.messages, (tank_live_write(0, b"\x64\x00"),))
        self.assertEqual(result.summary, "TANK Mini V09: Preset volume = 100")
        for value in (-1, 101, True, 50.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, "action.value"):
                self.compile(device_id="tank", type="preset_volume", value=value)
        with self.assertRaisesRegex(ValidationError, "Preset volume is only available for tank_v09"):
            self.compile(device_id="generic", type="preset_volume", value=50)

    def test_device_actions_need_their_capability_and_reject_target_fields(self):
        del self.profiles["tank"]["device_actions"]
        for action_type, label in (("select_preset", "Select preset"), ("preset_volume", "Preset volume")):
            with self.subTest(action_type=action_type), self.assertRaisesRegex(ValidationError, f"{label} is unavailable"):
                self.compile(device_id="tank", type=action_type, value=1)
        for field in ("block_id", "preset", "address"):
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "does not accept target"):
                self.compile(device_id="generic", type="select_preset", value=1, **{field: "fx"})

    def test_capabilities_are_validated(self):
        for presets, reason in ((None, "expected a list"), ([], "1 to 128"), (["A"] * 129, "1 to 128"),
                                (["1A", ""], "nonempty text"), (["1A", "1A"], "unique")):
            profile = cc_fixture()
            profile["device_actions"] = [{"id": "select_preset", "label": "Select preset", "presets": presets}]
            with self.subTest(presets=presets), self.assertRaisesRegex(ValidationError, reason):
                validate_profile(profile)
        profile = cc_fixture()
        profile["device_actions"] = [{"id": "preset_volume", "label": "Preset volume"}]
        with self.assertRaisesRegex(ValidationError, "only available for tank_v09"):
            validate_profile(profile)

    def test_program_change_is_a_midi_row_that_can_join_cc_rows_but_not_sysex(self):
        select = {"device_id": "tank", "type": "select_preset", "value": 2}
        delay_on = {"device_id": "generic", "block_id": "delay", "type": "enable", "value": True}
        state = compile_state({"actions": [select, delay_on]}, self.profiles)
        self.assertEqual(state.midi_rows, ({"channel": 1, "type": "PC", "data1": 2, "data2": 0, "hex": "C0 02"},
                                           {"channel": 1, "type": "CC", "data1": 26, "data2": 127, "hex": "B0 1A 7F"}))
        self.assertEqual(state.sysex_hex, "")
        for actions in ([select, enabled(True)], [{"device_id": "tank", "type": "preset_volume", "value": 9}, select]):
            with self.subTest(actions=actions), self.assertRaisesRegex(ValidationError, "cannot mix CC or Program Change with SysEx"):
                compile_state({"actions": actions}, self.profiles)

    def test_preset_volume_decodes_and_program_changes_are_not_named(self):
        message = self.compile(device_id="tank", type="preset_volume", value=77).messages[0]
        actions = decode_message(message, self.profiles)
        self.assertEqual(actions, [{"device_id": "tank", "type": "preset_volume", "value": 77}])
        self.assertEqual(describe_decoded(actions, self.profiles), "TANK Mini V09: Preset volume = 77")
        self.assertEqual(decode_message(tank_live_write(0, b"\x65\x00"), self.profiles), [])
        # Like CC, a Program Change could be meant for any device.
        self.assertEqual(decode_message(bytes.fromhex("C0 02"), self.profiles), [])

    def test_bundled_tank_profile_lists_presets_1a_to_7c(self):
        profiles = load_profiles(Path(__file__).resolve().parent / "profiles")
        capability = next(item for item in profiles["tank_mini_v09"]["device_actions"] if item["id"] == "select_preset")
        self.assertEqual(len(capability["presets"]), 21)
        self.assertEqual(capability["presets"][4], "2B")
        result = compile_action({"device_id": "tank_mini_v09", "type": "select_preset", "value": 4}, profiles)
        self.assertEqual(result.hex, "C0 04")
        self.assertEqual(result.summary, "TANK Mini - stock firmware V09: Select preset 2B")



class ToggleTests(unittest.TestCase):
    def setUp(self):
        tank, generic = tank_fixture(), cc_fixture()
        tank["blocks"][0]["toggle"] = {"cc": 0, "value": 0, "evidence": {"status": "device_checked"}}
        generic["blocks"][0]["toggle"] = {"cc": 28}
        self.profiles = {"tank": tank, "generic": generic}

    def test_toggle_sends_the_block_cc_on_the_action_channel(self):
        result = compile_action({"device_id": "tank", "type": "toggle", "block_id": "fx"}, self.profiles)
        self.assertEqual(result.hex, "B0 00 00")
        self.assertEqual(result.summary, "TANK Mini V09 · FX: toggle ON/OFF (CC 0)")
        self.assertEqual(result.evidence_status, "device_checked")
        self.assertIn("does not set a fixed state", result.warnings[0])
        generic = compile_action({"device_id": "generic", "type": "toggle", "block_id": "delay", "channel": 3}, self.profiles)
        self.assertEqual(generic.hex, "B2 1C 00")
        state = compile_state({"actions": [{"device_id": "tank", "type": "toggle", "block_id": "fx"}]}, self.profiles)
        self.assertEqual(state.midi_rows[0]["hex"], "B0 00 00")

    def test_toggle_needs_the_block_command_and_names_a_given_model(self):
        with self.assertRaisesRegex(ValidationError, "toggle command is unavailable"):
            compile_action({"device_id": "tank", "type": "toggle", "block_id": "rev"}, self.profiles)
        result = compile_action({"device_id": "tank", "type": "toggle", "block_id": "fx", "model_id": "boost"}, self.profiles)
        self.assertIn("The preset must already have FX = Boost selected", result.warnings[1])

    def test_host_mode_example_uses_only_cc_and_program_change_rows(self):
        profiles = load_profiles(Path(__file__).resolve().parent / "profiles")
        example = Path(__file__).resolve().parent.parent / "examples" / "tank_mini_host_cc_pc.json"
        pages = compile_config(load_config(example, profiles), profiles)["pages"]
        sent = {switch: [state["messages_hex"] for state in data["states"]] for switch, data in pages[0]["switches"].items()}
        self.assertEqual(sent, {"A": [["B0 02 00"]], "B": [["B0 00 00"]], "C": [[]], "D": [["C0 04"]]})

    def test_toggle_definitions_are_validated(self):
        for toggle, reason in (([], "expected a JSON object"), ({"value": 0}, "toggle.cc"),
                               ({"cc": 128}, "toggle.cc"), ({"cc": 1, "value": -1}, "toggle.value")):
            profile = cc_fixture()
            profile["blocks"][0]["toggle"] = toggle
            with self.subTest(toggle=toggle), self.assertRaisesRegex(ValidationError, reason):
                validate_profile(profile)


if __name__ == "__main__":
    unittest.main()
