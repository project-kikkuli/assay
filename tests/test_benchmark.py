import random
import sqlite3
import unittest
from contextlib import closing

from assay.benchmark import BASELINE_QUERY, CANDIDATE_QUERY, measure_queries


class BenchmarkTests(unittest.TestCase):
    def test_rewrite_preserves_oldest_eligible_across_mixed_states(self):
        rng = random.Random(812)
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT, lease_until INTEGER)")
            for _ in range(100):
                db.execute("DELETE FROM jobs")
                rows = [(i, rng.choice(["queued", "leased", "done"]), rng.randrange(20)) for i in range(rng.randrange(30))]
                db.executemany("INSERT INTO jobs VALUES (?, ?, ?)", rows)
                now = rng.randrange(20)
                eligible = [row[0] for row in rows if row[1] == "queued" or (row[1] == "leased" and row[2] <= now)]
                expected = [(min(eligible),)] if eligible else []
                self.assertEqual(db.execute(BASELINE_QUERY, (now,)).fetchall(), expected)
                self.assertEqual(db.execute(CANDIDATE_QUERY, (now,)).fetchall(), expected)

    def test_measured_samples_and_work_are_present(self):
        reports = measure_queries(sizes=(50,), repeats=3)
        self.assertEqual(len(reports), 2)
        for result in reports:
            self.assertEqual(len(result["samples_ms"]), 3)
            self.assertGreater(result["work"]["sqlite_vm_steps"], 0)
            self.assertTrue(result["work"]["query_plan"])
            self.assertTrue(all(x >= 0 for x in result["samples_ms"]))


if __name__ == "__main__":
    unittest.main()
