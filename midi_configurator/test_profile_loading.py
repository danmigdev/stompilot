"""Portable profile loading checks using temporary bundled and custom folders."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from . import app
from .core import ValidationError


def cc_profile(profile_id, name):
    return {
        "schema_version": 1,
        "id": profile_id,
        "name": name,
        "protocol": "midi_cc",
        "blocks": [{
            "id": "effect",
            "label": "Effect",
            "enable": {"cc": 16, "on": 127, "off": 0},
            "models": [{"id": "current", "label": "Current effect", "parameters": []}],
        }],
    }


class ProfileLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bundled = root / "bundle" / "profiles"
        self.custom = root / "portable" / "profiles"
        self.bundled.mkdir(parents=True)
        self.builtin = cc_profile("builtin_device", "Bundled device")
        self.write_profile(self.bundled / "builtin.json", self.builtin)
        bundled_patch = patch.object(app, "BUNDLED_PROFILE_DIR", self.bundled)
        bundled_patch.start()
        self.addCleanup(bundled_patch.stop)
        custom_patch = patch.object(app, "PROFILE_DIR", self.custom)
        custom_patch.start()
        self.addCleanup(custom_patch.stop)

    @staticmethod
    def write_profile(path, profile):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile), encoding="utf-8")

    def test_missing_custom_directory_loads_bundled_profiles_without_creating_it(self):
        self.assertFalse(self.custom.exists())
        self.assertEqual(app.load_device_profiles(), {self.builtin["id"]: self.builtin})
        self.assertFalse(self.custom.exists())

    def test_empty_custom_directory_keeps_bundled_profiles(self):
        self.custom.mkdir(parents=True)
        self.assertEqual(app.load_device_profiles(), {self.builtin["id"]: self.builtin})

    def test_custom_cc_profile_is_merged_with_bundled_profiles(self):
        custom = cc_profile("custom_device", "Custom device")
        custom["blocks"][0]["enable"]["cc"] = 74
        self.write_profile(self.custom / "custom.json", custom)
        self.assertEqual(app.load_device_profiles(), {
            self.builtin["id"]: self.builtin,
            custom["id"]: custom,
        })
        self.assertEqual(json.loads((self.bundled / "builtin.json").read_text()), self.builtin)
        self.assertEqual(json.loads((self.custom / "custom.json").read_text()), custom)

    def test_custom_profiles_cannot_duplicate_a_bundled_id_or_name(self):
        for collision in ("id", "name"):
            with self.subTest(collision=collision):
                custom = cc_profile("custom_device", "Custom device")
                custom[collision] = self.builtin[collision]
                self.write_profile(self.custom / "custom.json", custom)
                with self.assertRaises(ValidationError):
                    app.load_device_profiles()

    def test_source_directory_loads_each_profile_once(self):
        custom = cc_profile("source_custom", "Source custom device")
        self.write_profile(self.bundled / "custom.json", custom)
        with patch.object(app, "PROFILE_DIR", self.bundled):
            self.assertEqual(app.load_device_profiles(), {
                self.builtin["id"]: self.builtin,
                custom["id"]: custom,
            })

    def test_source_directory_also_rejects_duplicate_ids_and_names(self):
        for collision in ("id", "name"):
            with self.subTest(collision=collision):
                other = deepcopy(self.builtin)
                other["id"], other["name"] = "source_custom", "Source custom device"
                other[collision] = self.builtin[collision]
                self.write_profile(self.bundled / "custom.json", other)
                with patch.object(app, "PROFILE_DIR", self.bundled), self.assertRaises(ValidationError):
                    app.load_device_profiles()


if __name__ == "__main__":
    unittest.main()
