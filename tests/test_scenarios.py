import unittest
from assay.scenarios import FENCING_CASE, explore, replay, shrink


class ScenarioTests(unittest.TestCase):
    def test_good_queue_rejects_stale_completion(self):
        result = replay(FENCING_CASE)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["events"][-1]["result"])

    def test_fault_injection_is_detected(self):
        result = replay(FENCING_CASE, unsafe=True)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["failures"][0]["rule"], "only-current-live-lease-may-complete")

    def test_semantics_replay_identically_despite_different_host_timing(self):
        first = replay(FENCING_CASE)
        second = replay(FENCING_CASE)
        self.assertEqual(first["semantic_digest"], second["semantic_digest"])

    def test_counterexample_shrinking(self):
        shrunk = shrink(FENCING_CASE)
        self.assertLess(len(shrunk), len(FENCING_CASE))
        self.assertEqual(replay(shrunk, unsafe=True)["status"], "rejected")
        self.assertEqual(replay(shrunk)["status"], "verified")

    def test_generated_schedules(self):
        result = explore(seeds=12, steps=30)
        self.assertEqual(result["status"], "verified")
        self.assertGreater(result["events_checked"], 200)


if __name__ == "__main__":
    unittest.main()
