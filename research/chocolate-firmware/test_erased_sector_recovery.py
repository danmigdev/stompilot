"""Host-only guards for recovering a blank or partially restored fixed sector."""
import unittest

from restore_erased_sector import recovery_pages
from probe_sector_erase import baseline, RELATIVE, SECTOR_SIZE, PAGE_SIZE


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        _, self.backup = baseline()
        self.blank = bytearray(self.backup)
        self.blank[RELATIVE:RELATIVE + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE

    def test_blank_and_partially_restored_sector(self):
        pages = recovery_pages(self.blank, self.backup)
        self.assertEqual(len(pages), 16)
        offset, data = pages[0]
        self.blank[offset:offset + PAGE_SIZE] = data
        self.assertEqual(len(recovery_pages(self.blank, self.backup)), 15)
        self.assertEqual(recovery_pages(self.backup, self.backup), [])

    def test_unexpected_changes_are_rejected(self):
        self.blank[RELATIVE + 1] = 0x7f
        with self.assertRaises(ValueError):
            recovery_pages(self.blank, self.backup)
        self.blank[RELATIVE:RELATIVE + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE
        self.blank[RELATIVE - 1] ^= 1
        with self.assertRaises(ValueError):
            recovery_pages(self.blank, self.backup)
        with self.assertRaises(ValueError):
            recovery_pages(self.backup[:-1], self.backup)


if __name__ == "__main__":
    unittest.main()
