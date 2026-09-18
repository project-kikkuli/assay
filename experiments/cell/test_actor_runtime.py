import shutil
import tempfile
import unittest
from pathlib import Path

from experiments.cell.actor_runtime import Actor, ActorError, ActorUnknown, _decode


ROOT = Path(__file__).parent


def view(actor="alice", superuser=False, owner="alice"):
    return {
        "actor_id": actor,
        "is_superuser": superuser,
        "items": {"item-1": {"id": "item-1", "owner_id": owner, "title": "old"}},
    }


class TransportTests(unittest.TestCase):
    def test_decode_rejects_duplicate_json_keys(self):
        with self.assertRaises(ValueError):
            _decode('{"request_id":"a","request_id":"b"}')

    def test_transport_does_not_authorize_semantically_wrong_proposal(self):
        candidate = ROOT / "candidates" / "healthy.py"
        with Actor(candidate, scratch=Path(tempfile.gettempdir()) / "cell-actor-transport-test") as actor:
            proposal = actor(
                {"op": "create", "fields": {"title": "x", "owner_id": "bob"}},
                view(owner="bob"),
            )
        self.assertEqual(
            proposal,
            {"op": "create", "target": None, "fields": {"title": "x", "owner_id": "bob"}},
        )

    def test_transport_exceptions_are_distinct_from_policy_rejection(self):
        self.assertTrue(issubclass(ActorUnknown, ActorError))


@unittest.skipUnless(shutil.which("docker"), "Docker is required for the persistent worker replay")
class PersistentActorTests(unittest.TestCase):
    def test_healthy_ten_request_persistent_replay(self):
        candidate = ROOT / "candidates" / "healthy.py"
        with Actor(candidate, scratch=Path(tempfile.gettempdir()) / "cell-actor-test") as actor:
            for index in range(10):
                command = {"op": "create", "fields": {"title": f"item-{index}"}}
                proposal = actor(command, view())
                self.assertEqual(
                    proposal,
                    {"op": "create", "target": None, "fields": command["fields"]},
                )
            self.assertEqual(len(actor.observations["calls"]), 10)
            self.assertTrue(actor.observations["container_controls"]["passed"])
        self.assertEqual(actor.observations["cleanup"]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
