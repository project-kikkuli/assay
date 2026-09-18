#!/usr/bin/env python3
"""Prepare the pinned actor image and production UI; not part of gate timing."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from actor_runtime import IMAGE_DIGESTS, IMAGE_TAG, _safe_env

ROOT = Path(__file__).resolve().parents[2]
SUBJECT = ROOT / "out/lab/fullstack"


def main():
    output = ROOT / "out/cell/preparation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "unknown", "steps": []}
    output.write_text(json.dumps(report) + "\n")
    started = time.perf_counter()
    try:
        if not (SUBJECT / ".venv/bin/python").is_file() or not (SUBJECT / "node_modules").is_dir():
            raise RuntimeError("run ./lab prepare first")
        machine = platform.machine().lower()
        arch = {"arm64": "aarch64", "aarch64": "aarch64", "amd64": "x86_64", "x86_64": "x86_64"}.get(machine)
        if arch not in IMAGE_DIGESTS:
            raise RuntimeError("unsupported machine architecture")
        image = IMAGE_TAG + "@" + IMAGE_DIGESTS[arch]
        stages = [
            ("actor_image", ["docker", "pull", image], ROOT, _safe_env(), 180),
            ("production_ui", ["node", str(SUBJECT / "node_modules/vite/bin/vite.js"), "build"],
             SUBJECT / "frontend", {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                    "VITE_API_URL": "", "LANG": "C"}, 180),
        ]
        for name, command, cwd, env, timeout in stages:
            step_started = time.perf_counter()
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
            step = {"name": name, "exit_code": result.returncode,
                    "seconds": time.perf_counter() - step_started,
                    "output_sha256": hashlib.sha256(result.stdout + result.stderr).hexdigest()}
            report["steps"].append(step)
            print(json.dumps(step), flush=True)
            if result.returncode:
                raise RuntimeError(name + " failed")
        report.update(status="prepared", actor_image=image)
    except Exception as error:
        report.update(status="unknown", failure=type(error).__name__)
        print("cell preparation failed; check ./lab prepare, Docker, and Node 22.12.0", flush=True)
    finally:
        report["seconds"] = time.perf_counter() - started
        output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "prepared" else 1


if __name__ == "__main__":
    raise SystemExit(main())
