import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import prepare_lab


class PrepareLabTests(unittest.TestCase):
    def test_environment_is_allowlisted_and_npm_configs_are_distinct(self):
        synthetic_environment = {
            "ASSAY_SYNTHETIC_SECRET": "must-not-propagate",
            "DATABASE_URL": "postgresql://synthetic",
            "AWS_SECRET_ACCESS_KEY": "synthetic-secret",
        }

        with mock.patch.dict(os.environ, synthetic_environment, clear=False):
            child_environment = prepare_lab.environment()

        for name in synthetic_environment:
            self.assertNotIn(name, child_environment)
        self.assertEqual(child_environment["NPM_CONFIG_USERCONFIG"], os.devnull)
        self.assertNotEqual(
            child_environment["NPM_CONFIG_USERCONFIG"],
            child_environment["NPM_CONFIG_GLOBALCONFIG"],
        )
        self.assertEqual(child_environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(child_environment["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(child_environment["UV_NO_CONFIG"], "1")

    def test_require_free_rejects_an_occupied_port(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = OSError("already bound")

        with mock.patch.object(prepare_lab.socket, "socket", return_value=probe):
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                prepare_lab.require_free(55439)

        probe.bind.assert_called_once_with(("127.0.0.1", 55439))

    def test_stop_external_services_never_invokes_stop_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({"services": {"external": True}}))

            with (
                mock.patch.object(prepare_lab, "STATE", state_path),
                mock.patch.object(prepare_lab, "run") as runner,
                mock.patch.object(subprocess, "run") as subprocess_runner,
                mock.patch.object(prepare_lab.os, "kill") as kill,
            ):
                prepare_lab.stop_services()

        runner.assert_not_called()
        subprocess_runner.assert_not_called()
        kill.assert_not_called()

    def test_stop_pid_mismatch_never_kills_process(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "services": {
                            "mailpit_pid": 8123,
                            "mailpit_command": "/owned/mailpit --listen 127.0.0.1:58027",
                        }
                    }
                )
            )
            process_listing = SimpleNamespace(returncode=0, stdout="/other/program\n")

            with (
                mock.patch.object(prepare_lab, "STATE", state_path),
                mock.patch.object(
                    subprocess, "run", return_value=process_listing
                ) as subprocess_runner,
                mock.patch.object(prepare_lab.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to signal"):
                    prepare_lab.stop_services()

        subprocess_runner.assert_called_once()
        kill.assert_not_called()

    def test_stop_wrong_docker_identity_never_removes_container(self):
        cases = {
            "wrong id": {"Id": "different-id", "label": "owned"},
            "wrong label": {"Id": "container-id", "label": "different"},
        }

        for name, resource in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "services": {
                                "postgres_id": "container-id",
                                "identity": "owned",
                            }
                        }
                    )
                )
                inspection = [
                    {
                        "Id": resource["Id"],
                        "Config": {"Labels": {prepare_lab.LABEL: resource["label"]}},
                    }
                ]

                with (
                    mock.patch.object(prepare_lab, "STATE", state_path),
                    mock.patch.object(
                        prepare_lab,
                        "run",
                        return_value=(json.dumps(inspection), 0.0),
                    ) as runner,
                ):
                    with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                        prepare_lab.stop_services()

                commands = [call.args[0] for call in runner.call_args_list]
                self.assertEqual(len(commands), 1)
                self.assertEqual(commands[0][:2], ["docker", "inspect"])
                self.assertNotIn(
                    ["docker", "rm", "-f", "container-id"], commands
                )

    def test_bad_mailpit_checksum_never_opens_or_executes_archive(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b"not-a-mailpit-archive")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with (
                mock.patch.object(
                    prepare_lab.platform,
                    "system",
                    return_value="Linux",
                ),
                mock.patch.object(
                    prepare_lab.platform,
                    "machine",
                    return_value="x86_64",
                ),
                mock.patch.object(
                    prepare_lab.urllib.request,
                    "urlopen",
                    return_value=response,
                ),
                mock.patch.object(prepare_lab.tarfile, "open") as archive_open,
                mock.patch.object(prepare_lab.subprocess, "Popen") as popen,
                mock.patch.object(prepare_lab.subprocess, "run") as subprocess_run,
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    prepare_lab.prepare_mailpit(target)

                self.assertFalse((target / "mailpit").exists())

        archive_open.assert_not_called()
        popen.assert_not_called()
        subprocess_run.assert_not_called()

    def test_inspect_postgres_requires_exact_fixture_version(self):
        for observed_version, expected_error in ((180005, True), (180006, False)):
            with self.subTest(observed_version=observed_version):
                output = json.dumps({"server_version": observed_version})
                with mock.patch.object(
                    prepare_lab, "run", return_value=(output, 0.0)
                ) as runner:
                    if expected_error:
                        with self.assertRaisesRegex(RuntimeError, "PostgreSQL 18.6"):
                            prepare_lab.inspect_postgres(Path("/subject"))
                    else:
                        self.assertEqual(
                            prepare_lab.inspect_postgres(Path("/subject")),
                            {"server_version": 180006},
                        )

                command = runner.call_args.args[0]
                self.assertIn("connect_timeout=3", str(command[2]))

    def test_fresh_setup_records_partial_owned_subject_as_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "out" / "lab" / "state.json"

            def fail_during_fetch(argv, **kwargs):
                command = [str(argument) for argument in argv]
                if command[:2] == ["node", "--version"]:
                    return "v22.12.0\n", 0.0
                if command[:2] == ["git", "fetch"]:
                    raise RuntimeError("injected fetch failure")
                return "", 0.0

            with (
                mock.patch.object(prepare_lab, "STATE", state_path),
                mock.patch.object(prepare_lab.shutil, "which", return_value="tool"),
                mock.patch.object(prepare_lab, "run", side_effect=fail_during_fetch),
            ):
                with (
                    mock.patch("sys.argv", ["prepare_lab"]),
                    self.assertRaisesRegex(RuntimeError, "injected fetch failure"),
                ):
                    prepare_lab.main()

            state = json.loads(state_path.read_text())
            self.assertFalse(state["ready"])
            self.assertTrue(state["created_subject"])
            self.assertEqual(state["subject"], str(root / "out" / "lab" / "fullstack"))
            self.assertNotIn("services", state)
            self.assertEqual(len(state["patch_sha256"]), 64)

    def test_ready_is_written_only_after_prepared_subject_and_patch_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "prepared-subject"
            (subject / ".venv" / "bin").mkdir(parents=True)
            (subject / ".venv" / "bin" / "python").touch()
            (subject / "node_modules").mkdir()
            state_path = root / "state.json"

            def successful_run(argv, **kwargs):
                command = [str(argument) for argument in argv]
                if command[:2] == ["node", "--version"]:
                    return "v22.12.0\n", 0.0
                if command[:2] == ["git", "rev-parse"]:
                    return prepare_lab.REVISION + "\n", 0.0
                return "", 0.0

            with (
                mock.patch.object(prepare_lab, "STATE", state_path),
                mock.patch.object(prepare_lab.shutil, "which", return_value="tool"),
                mock.patch.object(prepare_lab, "run", side_effect=successful_run),
                mock.patch.object(prepare_lab, "prepare_services") as services,
                mock.patch.object(
                    prepare_lab,
                    "inspect_postgres",
                    return_value={"server_version": 180006},
                ),
            ):
                with mock.patch(
                    "sys.argv", ["prepare_lab", "--subject", str(subject)]
                ):
                    prepare_lab.main()

            state = json.loads(state_path.read_text())
            self.assertTrue(state["ready"])
            self.assertEqual(state["postgres"], {"server_version": 180006})
            self.assertEqual(
                state["patch_sha256"],
                prepare_lab.hashlib.sha256(
                    (prepare_lab.ROOT / "experiments/fullstack/repaired.patch").read_bytes()
                ).hexdigest(),
            )
            services.assert_called_once()

    def test_patch_digest_change_keeps_setup_unready(self):
        class FakeDigest:
            def __init__(self, digest):
                self.digest = digest

            def hexdigest(self):
                return self.digest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "prepared-subject"
            (subject / ".venv" / "bin").mkdir(parents=True)
            (subject / ".venv" / "bin" / "python").touch()
            (subject / "node_modules").mkdir()
            state_path = root / "state.json"

            def successful_run(argv, **kwargs):
                command = [str(argument) for argument in argv]
                if command[:2] == ["node", "--version"]:
                    return "v22.12.0\n", 0.0
                if command[:2] == ["git", "rev-parse"]:
                    return prepare_lab.REVISION + "\n", 0.0
                return "", 0.0

            with (
                mock.patch.object(prepare_lab, "STATE", state_path),
                mock.patch.object(prepare_lab.shutil, "which", return_value="tool"),
                mock.patch.object(prepare_lab, "run", side_effect=successful_run),
                mock.patch.object(prepare_lab, "prepare_services"),
                mock.patch.object(
                    prepare_lab,
                    "inspect_postgres",
                    return_value={"server_version": 180006},
                ),
                mock.patch.object(
                    prepare_lab.hashlib,
                    "sha256",
                    side_effect=[
                        FakeDigest("initial-digest"),
                        FakeDigest("changed-digest"),
                    ],
                ),
            ):
                with (
                    mock.patch(
                        "sys.argv", ["prepare_lab", "--subject", str(subject)]
                    ),
                    self.assertRaisesRegex(RuntimeError, "patch changed"),
                ):
                    prepare_lab.main()

            state = json.loads(state_path.read_text())
            self.assertFalse(state["ready"])
            self.assertEqual(state["patch_sha256"], "initial-digest")


if __name__ == "__main__":
    unittest.main()
