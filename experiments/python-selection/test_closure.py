import tempfile
import unittest
from pathlib import Path

from closure import action_key, inventory, reusable


class ClosureTests(unittest.TestCase):
    def test_membership_content_mode_and_absence_are_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            initial = inventory(root, ["assets", "config"])
            target = root / "assets/icon"
            target.write_bytes(b"one")
            added = inventory(root, ["assets", "config"])
            self.assertNotEqual(initial, added)
            target.write_bytes(b"two")
            changed = inventory(root, ["assets", "config"])
            self.assertNotEqual(added, changed)
            target.chmod(0o755)
            self.assertNotEqual(changed, inventory(root, ["assets", "config"]))
            target.unlink()
            self.assertEqual(initial, inventory(root, ["assets", "config"]))
            (root / "config").write_text("created")
            self.assertNotEqual(initial, inventory(root, ["assets", "config"]))

    def test_links_and_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to(root, target_is_directory=True)
            for name in ("link", "link/missing", "../outside", "/outside", ""):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    inventory(root, [name])

    def test_command_environment_and_verifier_invalidate(self):
        initial = action_key({}, ["test"], {"runtime": "v1"}, "oracle1")
        self.assertNotEqual(initial, action_key({}, ["other"], {"runtime": "v1"}, "oracle1"))
        self.assertNotEqual(initial, action_key({}, ["test"], {"runtime": "v2"}, "oracle1"))
        self.assertNotEqual(initial, action_key({}, ["test"], {"runtime": "v1"}, "oracle2"))

    def test_internal_file_link_binds_target_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("one")
            (root / "link").symlink_to(target)
            before = inventory(root, ["link"])
            target.write_text("two")
            self.assertNotEqual(before, inventory(root, ["link"]))

    def test_missing_failed_partial_and_wrong_receipts_never_reuse(self):
        valid = dict(key="key", status="passed", passed=41, failed=0, skipped=0)
        self.assertTrue(reusable(valid, "key", 41))
        for bad in (None, {}, valid | {"status": "unknown"}, valid | {"failed": 1},
                    valid | {"skipped": 1}, valid | {"passed": 0},
                    valid | {"passed": True}, valid | {"key": "other"}):
            self.assertFalse(reusable(bad, "key", 41))


if __name__ == "__main__":
    unittest.main()
