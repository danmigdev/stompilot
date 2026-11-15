"""TANK Mini check against a simulated TANK: live patch, model defaults and reload."""
import copy
from types import SimpleNamespace
import unittest

from .core import TANK_LIVE_PATCH, compile_action, load_profiles, packet, parse_packet
from .runtime_paths import resource_dir
from .tank_check import (DEVICE_ID, FLASH_BANK, STATE, Checker, TankError, UsbTank,
                         describe_changes, open_tank, show)


PROFILES = load_profiles(resource_dir() / "profiles")
PROFILE = PROFILES[DEVICE_ID]
# A live patch read from a TANK MINI_009 (preset 7B): FX Boost off, MOD Chorus off.
LIVE = bytes.fromhex(
    "4c000001050203040001000001010103000301031f00000000000000000000006400420031002700320000000300"
    "3200320000000000000048003200160017001f00270020000a00270026002f000000420000006400000000000000")
PRESET = 19
# Each saved preset differs in its volume, so a selection shows which one loaded.
BANK = b"".join(LIVE if index == PRESET else bytes([60 + index]) + LIVE[1:] for index in range(21)) + bytes(10)


class FakeMessage:
    def __init__(self, wire):
        self.wire = bytes(wire)
        self.type = "sysex" if self.wire[0] == 0xF0 else "control_change"

    @classmethod
    def from_bytes(cls, wire):
        return cls(wire)

    def bytes(self):
        return list(self.wire)


class FakeInput:
    def __init__(self):
        self.pending = []

    def iter_pending(self):
        messages, self.pending = self.pending, []
        return iter(messages)

    def close(self):
        pass


class FakeTank:
    """Answers like TANK MINI_009 and loads model defaults from the bundled profile."""

    def __init__(self):
        self.name = b"TANK MINI_009"
        self.live = bytearray(LIVE)
        self.state = bytearray([PRESET, PRESET, 0, 0])
        self.ram_bank = bytearray(BANK)
        self.flash_bank = bytearray(self.ram_bank)
        self.input = FakeInput()
        self.sent = []
        self.other_writes = []
        self.lose_ack = False
        self.ignore_reload = False
        self.misplace = {}  # parameter offset -> offset where the firmware stores it

    def send(self, message):
        wire = bytes(message.bytes())
        self.sent.append(wire)
        if wire[0] == 0xC0:
            self.state[0] = self.state[1] = wire[1]
            self.live[:] = self.ram_bank[wire[1] * 92:(wire[1] + 1) * 92]
            return
        if wire[0] == 0xB0:
            block = next((b for b in PROFILE["blocks"] if b["toggle"]["cc"] == wire[1]), None)
            if block and wire[2] == block["toggle"]["value"]:
                self.live[8 + block["wire_id"]] ^= 1
            return
        request = parse_packet(wire)
        opcode, payload = request["opcode"], request["payload"]
        if opcode == 0x11:
            self.reply(0x11, self.name + bytes(16 - len(self.name)) + bytes(8))
        elif opcode == 0x23:
            self.reply(0x23, payload + self.region(payload[0], int.from_bytes(payload[1:5], "little"),
                                                    int.from_bytes(payload[5:8], "little")))
        elif opcode == 0x22:
            self.write(int.from_bytes(payload[1:5], "little"), payload[8:])
            if not self.lose_ack:
                self.reply(0, b"\x00")

    def region(self, memory_type, address, count):
        for kind, start, data in ((4, TANK_LIVE_PATCH, self.live), (4, STATE, self.state),
                                  (4, 0, self.ram_bank), (5, FLASH_BANK, self.flash_bank)):
            if kind == memory_type and start <= address and address + count <= start + len(data):
                return bytes(data[address - start:address - start + count])
        raise AssertionError(f"read outside known memory: {memory_type} 0x{address:08X}")

    def write(self, address, data):
        offset = address - TANK_LIVE_PATCH
        if 0 <= offset and offset + len(data) <= 92:
            if len(data) == 2:
                offset = self.misplace.get(offset, offset)
            self.live[offset:offset + len(data)] = data
            if len(data) == 1 and 14 <= offset < 20:
                block = next(b for b in PROFILE["blocks"] if b["wire_id"] == offset - 14)
                model = next(m for m in block["models"] if m["wire_value"] == data[0])
                for parameter in model["parameters"]:
                    self.live[parameter["patch_offset"]:parameter["patch_offset"] + 2] = parameter["default"].to_bytes(2, "little")
        elif address == STATE + 1 and data == b"\xff":
            self.state[1] = 0xFF
            if not self.ignore_reload:
                self.live[:] = self.ram_bank[self.state[0] * 92:(self.state[0] + 1) * 92]
                self.state[1] = self.state[0]
        else:
            self.other_writes.append((address, data))

    def reply(self, opcode, payload):
        self.input.pending.append(FakeMessage(packet(opcode, payload)))

    def close(self):
        pass

    def module(self, chocolate=None):
        return SimpleNamespace(
            Message=FakeMessage,
            get_input_names=lambda: ["FootCtrlPlus 0", "USB-Midi 1"],
            get_output_names=lambda: ["Microsoft GS Wavetable Synth 0", "FootCtrlPlus 1", "USB-Midi 2"],
            open_input=lambda name: self.input if name.startswith("USB-Midi") else chocolate,
            open_output=lambda name: self)


class TankCheckTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeTank()
        self.output = []
        self.connect()

    def connect(self, profiles=PROFILES):
        self.tank, self.name = open_tank(PROFILE, self.device.module(), ack_timeout=0)
        self.checker = Checker(self.tank, profiles, out=self.output.append, sleep=lambda _: None)

    def assert_untouched(self):
        self.assertEqual(bytes(self.device.live), LIVE)
        self.assertEqual(self.device.state[0], PRESET)
        self.assertEqual(bytes(self.device.ram_bank), BANK)
        self.assertEqual(self.device.flash_bank, self.device.ram_bank)
        self.assertEqual(self.device.other_writes, [])

    def test_memory_checks_every_command_with_the_bytes_stompilot_writes_and_restores_the_patch(self):
        report = self.checker.memory()
        self.assertTrue(report["ok"], report["failed"])
        self.assertEqual(report["notes"], [])
        self.assertTrue(all(report["integrity"].values()))
        self.assert_untouched()
        checks = {result["check"]: result for result in report["checks"]}
        tremolo = compile_action({"device_id": DEVICE_ID, "type": "enable", "block_id": "mod", "value": True}, PROFILES)
        self.assertEqual(checks["MOD: ON"]["messages_hex"], [tremolo.hex])
        self.assertEqual(checks["MOD: ON"]["actual"], ["MOD: OFF -> ON (Chorus)"])
        self.assertEqual(checks["MOD / Tremolo / Speed = 100"]["actual"], ["MOD / Speed: 0 -> 100"])
        self.assertEqual(checks["MOD: model Tremolo"]["actual"][0], "MOD model: Phaser -> Tremolo")
        self.assertEqual(checks["MOD: stock CC 2 toggle"]["messages_hex"], ["B0 02 00"])
        self.assertEqual(checks["Reload current preset"]["actual"], ["FX / Gate: 0 -> 31"])
        self.assertEqual(checks["Preset volume = 36"]["actual"], ["Preset volume: 76 -> 36"])
        self.assertEqual(checks["Select preset 7C"]["messages_hex"], ["C0 14"])
        self.assertEqual(checks["Select preset 7C"]["actual"], ["Preset volume: 76 -> 80"])
        self.assertEqual(checks["Select preset 7B"]["actual"], ["Preset volume: 80 -> 76"])
        self.assertIn("ok   Select preset: 7C and back to 7B, each loaded exactly as saved", self.output)
        models = sum(len(block["models"]) for block in PROFILE["blocks"])
        parameters = sum(len(model["parameters"]) for block in PROFILE["blocks"] for model in block["models"])
        self.assertEqual(len([c for c in checks if ": model " in c]), models)
        self.assertEqual(len([r for r in report["checks"] if " / " in r["check"] and " = " in r["check"]]), 3 * parameters)
        self.assertIn("ok   MOD: ON/OFF, 7 models, 72 parameter writes", self.output)

    def test_a_parameter_stored_elsewhere_fails_and_the_patch_is_still_restored(self):
        self.device.misplace = {44: 46}
        report = self.checker.memory()
        self.assertFalse(report["ok"])
        self.assertIn("MOD / Tremolo / Speed = 100", report["failed"])
        self.assertTrue(all(name.startswith("MOD / ") and "/ Speed = " in name for name in report["failed"]))
        failure = next(r for r in report["checks"] if r["check"] == "MOD / Tremolo / Speed = 100")
        self.assertEqual(failure["expected"], ["MOD / Speed: 39 -> 100"])
        self.assertEqual(failure["actual"], ["MOD / Depth: 0 -> 100"])
        self.assertTrue(report["integrity"]["live_patch_restored"])

    def test_defaults_that_differ_from_the_profile_are_notes_not_failures(self):
        profiles = copy.deepcopy(PROFILES)
        profiles[DEVICE_ID]["blocks"][2]["models"][2]["parameters"][0]["default"] = 41
        self.connect(profiles)
        report = self.checker.memory()
        self.assertTrue(report["ok"])
        self.assertEqual(report["notes"], ["MOD / Tremolo defaults: Speed 41 in the profile, 39 loaded"])

    def test_lost_acknowledgements_are_decided_by_the_readback(self):
        self.device.lose_ack = True
        report = self.checker.memory()
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"][2]["ack"], [None])

    def test_a_reload_that_does_nothing_fails(self):
        self.device.ignore_reload = True
        report = self.checker.memory()
        self.assertEqual(report["failed"], ["Reload current preset"])
        self.assert_untouched()

    def test_another_firmware_or_no_tank_is_refused(self):
        self.device.name = b"TANK MINI_010"
        with self.assertRaisesRegex(TankError, "reports 'TANK MINI_010'; this profile is for TANK MINI_009"):
            open_tank(PROFILE, self.device.module())
        midi = SimpleNamespace(get_input_names=lambda: ["FootCtrlPlus 0"], get_output_names=lambda: ["FootCtrlPlus 1"])
        with self.assertRaisesRegex(TankError, "found 0 USB-Midi ports"):
            UsbTank.connect(midi)

    def test_show_names_each_block_and_the_unsaved_changes(self):
        self.device.live[10] = 1
        report = show(self.tank, self.name, PROFILE, out=self.output.append)
        self.assertEqual(self.output[:2], ["TANK MINI_009, preset 7B", "  Volume 76"])
        self.assertIn("  MOD  ON   Chorus: Speed 3, Depth 50, Mix 50", self.output)
        self.assertEqual(report["unsaved"], ["MOD: OFF -> ON (Chorus)"])

    def test_relay_forwards_each_press_and_says_which_model_turned_on(self):
        chocolate = FakeInput()
        on = compile_action({"device_id": DEVICE_ID, "type": "enable", "block_id": "mod", "value": True}, PROFILES)
        reload = compile_action({"device_id": DEVICE_ID, "type": "reload_current_preset"}, PROFILES)
        presses = [on.messages, reload.messages, (bytes.fromhex("B0 07 40"),), (bytes.fromhex("C0 04"),)]
        self.tank = UsbTank.connect(self.device.module(chocolate), ack_timeout=0)
        self.checker = Checker(self.tank, PROFILES, out=self.output.append, sleep=lambda _: None)

        def stop():
            if presses:
                chocolate.pending = [FakeMessage(message) for message in presses.pop(0)]
            return not chocolate.pending
        report = self.checker.relay(chocolate, stop=stop)
        first, second, third, fourth = report["presses"]
        self.assertEqual(first["received"], ["TANK Mini - stock firmware V09 · MOD: ON"])
        self.assertEqual(first["changes"], ["MOD: OFF -> ON (Chorus)"])
        self.assertTrue(first["ok"])
        self.assertEqual(second["changes"], ["MOD: ON -> OFF (Chorus)"])
        self.assertTrue(second["ok"])
        self.assertIsNone(third["ok"])
        self.assertEqual(fourth["received"], ["Program Change 4 (TANK preset 2B)"])
        self.assertEqual(fourth["changes"], ["Preset 7B -> 2B", "loaded exactly as saved"])
        self.assertTrue(fourth["ok"])
        self.assertTrue(report["ok"])
        # The old live patch belongs to 7B, so it is not written over 2B.
        self.assertIn("The TANK is now on preset 2B; its live patch is left as loaded.", self.output)
        self.assertEqual(bytes(self.device.live), BANK[4 * 92:5 * 92])
        self.assertEqual(self.device.other_writes, [])

    def test_listen_plays_quarter_and_three_quarter_values_and_restores_the_patch(self):
        answers = iter(["r", "y", "y", "n", "x", "s"])
        report = self.checker.listen("mod", ["2"], ask=lambda prompt: next(answers), interval=0)
        self.assertEqual(report["heard"], [{"block": "MOD", "model": "Tremolo", "heard": "y",
                                            "parameters": {"Speed": "y", "Depth": "n", "Level": "s"}}])
        self.assertEqual(report["not_heard"], ["MOD / Tremolo / Depth"])
        speed = [parse_packet(m)["payload"][8:] for m in self.device.sent
                 if m[0] == 0xF0 and parse_packet(m)["payload"][:5] == b"\x04" + (TANK_LIVE_PATCH + 44).to_bytes(4, "little")]
        self.assertEqual(speed[:6], [b"\x19\x00", b"\x4b\x00"] * 3)
        self.assert_untouched()

    def test_changes_outside_the_blocks_are_reported_by_byte(self):
        after = bytearray(LIVE)
        after[0] = 80
        after[3] = 9
        after[84:86] = (180).to_bytes(2, "little")
        self.assertEqual(describe_changes(LIVE, bytes(after), PROFILE),
                         ["Preset volume: 76 -> 80", "CAB / Hcut: 100 -> 180", "byte 3: 1 -> 9"])


if __name__ == "__main__":
    unittest.main()
