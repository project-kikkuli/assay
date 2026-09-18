from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import capsule as continuum_capsule
import run as continuum_run
from evidence import Authority, Store, admit, digest, expected_keys, run_graph, tree


class RunnerFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuum-runner-test-")
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source"
        self.output = Path(self.tmp.name) / "out"
        self._write_fixture()

    def _write(self, relative, contents):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def _write_fixture(self):
        self._write("app/api.py", "APP_VALUE = 1\n")
        self._write("client/src/client.ts", "export const client = 1;\n")
        self._write("client/dist/client.js", "compiled-v1\n")
        self._write("client/package.json", '{"name":"tiny-client"}\n')
        self._write("client/node_modules/tiny/index.js", "module.exports = 1;\n")
        self._write("client/node_modules/tiny/package.json", '{"name":"tiny"}\n')
        self._write("client/node_modules/@playwright/test/package.json", '{"name":"@playwright/test"}\n')
        self._write("auditors/ledger.rs", "fn main() {}\n")
        for name in ("run.py", "capsule.py", "evidence.py", "verify.py", "contract.json", "browser.mjs", "edge.py"):
            self._write(name, f"# tiny {name}\n")
        self._write("verifier/verify.py", "VERIFIER = 1\n")
        self._write("verifier/contract.json", '{"version":1}\n')
        self._write("edge.py", "EDGE = 1\n")
        self._write("browser.mjs", "console.log('browser');\n")

    @contextmanager
    def _patched_runner(self):
        executor_source = {
            name: continuum_run.file_hash(self.source / name)
            for name in continuum_run.CONTROL_FILES
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(continuum_run, "ROOT", self.source))
            stack.enter_context(patch.object(continuum_run, "OUT", self.output))
            stack.enter_context(patch.object(continuum_run, "EXECUTOR_SOURCE", executor_source))
            stack.enter_context(patch.object(continuum_run, "command", return_value="tool-version"))
            yield

    def test_freeze_rejects_verifier_compiled_and_dependency_overrides(self):
        targets = {
            "verifier/verify.py": "OVERRIDE = True\n",
            "client/dist/client.js": "changed compiled bytes\n",
            "client/node_modules/tiny/index.js": "changed dependency\n",
        }
        with self._patched_runner():
            for target, replacement in targets.items():
                with self.subTest(target=target), self.assertRaises(ValueError):
                    continuum_run.freeze({target: replacement})

    def test_make_actions_binds_frozen_verifier_controls(self):
        with self._patched_runner():
            frozen = continuum_run.freeze()
            actions = continuum_run.make_actions(frozen, "current")

        controls = tree(frozen / "verifier")
        self.assertEqual(actions["package"]["inputs"]["controls"], controls)
        self.assertEqual(actions["behavior"]["inputs"]["controls"], controls)
        self.assertEqual(actions["package"]["inputs"]["contract"], continuum_run.file_hash(frozen / "verifier/contract.json"))

    def test_verifier_control_change_is_not_hidden_by_frozen_action_inputs(self):
        with self._patched_runner():
            frozen = continuum_run.freeze()
            actions = continuum_run.make_actions(frozen, "current")
            original_controls = actions["package"]["inputs"]["controls"]
            (frozen / "verifier/verify.py").write_text("VERIFIER = 2\n")
            changed_controls = tree(frozen / "verifier")

        self.assertNotEqual(original_controls, changed_controls)

    def test_make_actions_rejects_executor_source_drift(self):
        with self._patched_runner():
            self._write("capsule.py", "executor drift\n")
            with self.assertRaisesRegex(ValueError, "executor source changed"):
                continuum_run.make_actions(self.source, "current")

    def test_load_verifier_executes_bytes_without_changing_verifier_tree(self):
        before = tree(self.source / "verifier")
        verifier = continuum_run.load_verifier(self.source)
        after = tree(self.source / "verifier")

        self.assertEqual(verifier.VERIFIER, 1)
        self.assertEqual(before, after)
        self.assertFalse((self.source / "verifier/__pycache__").exists())

    def test_changed_compiled_bytes_reject_package_binding(self):
        with self._patched_runner():
            actions = continuum_run.make_actions(self.source, "current")
            actions["client"]["run"] = lambda _inputs: {
                "passed": True,
                "outputs": {"client.js": "a" * 64, "worker.js": "b" * 64},
            }
            selected = {name: actions[name] for name in ("client", "package")}
            authority = Authority(Path(self.tmp.name) / "keys")
            store = Store(Path(self.tmp.name) / "receipts", authority)
            graph = run_graph(selected, store, jobs=1, reuse=False)
            self.assertTrue(graph["passed"])
            receipts = {name: item["receipt"] for name, item in graph["actions"].items()}
            required = expected_keys(selected, receipts)
            environment = continuum_run.ENVIRONMENTS["current"]
            policy = digest({name: action["recipe"] for name, action in selected.items()})
            before = continuum_run.artifact_digest(self.source)
            self.assertEqual(admit(receipts, required, authority, artifact=before,
                                    environment=environment, policy=policy)["decision"], "admit")

            (self.source / "client/dist/client.js").write_text("compiled-v2\n")
            after = continuum_run.artifact_digest(self.source)
            decision = admit(receipts, required, authority, artifact=after,
                             environment=environment, policy=policy)

        self.assertNotEqual(before, after)
        self.assertEqual(decision["decision"], "reject")
        self.assertIn("deployed bytes differ from verified package", decision["reasons"])


class ProcessCleanupTests(unittest.TestCase):
    def test_run_process_names_and_removes_only_its_timed_out_docker_run(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            if args[:2] == ["docker", "run"]:
                raise continuum_capsule.subprocess.TimeoutExpired(args, 1)
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch.object(continuum_capsule.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(continuum_capsule.subprocess.TimeoutExpired):
                continuum_capsule.run_process(["docker", "run", "image", "command"], timeout=1)

        run_args, _ = calls[0]
        self.assertIn("--name", run_args)
        owned = run_args[run_args.index("--name") + 1]
        self.assertTrue(owned.startswith("assay-continuum-action-"))
        self.assertEqual(calls[1][0], ["docker", "rm", "-f", owned])

    def test_run_process_does_not_remove_a_caller_named_container(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            raise continuum_capsule.subprocess.TimeoutExpired(args, 1)

        with patch.object(continuum_capsule.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(continuum_capsule.subprocess.TimeoutExpired):
                continuum_capsule.run_process(["docker", "run", "--name", "foreign", "image"], timeout=1)

        self.assertEqual(calls, [["docker", "run", "--name", "foreign", "image"]])

    def test_world_close_attempts_all_containers_and_network_after_timeout(self):
        with tempfile.TemporaryDirectory(prefix="continuum-world-test-") as temporary:
            world = continuum_capsule.World(Path(temporary), {}, provider=False)
            world.containers = ["first", "second"]
            calls = []

            def fake_run(args, **kwargs):
                calls.append(list(args))
                if args == ["docker", "rm", "-f", "second"]:
                    raise continuum_capsule.subprocess.TimeoutExpired(args, 15)
                if args == ["docker", "rm", "-f", "first"]:
                    return SimpleNamespace(returncode=1, stderr="still present")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(continuum_capsule.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "first|second"):
                    world.close()

            self.assertIn(["docker", "rm", "-f", "second"], calls)
            self.assertIn(["docker", "rm", "-f", "first"], calls)
            self.assertIn(["docker", "network", "rm", world.network], calls)


if __name__ == "__main__":
    unittest.main()
