#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Measure a pinned upstream app; keep verification policy outside candidates."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REVISION = "cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7"
UPSTREAM = "https://github.com/fastapi/full-stack-fastapi-template"
HERE = Path(__file__).resolve().parent


def clean_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PROJECT_NAME": "Assay public fixture",
        "SECRET_KEY": "public-fixture-key-not-for-deployment",
        "FIRST_SUPERUSER": "admin@example.com",
        "FIRST_SUPERUSER_PASSWORD": "AssayFixturePassword123",
        "FASTAPI_ENV": "development",
        "NO_COLOR": "1",
    }


def sanitize(text: str, subject: Path) -> str:
    for path in {str(subject), str(subject.resolve()), str(HERE)}:
        text = text.replace(path, "$SOURCE")
    text = re.sub(r"/(?:private/)?tmp/[^/\s\"']+", "$SCRATCH", text)
    text = re.sub(r"/(?:private/)?var/folders/[^/]+/[^/]+/T/[^/\s\"']+", "$SCRATCH", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<fixture-token>", text)
    return re.sub(r"/Users/[^/\s]+", "$USER_HOME", text)


def run(command, cwd, env, timeout=180):
    start = time.perf_counter()
    process = subprocess.Popen(
        list(map(str, command)), cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        code = None
    return {"seconds": time.perf_counter() - start, "exit_code": code,
            "status": "passed" if code == 0 else "failed" if code is not None else "unresolved",
            "output": output}


@contextmanager
def database(admin_dsn):
    """Only drop the UUID-named database this invocation successfully created."""
    params = conninfo_to_dict(admin_dsn)
    if params.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This experiment only accepts a local disposable PostgreSQL server")
    name = "assay_fullstack_" + uuid.uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(admin_dsn, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def database_url(dsn):
    from urllib.parse import quote
    p = conninfo_to_dict(dsn)
    return (f"postgresql://{quote(p.get('user', 'postgres'), safe='')}:"
            f"{quote(p.get('password', ''), safe='')}@{p['host']}:"
            f"{p.get('port', '5432')}/{p['dbname']}")


def junit_counts(path):
    if not path.exists():
        return {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    cases = list(ET.parse(path).getroot().iter("testcase"))
    result = {"collected": len(cases), "failed": 0, "skipped": 0, "errors": 0}
    for case in cases:
        for tag, key in [("failure", "failed"), ("skipped", "skipped"), ("error", "errors")]:
            result[key] += case.find(tag) is not None
    result["passed"] = len(cases) - sum(result[k] for k in ("failed", "skipped", "errors"))
    return result


def tree_identity(root):
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    digest = hashlib.sha256()
    for raw in sorted(p for p in paths if p):
        path = root / os.fsdecode(raw)
        digest.update(raw + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def verifier_identity():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(HERE.iterdir()) if p.suffix in {".py", ".json", ".ts"}}


def artifact_identity(root):
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    if not paths:
        raise ValueError("built frontend artifact is missing or empty")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class Lab:
    subject: Path
    admin_dsn: str
    node: str
    python: Path
    records: list

    def record(self, name, result, **extra):
        output = sanitize(result.pop("output"), self.subject)
        record = {"check": name, **extra, **result,
                  "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                  "output_tail": output[-5000:]}
        self.records.append(record)
        print(json.dumps({k: record[k] for k in ("check", "status", "seconds")}), flush=True)
        return record

    def backend(self, candidate, kind="upstream", label="baseline"):
        with database(self.admin_dsn) as dsn, tempfile.TemporaryDirectory(prefix="assay-junit-") as scratch:
            env = clean_env() | {"DATABASE_URL": database_url(dsn), "PYTHONPATH": str(candidate / "backend")}
            migration = self.record("migration", run([self.python, "-m", "alembic", "upgrade", "head"], candidate / "backend", env), candidate=label)
            if migration["status"] != "passed":
                return migration
            target = "tests" if kind == "upstream" else HERE / "oracle_tests.py"
            report = Path(scratch) / "junit.xml"
            result = run([self.python, "-m", "pytest", str(target), "-q", "--junitxml", report], candidate / "backend", env)
            counts = junit_counts(report)
            if result["status"] == "passed" and (counts["passed"] == 0 or counts["errors"] or counts["failed"]):
                result["status"] = "unresolved"
            return self.record(kind, result, candidate=label, tests=counts)

    def frontend(self, candidate, label="baseline", build=False):
        modules = self.subject / "node_modules"
        command = ([self.node, modules / "vite/bin/vite.js", "build"] if build else
                   [self.node, modules / "typescript/bin/tsc", "-p", "tsconfig.build.json", "--noEmit"])
        env = clean_env() | {"VITE_API_URL": ""}
        return self.record("build" if build else "typecheck", run(command, candidate / "frontend", env), candidate=label)

    def python_static(self):
        for name, command in [
            ("mypy", [self.python, "-m", "mypy", "app", "--no-incremental"]),
            ("ty", [self.subject / ".venv/bin/ty", "check", "app"]),
            ("ruff", [self.python, "-m", "ruff", "check", "app"]),
            ("python_format", [self.python, "-m", "ruff", "format", "app", "--check"]),
        ]:
            self.record(name, run(command, self.subject / "backend", clean_env()), candidate="baseline")

    def faults(self):
        baseline = [self.backend(self.subject), self.backend(self.subject, "oracle"), self.frontend(self.subject)]
        if any(r["status"] != "passed" for r in baseline):
            raise RuntimeError("A green baseline is required to interpret mutation results")
        matrix = []
        with tempfile.TemporaryDirectory(prefix="assay-candidates-") as temp:
            root = Path(temp).resolve()
            archive = root / "source.tar"
            subprocess.run(["git", "archive", "--output", str(archive), REVISION], cwd=self.subject, check=True)
            candidate = root / "candidate"
            candidate.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(candidate, filter="data")
            (candidate / "node_modules").symlink_to(self.subject / "node_modules", target_is_directory=True)
            for fault in json.loads((HERE / "faults.json").read_text()):
                path = (candidate / fault["path"]).resolve()
                if not path.is_relative_to(candidate):
                    raise ValueError("fault path escapes candidate")
                before = path.read_text()
                if before.count(fault["before"]) != 1:
                    raise ValueError(f"{fault['id']}: preimage must occur exactly once")
                after = before.replace(fault["before"], fault["after"], 1)
                path.write_text(after)
                try:
                    outcomes = {}
                    for kind in ("upstream", "oracle"):
                        rec = self.backend(candidate, kind, fault["id"])
                        counts = rec.get("tests", {})
                        outcomes[kind] = ("survived" if rec["status"] == "passed" else
                            "caught" if counts.get("failed", 0) > 0 and counts.get("errors", 0) == 0 else "unresolved")
                    rec = self.frontend(candidate, fault["id"])
                    outcomes["typecheck"] = "survived" if rec["status"] == "passed" else "caught" if rec["exit_code"] else "unresolved"
                    matrix.append({"fault": fault["id"], "path": fault["path"], "claim": fault["claim"],
                                   "mutated_file_sha256": hashlib.sha256(after.encode()).hexdigest(), **outcomes})
                finally:
                    path.write_text(before)
        return matrix

    def browser(self, workers):
        with database(self.admin_dsn) as dsn, tempfile.TemporaryDirectory(prefix="assay-browser-") as scratch:
            # A socket reservation chooses a free port; uvicorn bind failure remains an explicit setup failure.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            env = clean_env() | {"DATABASE_URL": database_url(dsn), "FRONTEND_HOST": base,
                "PLAYWRIGHT_BASE_URL": base, "VITE_API_URL": base, "MAILPIT_HOST": os.environ.get("ASSAY_MAIL_HTTP", "http://127.0.0.1:58027"),
                "SMTP_HOST": "127.0.0.1", "SMTP_PORT": os.environ.get("ASSAY_MAIL_PORT", "51027"), "SMTP_TLS": "false", "EMAILS_FROM_EMAIL": "hello@example.com"}
            # Playwright needs its normal browser cache, but the tested app receives no host credentials.
            cache = Path.home() / ("Library/Caches/ms-playwright" if platform.system() == "Darwin" else ".cache/ms-playwright")
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
            for name, command in [("migration", [self.python, "-m", "alembic", "upgrade", "head"]),
                                  ("seed", [self.python, "app/initial_data.py"])]:
                rec = self.record(name, run(command, self.subject / "backend", env), workers=workers)
                if rec["status"] != "passed":
                    return rec
            with (Path(scratch) / "server.log").open("w+") as log:
                server = subprocess.Popen([self.python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=self.subject / "backend", env=env, stdout=log, stderr=log, start_new_session=True)
                try:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with urllib.request.urlopen(base + "/api/v1/utils/health-check/", timeout=.5) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            if server.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("API failed to become ready")
                            time.sleep(.05)  # readiness polling, not a correctness assertion
                    report = Path(scratch) / "browser.json"
                    env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
                    result = run([self.node, self.subject / "node_modules/@playwright/test/cli.js", "test", f"--workers={workers}", "--retries=0", "--reporter=json"], self.subject / "frontend", env, timeout=300)
                    browser_report = json.loads(report.read_text()) if report.exists() else {}
                    stats = browser_report.get("stats", {})
                    errors = []
                    actual_passed = 0
                    def failures(suites):
                        nonlocal actual_passed
                        for suite in suites:
                            for spec in suite.get("specs", []):
                                for test in spec.get("tests", []):
                                    results = test.get("results", [])
                                    if results and results[-1].get("status") == "passed" and test.get("expectedStatus") == "passed":
                                        actual_passed += 1
                                    for attempt in test.get("results", []):
                                        if attempt.get("status") not in {"passed", "skipped"}:
                                            errors.append({"test": spec["title"], "status": attempt.get("status"),
                                                "errors": [sanitize(e.get("message", ""), self.subject) for e in attempt.get("errors", [])]})
                            failures(suite.get("suites", []))
                    failures(browser_report.get("suites", []))
                    stats["actual_passed"] = actual_passed
                    if result["status"] == "passed" and (not stats.get("expected") or stats.get("unexpected") or stats.get("flaky")):
                        result["status"] = "unresolved"
                    log.flush()
                    log.seek(0)
                    server_tail = sanitize(log.read()[-5000:], self.subject) if errors else ""
                    return self.record("browser", result, workers=workers, tests=stats, failures=errors, server_tail=server_tail)
                finally:
                    if server.poll() is None:
                        os.killpg(server.pid, signal.SIGTERM)
                        try:
                            server.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(server.pid, signal.SIGKILL)
                            server.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--dsn", default=os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres"))
    parser.add_argument("--mode", choices=["baseline", "faults", "browser"], default="baseline")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("out/fullstack.json"))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    subject = args.subject.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject, text=True).strip()
    if revision != REVISION:
        parser.error(f"expected upstream revision {REVISION}")
    python = subject / ".venv/bin/python"
    if not python.exists() or not (subject / "node_modules").is_dir():
        parser.error("prepare upstream .venv and node_modules first")
    lab = Lab(subject, args.dsn, shutil.which("node") or "node", python, [])
    start = time.perf_counter()
    report = {"upstream": UPSTREAM, "revision": revision, "platform": {"system": platform.system(), "machine": platform.machine()},
              "source_sha256": tree_identity(subject),
              "verifier_sha256": verifier_identity(),
              "python": subprocess.check_output([python, "--version"], text=True).strip(),
              "node": subprocess.check_output([lab.node, "--version"], text=True).strip(),
              "mode": args.mode, "records": lab.records, "setup_included": False,
              "limitations": ["Small template, not a large production system", "Host execution, not a sandbox", "Cached dependencies; provisioning and hosted dispatch excluded", "Hand-selected faults do not estimate production defect detection"]}
    try:
        if args.mode == "baseline":
            for _ in range(args.repeat):
                lab.backend(subject)
                lab.frontend(subject)
                lab.frontend(subject, build=True)
                if (HERE / "oracle_tests.py").exists():
                    lab.backend(subject, "oracle")
        elif args.mode == "browser":
            lab.frontend(subject, build=True)
            for _ in range(args.repeat):
                lab.browser(args.workers)
        else:
            report["matrix"] = lab.faults()
    finally:
        report["total_seconds"] = time.perf_counter() - start
        report["source_unchanged"] = report["source_sha256"] == tree_identity(subject)
        report["verifier_unchanged"] = report["verifier_sha256"] == verifier_identity()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["source_unchanged"] or not report["verifier_unchanged"]:
        print("UNRESOLVED: source or verification policy changed during measurement")
        return 2
    if args.mode == "faults":
        return 0 if report.get("matrix") and all(row[k] != "unresolved" for row in report["matrix"] for k in ("upstream", "oracle", "typecheck")) else 1
    return 0 if lab.records and all(r["status"] == "passed" for r in lab.records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
