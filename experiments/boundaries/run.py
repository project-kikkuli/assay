#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Observe real boundary mismatches; this intentionally can report application defects."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fullstack"))
import run as harness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("out/boundaries.json"))
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--enforce", action="store_true", help="exit 1 when an explicit application expectation is violated")
    parser.add_argument("--probe", choices=["data", "signup"], default="data")
    args = parser.parse_args()
    subject = args.subject.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject, text=True).strip()
    if revision != harness.REVISION:
        parser.error("wrong upstream revision")
    python = subject / ".venv/bin/python"
    node = shutil.which("node")
    dsn = os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres")
    evidence = {"revision": revision, "probe": args.probe, "source_sha256": harness.tree_identity(subject), "steps": [],
                "oracle_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.iterdir() if p.suffix in {".ts", ".mts", ".py"}},
                "expectations": (["Invalid user input returns 4xx rather than 500", "All 150 owned items are reachable through table pagination", "Stored titles cannot execute JavaScript"] if args.probe == "data" else ["Signup helper cannot return before registration succeeds", "Application-driven navigation follows registration acknowledgement"]),
                "limits": ["Explicit product expectations, not inferred specifications", "One dataset and one browser engine", "Authentication is supplied as a trusted fixture", "Host execution; not sandboxed"]}
    started = time.perf_counter()
    # Place only our ephemeral probe alongside generated client types for real typechecking.
    fd, probe_name = tempfile.mkstemp(prefix="assay_boundary_", suffix=".mts", dir=subject / "frontend")
    os.close(fd)
    probe = Path(probe_name)
    probe.write_text((HERE / ("probe.ts" if args.probe == "data" else "signup.mts")).read_text())
    try:
        with harness.database(dsn) as db, tempfile.TemporaryDirectory(prefix="assay-boundary-") as scratch:
            env = harness.clean_env() | {"DATABASE_URL": harness.database_url(db), "PYTHONPATH": str(subject / "backend"), "VITE_API_URL": ""}
            def execute(name, command, cwd):
                result = harness.run(command, cwd, env)
                output = result.pop("output")
                evidence["steps"].append({"check": name, **result, "output_tail": harness.sanitize(output[-2000:], subject)})
                if result["exit_code"] != 0:
                    raise RuntimeError(f"{name} failed: {harness.sanitize(output[-1000:], subject)}")
                return output
            execute("migration", [python, "-m", "alembic", "upgrade", "head"], subject / "backend")
            fixture = json.loads(execute("seed", [python, HERE / "seed.py"], subject / "backend"))
            # Tokens are fixture inputs, not useful evidence.
            evidence["steps"][-1]["output_tail"] = "150 synthetic items; fixture token omitted"
            execute("typescript_contract", [node, subject / "node_modules/typescript/bin/tsc", "--ignoreConfig", "--noEmit", "--target", "es2022", "--module", "preserve", "--moduleResolution", "bundler", "--types", "node", "--skipLibCheck", probe], subject / "frontend")
            execute("build", [node, subject / "node_modules/vite/bin/vite.js", "build"], subject / "frontend")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            env |= {"FRONTEND_HOST": base, "ASSAY_API": base, "ASSAY_BOUNDARY_FIXTURE": json.dumps(fixture)}
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path.home() / ("Library/Caches/ms-playwright" if platform.system() == "Darwin" else ".cache/ms-playwright"))
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                env["ASSAY_SCREENSHOT"] = str(args.screenshot.resolve())
            with (Path(scratch) / "server.log").open("w+") as log:
                server = subprocess.Popen([python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=subject / "backend", env=env, stdout=log, stderr=log, start_new_session=True)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with urllib.request.urlopen(base + "/api/v1/utils/health-check/", timeout=.5):
                                break
                        except OSError:
                            if server.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("server startup failed")
                            time.sleep(.05)
                    output = execute("wire_and_browser", [node, "--experimental-strip-types", probe], subject / "frontend")
                    evidence["observations"] = json.loads(output.splitlines()[-1])
                    observation = evidence["observations"]
                    evidence["claims"] = ({
                        "invalid_title_is_client_error": 400 <= observation["null_update"]["http_status"] < 500,
                        "invalid_offset_is_client_error": 400 <= observation["negative_offset"]["http_status"] < 500,
                        "all_owned_items_reachable": observation["pagination"]["all_items_reachable"],
                        "stored_title_does_not_execute": observation["title_rendering"]["safely_rendered"],
                    } if args.probe == "data" else {
                        "original_helper_waits_for_registration": not observation["old"]["returned_before_registration"],
                        "fixed_helper_waits_for_registration": observation["fixed"]["awaited_success"] and observation["fixed"]["actual_login"],
                    })
                    evidence["application_verdict"] = "supported" if all(evidence["claims"].values()) else "rejected"
                finally:
                    if server.poll() is None:
                        os.killpg(server.pid, signal.SIGTERM)
                        try:
                            server.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(server.pid, signal.SIGKILL)
                            server.wait()
    finally:
        probe.unlink(missing_ok=True)
        evidence["seconds"] = time.perf_counter() - started
        evidence["source_unchanged"] = evidence["source_sha256"] == harness.tree_identity(subject)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence.get("observations"), indent=2))
    # Successful observation is not an application pass.
    if not evidence["source_unchanged"] or not evidence.get("observations"):
        return 2
    return 1 if args.enforce and evidence["application_verdict"] == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
