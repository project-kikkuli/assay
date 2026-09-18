import tempfile
import unittest
from pathlib import Path

from admission import ACTOR, REQUIRED, decide, digest, inventory


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.base = {name: digest(name.encode()) for name in REQUIRED}

    def test_actor_change_is_not_itself_a_green_result(self):
        candidate = self.base | {ACTOR: digest(b"new")}
        self.assertEqual(decide(self.base, candidate)["lane"],
                         "actor_checks_required")

    def test_every_protected_change_routes_broader(self):
        for candidate in (self.base | {"kernel.py": digest(b"evil")},
                          self.base | {".new-config": digest(b"new")},
                          {k: v for k, v in self.base.items() if k != "worker.py"}):
            self.assertEqual(decide(self.base, candidate)["lane"],
                             "broader_required")

    def test_missing_inputs_never_fall_back_to_green(self):
        for base, candidate in (({}, self.base), (self.base, {}),
                                (self.base, {"kernel.py": "anything"})):
            self.assertEqual(decide(base, candidate)["lane"], "broader_required")

    def test_incomplete_baseline_and_malformed_digests_are_rejected(self):
        self.assertEqual(decide({ACTOR: digest(b"x")}, {ACTOR: digest(b"y")})["lane"],
                         "broader_required")
        self.assertEqual(decide(self.base, self.base | {ACTOR: "not a hash"})["lane"],
                         "broader_required")

    def test_actor_name_cannot_be_changed_by_caller(self):
        with self.assertRaises(TypeError):
            decide(self.base, self.base, "kernel.py")

    def test_inventory_includes_hidden_new_files_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                inventory(root)
            (root / ".config").write_text("x")
            self.assertEqual(inventory(root), {".config": digest(b"x")})
            (root / "link").symlink_to(root / ".config")
            with self.assertRaises(ValueError):
                inventory(root)


if __name__ == "__main__":
    unittest.main()
