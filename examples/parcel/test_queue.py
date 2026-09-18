import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from pathlib import Path

from examples.parcel.queue import Lease, Queue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "jobs.sqlite")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_basic_lifecycle_and_snapshot(self):
        with Queue(self.path) as queue:
            job_id = queue.enqueue("a", "payload")
            lease = queue.claim("worker", 10, 5)
            self.assertEqual(lease, Lease(job_id, "a", "payload", "worker", 1, 15))
            self.assertTrue(queue.complete(job_id, "worker", 1, 14))
            self.assertFalse(queue.complete(job_id, "worker", 1, 14))
            self.assertEqual(queue.snapshot()[0]["status"], "done")

    def test_restart_persistence(self):
        first = Queue(self.path)
        job_id = first.enqueue("persisted", "x")
        first.close()
        second = Queue(self.path)
        self.assertEqual(second.claim("w", 0).job_id, job_id)
        second.close()

    def test_duplicate_keys(self):
        queue = Queue(self.path)
        self.assertEqual(queue.enqueue("same", "x"), queue.enqueue("same", "x"))
        with self.assertRaises(ValueError):
            queue.enqueue("same", "y")
        queue.close()

    def test_stale_token_same_owner_is_rejected(self):
        queue = Queue(self.path)
        job_id = queue.enqueue("same-owner", "x")
        first = queue.claim("worker", 0, 1)
        second = queue.claim("worker", 1, 1)
        self.assertFalse(queue.complete(job_id, "worker", first.token, 1))
        self.assertTrue(queue.complete(job_id, "worker", second.token, 1))
        queue.close()

    def test_unsafe_fencing_is_explicitly_unsafe(self):
        queue = Queue(self.path, unsafe_fencing=True)
        job_id = queue.enqueue("unsafe", "x")
        first = queue.claim("worker", 0, 1)
        queue.claim("worker", 1, 1)
        self.assertTrue(queue.complete(job_id, "worker", first.token, 1))
        queue.close()

    def test_expiry_boundary(self):
        queue = Queue(self.path)
        job_id = queue.enqueue("boundary", "x")
        lease = queue.claim("w", 0, 2)
        self.assertFalse(queue.complete(job_id, "w", lease.token, 2))
        replacement = queue.claim("w2", 2, 2)
        self.assertEqual(replacement.token, 2)
        queue.close()

    def test_two_connections_compete(self):
        one, two = Queue(self.path), Queue(self.path)
        job_id = one.enqueue("one", "x")
        first = one.claim("one-worker", 0)
        self.assertEqual(first.job_id, job_id)
        self.assertIsNone(two.claim("two-worker", 0))
        one.close()
        two.close()

    def test_concurrent_connections_claim_once(self):
        with Queue(self.path) as queue:
            queue.enqueue("contended", "x")
        barrier = threading.Barrier(4, timeout=5)

        def claim(index):
            with Queue(self.path) as queue:
                barrier.wait()
                return queue.claim(f"worker-{index}", 0)

        with ThreadPoolExecutor(max_workers=4) as pool:
            leases = list(pool.map(claim, range(4)))
        self.assertEqual(sum(lease is not None for lease in leases), 1)

    def test_invalid_inputs(self):
        queue = Queue(self.path)
        for key in ("", "   ", "bad\x00key", 1):
            with self.assertRaises(ValueError):
                queue.enqueue(key, "x")
        for owner in ("", " ", "bad\x00owner", 1):
            with self.assertRaises(ValueError):
                queue.claim(owner, 0)
        queue.enqueue("valid", "x")
        for now in (True, "0"):
            with self.assertRaises(ValueError):
                queue.claim("w", now)
        for lease_for in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                queue.claim("w", 0, lease_for)
        queue.close()


if __name__ == "__main__":
    unittest.main()
