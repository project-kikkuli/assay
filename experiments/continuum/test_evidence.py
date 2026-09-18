import copy
import json
from pathlib import Path
import tempfile
import unittest

from evidence import Authority, InvalidEvidence, Store, admit, canonical, digest, expected_keys, run_graph, tree


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.authority = Authority(Path(self.tmp.name) / "keys")
        self.store = Store(Path(self.tmp.name) / "cache", self.authority)

    def action(self, value="one", environment="v1"):
        return {"inputs": {"source": value}, "recipe": "trusted-check-v1", "environment": environment,
                "run": lambda _: {"passed": True, "observed": value}}

    def test_exact_inputs_reuse_and_changes_invalidate(self):
        first = run_graph({"behavior": self.action()}, self.store)
        self.assertEqual(run_graph({"behavior": self.action()}, self.store)["cache_hits"], 1)
        self.assertEqual(run_graph({"behavior": self.action("two")}, self.store)["cache_hits"], 0)
        self.assertEqual(run_graph({"behavior": self.action(environment="v2")}, self.store)["cache_hits"], 0)
        self.assertTrue(first["passed"])

    def test_tampered_local_pass_not_trusted(self):
        receipt = run_graph({"behavior": self.action()}, self.store)["actions"]["behavior"]["receipt"]
        forged = copy.deepcopy(receipt)
        forged["payload"]["result"]["observed"] = "invented"
        with self.assertRaises(InvalidEvidence):
            self.authority.verify(forged)

    def test_attacker_key_not_trusted(self):
        other = Authority(Path(self.tmp.name) / "other-key")
        with self.assertRaises(InvalidEvidence):
            self.authority.verify(other.sign({"passed": True}))

    def test_public_key_only_verification(self):
        receipt = self.authority.sign({"artifact": "one"})
        public_only = Path(self.tmp.name) / "consumer"
        public_only.mkdir()
        (public_only / "issuer.pub").write_bytes(self.authority.public.read_bytes())
        consumer = Authority(public_only, readonly=True)
        self.assertEqual(consumer.verify(receipt), {"artifact": "one"})
        self.assertFalse(consumer.private.exists())

    def test_failed_actions_block_downstream(self):
        action = self.action()
        action["run"] = lambda _: {"passed": False}
        child = {**self.action(), "deps": ["parent"]}
        result = run_graph({"parent": action, "child": child}, self.store, jobs=2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["actions"]["child"]["receipt"]["payload"]["result"]["kind"], "blocked_by_dependency")

    def test_unrelated_action_can_be_reused(self):
        run_graph({"a": self.action(), "b": self.action()}, self.store)
        result = run_graph({"a": self.action("changed"), "b": self.action()}, self.store)
        self.assertEqual(result["cache_hits"], 1)

    def test_admission_rejects_missing_stale_and_empty_policy(self):
        package = {**self.action(), "run": lambda _: {"passed": True, "artifact": "bytes"}}
        graph = run_graph({"behavior": self.action(), "package": package}, self.store)
        receipts = {name: item["receipt"] for name, item in graph["actions"].items()}
        required = {name: item["payload"]["action_key"] for name, item in receipts.items()}
        args = dict(authority=self.authority, artifact="bytes", environment="v1", policy="p1")
        self.assertEqual(admit(receipts, required, **args)["decision"], "admit")
        self.assertEqual(admit({}, required, **args)["decision"], "reject")
        self.assertEqual(admit(receipts, {"behavior": digest("other tree")}, **args)["decision"], "reject")
        self.assertEqual(admit(receipts, {}, **args)["decision"], "reject")
        self.assertEqual(admit(receipts, required, **{**args, "artifact": "other-bytes"})["decision"], "reject")
        self.assertEqual(admit(receipts, required, **args, artifact_action=None)["decision"], "reject")

    def test_expected_keys_are_derived_from_target_spec(self):
        original = self.action()
        graph = run_graph({"behavior": original}, self.store)
        receipts = {name: item["receipt"] for name, item in graph["actions"].items()}
        target = self.action("changed-target")
        expected = digest({"name": "behavior", "inputs": target["inputs"],
                           "recipe": target["recipe"], "environment": target["environment"],
                           "dependencies": {}})
        required = expected_keys({"behavior": target}, receipts)
        self.assertEqual(required, {"behavior": expected})
        args = dict(authority=self.authority, artifact="bytes", environment="v1", policy="p1")
        self.assertEqual(admit(receipts, required, **args)["decision"], "reject")

    def test_dependency_result_digest_invalidates_child_cache(self):
        parent = self.action()
        child = {**self.action(), "deps": ["parent"],
                 "run": lambda inputs: {"passed": True, "seen": inputs["parent"]["observed"]}}
        run_graph({"parent": parent, "child": child}, self.store)
        parent_path = self.store.root / f"{digest({
            'name': 'parent', 'inputs': parent['inputs'], 'recipe': parent['recipe'],
            'environment': parent['environment'], 'dependencies': {}
        })}.json"
        receipt = json.loads(parent_path.read_text())
        receipt["payload"]["result"] = {"passed": True, "observed": "changed"}
        receipt["payload"]["result_digest"] = digest(receipt["payload"]["result"])
        parent_path.write_bytes(canonical(self.authority.sign(receipt["payload"])))

        rerun = run_graph({"parent": parent, "child": child}, self.store)
        self.assertEqual(rerun["cache_hits"], 1)
        self.assertEqual(rerun["actions"]["child"]["receipt"]["payload"]["result"]["seen"], "changed")

    def test_symlinks_are_not_hidden_inputs(self):
        root = Path(self.tmp.name) / "source"
        root.mkdir()
        (root / "hidden").symlink_to(self.authority.public)
        with self.assertRaises(ValueError):
            tree(root)


if __name__ == "__main__":
    unittest.main()
