import unittest

from state_machine import ModelFailure, exercise_sequence, validate_patch


class FakeError(Exception):
    def __init__(self, status):
        self.status = status


class FakeKernel:
    def __init__(self):
        self.rows = {}
        self.counter = 0

    def create(self, owner, title, description=None):
        self.counter += 1
        item_id = f"id-{self.counter:03d}"
        row = {"id": item_id, "title": title, "description": description,
               "owner_id": str(owner), "created_at": f"{self.counter:04d}"}
        self.rows[item_id] = row
        return dict(row)

    def _owned(self, owner, item_id):
        row = self.rows.get(item_id)
        if row is None:
            raise FakeError(404)
        if row["owner_id"] != str(owner):
            raise FakeError(403)
        return row

    def read(self, owner, item_id):
        return dict(self._owned(owner, item_id))

    def update(self, owner, item_id, patch):
        row = self._owned(owner, item_id)
        row.update(patch)
        return dict(row)

    def delete(self, owner, item_id):
        self._owned(owner, item_id)
        del self.rows[item_id]
        return {"message": "Item deleted successfully"}

    def list(self, owner, skip, limit):
        rows = [row for row in self.rows.values() if row["owner_id"] == str(owner)]
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return {"data": [dict(row) for row in rows[skip:skip + limit]], "count": len(rows)}

    def observe(self):
        return [dict(row) for row in sorted(self.rows.values(), key=lambda row: row["id"])]


class BadPatchKernel(FakeKernel):
    def update(self, owner, item_id, patch):
        row = super().update(owner, item_id, patch)
        if "description" in patch:
            self.rows[item_id]["description"] = "wrong-persisted-value"
            row = dict(self.rows[item_id])
        return row


class StateMachineTests(unittest.TestCase):
    def test_healthy_seeded_sequence_passes(self):
        kernel = FakeKernel()
        report = exercise_sequence(kernel, "alice", "bob", kernel.observe, seed=17, steps=24)
        self.assertTrue(report["passed"])
        self.assertEqual(report["steps"], 24)
        self.assertGreaterEqual(report["creates"], 10)
        self.assertGreaterEqual(report["foreign_denials"], 3)

    def test_model_rejects_bad_partial_update(self):
        kernel = BadPatchKernel()
        with self.assertRaises(ModelFailure):
            exercise_sequence(kernel, "alice", "bob", kernel.observe, seed=17, steps=24)

    def test_model_rejects_invalid_patch_without_kernel(self):
        with self.assertRaises(ModelFailure):
            validate_patch({"title": None})


if __name__ == "__main__":
    unittest.main()
