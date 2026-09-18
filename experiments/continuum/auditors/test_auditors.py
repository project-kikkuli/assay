from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
RUST = HERE / "ledger.rs"
HASKELL = HERE / "admission.hs"
HASKELL_IMAGE = "haskell@sha256:2221ee2d1e2e7c91a9a4cee9a62bc06db89e6a7a70d126419a67e4590811c1b1"


class AuditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="assay-auditors-")
        cls.rust_binary = Path(cls.tempdir.name) / "ledger"
        result = subprocess.run(
            ["rustc", "--edition=2021", "-O", str(RUST), "-o", str(cls.rust_binary)],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AssertionError(f"rustc failed: {result.stderr[-2000:]}")
        cls.docker = shutil.which("docker")
        if cls.docker:
            result = subprocess.run(
                [
                    cls.docker,
                    "run",
                    "--network",
                    "none",
                    "--rm",
                    "-v",
                    f"{HERE}:/src:ro",
                    "-v",
                    f"{cls.tempdir.name}:/out",
                    HASKELL_IMAGE,
                    "ghc",
                    "-O1",
                    "-outputdir",
                    "/out",
                    "-o",
                    "/out/admission",
                    "/src/admission.hs",
                ],
                text=True,
                capture_output=True,
                timeout=120,
            )
            if result.returncode:
                raise AssertionError(f"Docker GHC failed: {result.stderr[-2000:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def run_rust(self, text: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [str(self.rust_binary)], input=text, text=True, capture_output=True
        )
        return result, json.loads(result.stdout)

    def test_rust_valid_and_independent_invariants(self) -> None:
        result, report = self.run_rust(
            "\n".join(
                [
                    "reservation\talpha\tr1\t3\tapproved",
                    "reservation\talpha\tr2\t2\tfulfilled",
                    "reservation\tbeta\tr3\t8\treserved",
                    "job\talpha\tr1\td1\tpending",
                    "job\talpha\tr2\td2\tdone",
                ]
            )
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report, {"passed": True, "violations": [], "reservations": 3, "jobs": 2})

    def test_rust_reports_cross_record_violations(self) -> None:
        result, report = self.run_rust(
            "\n".join(
                [
                    "reservation\talpha\tr1\t8\tapproved",
                    "reservation\talpha\tr1\t8\tfulfilled",
                    "reservation\talpha\tr2\t1\treserved",
                    "job\tbeta\tr1\td1\tpending",
                    "job\talpha\tr2\td1\tdone",
                ]
            )
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["passed"])
        self.assertGreaterEqual(len(report["violations"]), 5)

    def test_rust_malformed_is_nonzero_and_strict(self) -> None:
        result, report = self.run_rust("reservation\talpha\tr1\t9\treserved\textra\n")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(report["passed"])
        self.assertIn("line 1", report["violations"][0])

    def run_haskell(self, text: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [
                str(self.docker),
                "run",
                "--network",
                "none",
                "--rm",
                "-i",
                "-v",
                f"{self.tempdir.name}:/out:ro",
                HASKELL_IMAGE,
                "/out/admission",
            ],
            input=text,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return result, json.loads(result.stdout)

    @unittest.skipUnless(shutil.which("docker"), "Docker is unavailable; Haskell image test is explicitly skipped")
    def test_haskell_policy_matrix(self) -> None:
        cases = {
            "v1\tvalid\thealthy\tcompatible\tnone\n": "promote",
            "v1\tvalid\tregression\tcompatible\tapprove\n": "rollback",
            "v1\tvalid\tregression\tincompatible\tapprove\n": "contain_and_escalate",
            "v1\tvalid\tsecurity\tcompatible\tapprove\n": "contain_and_escalate",
            "v1\tvalid\tunknown\tcompatible\tapprove\n": "investigate",
            "v1\tinvalid\thealthy\tcompatible\tapprove\n": "reject",
            "v1\tvalid\thealthy\tcompatible\treject\n": "reject",
        }
        for text, decision in cases.items():
            result, report = self.run_haskell(text)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(report["decision"], decision)

    @unittest.skipUnless(shutil.which("docker"), "Docker is unavailable; Haskell image test is explicitly skipped")
    def test_haskell_malformed_is_nonzero(self) -> None:
        result, report = self.run_haskell("v1\tvalid\thealthy\tcompatible\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
