#!/usr/bin/env python3
"""An executable experiment in evidence reuse, not a replacement build system."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from capsule import World, command, request, run_process
from evidence import Authority, Store, admit, canonical, digest, expected_keys, file_hash, run_graph, tree

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
OUT = REPO / "out/continuum"
ENVIRONMENTS = json.loads((ROOT / "environment.json").read_text())
IGNORED = {"node_modules", "dist", "__pycache__", ".pytest_cache"}
CONTROL_FILES = ("run.py", "capsule.py", "evidence.py", "verify.py", "contract.json", "browser.mjs", "edge.py")
EXECUTOR_SOURCE = {name: file_hash(ROOT / name) for name in CONTROL_FILES}


def dependency_tree(root):
    inventory = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(Path(root).resolve()):
                raise ValueError(f"dependency symlink escapes closure: {rel}")
            inventory[rel] = {"symlink": os.readlink(path)}
        elif path.is_file():
            inventory[rel] = file_hash(path)
    return inventory


def freeze(overrides=None):
    OUT.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="candidate-", dir=OUT))
    for section in ("app", "client", "auditors"):
        shutil.copytree(ROOT / section, target / section,
                        ignore=lambda _dir, names: [name for name in names if name in IGNORED], symlinks=True)
        tree(target / section)  # rejects source links before executing anything
    if not (ROOT / "client/node_modules").is_dir():
        raise RuntimeError("run ./continuum prepare first")
    shutil.copytree(ROOT / "client/node_modules", target / "client/node_modules", symlinks=True)
    (target / "client/dist").mkdir()
    (target / "verifier").mkdir()
    for name in ("verify.py", "contract.json", "edge.py", "browser.mjs"):
        shutil.copy2(ROOT / name, target / "verifier" / name)
    for name, replacement in (overrides or {}).items():
        path = target / name
        relative = Path(name)
        if (not path.resolve().is_relative_to(target) or relative.parts[0] not in {"app", "client", "auditors"}
                or any(part in IGNORED for part in relative.parts) or not path.is_file()):
            raise ValueError("override must identify existing candidate source, never trusted controls or dependencies")
        path.write_text(replacement)
    return target


def blob_put(path):
    sha = file_hash(path)
    destination = OUT / "blobs" / sha
    destination.parent.mkdir(exist_ok=True, parents=True)
    if not destination.exists():
        shutil.copyfile(path, destination)
    return sha


def blob_restore(sha, path, *, executable=False):
    source = OUT / "blobs" / sha
    if file_hash(source) != sha:
        raise ValueError("cached output blob is corrupt")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    if executable:
        path.chmod(0o755)


def compile_client(root, environment):
    before = tree(root / "client", exclude=IGNORED)
    args = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,size=64m",
            "--mount", f"type=bind,source={root / 'client'},target=/client,readonly",
            "--mount", f"type=bind,source={root / 'client/dist'},target=/client/dist", "-w", "/client",
            environment["node"], "node", "node_modules/typescript/bin/tsc", "-p", "tsconfig.json"]
    command(args)
    if tree(root / "client", exclude=IGNORED) != before:
        raise ValueError("compiler changed source inputs")
    outputs = {path.name: blob_put(path) for path in (root / "client/dist").glob("*.js")}
    if not {"client.js", "worker.js"}.issubset(outputs):
        raise ValueError("missing compiled application output")
    return {"passed": True, "outputs": outputs}


def restore_client(root, result):
    for name, sha in result["outputs"].items():
        if Path(name).name != name:
            raise ValueError("invalid output name")
        blob_restore(sha, root / "client/dist" / name)


def artifact_digest(root):
    return digest({"app": tree(root / "app"), "client": tree(root / "client", exclude={"node_modules"}),
                   "dependencies": digest(dependency_tree(root / "client/node_modules"))})


def load_verifier(root):
    spec = importlib.util.spec_from_file_location("continuum_independent_verifier", root / "verifier/verify.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Import loaders write __pycache__ into otherwise captured input trees.
    # Execute these trusted source bytes without modifying the evidence subject.
    exec(compile((root / "verifier/verify.py").read_bytes(), spec.origin, "exec"), module.__dict__)
    return module


def ledger_tsv(db):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        rows = ["\t".join(["reservation", str(tenant), str(identity), str(quantity), str(status)])
                for tenant, identity, quantity, status in connection.execute(
                    "SELECT tenant,id,quantity,status FROM reservations ORDER BY seq")]
        rows += ["\t".join(["job", str(tenant), str(identity), str(key), str(state)])
                 for tenant, identity, key, state in connection.execute(
                     "SELECT tenant,reservation_id,delivery_key,state FROM outbox ORDER BY id")]
    return "\n".join(rows) + "\n"


def behavior(root, environment):
    verifier = load_verifier(root)
    with World(root, environment) as world:
        result = verifier.verify(world.urls["api"], world.db, worker_command=world.worker_command(),
                                 provider_url=world.urls["provider"])
        result["ledger"] = ledger_tsv(world.db)
        result["runtime"] = world.runtime
        return result


def browser(root, environment):
    # Browser binaries are preparation, but their version and runner source enter the action key.
    script = root / "verifier/browser.mjs"
    with World(root, environment, database=root / "browser-state", provider=False) as world:
        output = command(["node", script, world.urls["api"], root / "client/node_modules/@playwright/test/index.mjs"], timeout=20)
    return json.loads(output.splitlines()[-1])


def python_unit(root, environment):
    output = command(["docker", "run", "--rm", "--network=none", "--read-only", "--tmpfs", "/tmp:rw,size=64m",
                      "-e", "PYTHONDONTWRITEBYTECODE=1", "--mount", f"type=bind,source={root / 'app'},target=/app,readonly",
                      "-w", "/app", environment["python"], "python", "-m", "unittest", "discover", "-v"], timeout=60)
    return {"passed": True, "suite": "application unit tests", "stdout": output[-1000:]}


def compile_rust(root):
    output = root / "bin/ledger"
    output.parent.mkdir(exist_ok=True)
    command(["rustc", "--edition=2021", "-O", root / "auditors/ledger.rs", "-o", output], timeout=60)
    return {"passed": True, "binary": blob_put(output)}


def compile_haskell(root):
    output = root / "haskell-bin"
    output.mkdir(exist_ok=True)
    command(["docker", "run", "--rm", "--network=none", "--read-only", "--tmpfs", "/tmp:rw,size=128m",
             "--user", f"{os.getuid()}:{os.getgid()}",
             "--mount", f"type=bind,source={root / 'auditors'},target=/src,readonly",
             "--mount", f"type=bind,source={output},target=/out", ENVIRONMENTS["haskell"],
             "ghc", "-no-user-package-db", "-O1", "-outputdir", "/out", "-o", "/out/admission", "/src/admission.hs"], timeout=120)
    return {"passed": True, "binary": blob_put(output / "admission")}


def audit_ledger(root, inputs):
    blob_restore(inputs["rust"]["binary"], root / "bin/ledger", executable=True)
    result = subprocess.run([str(root / "bin/ledger")], input=inputs["behavior"]["ledger"],
                            text=True, capture_output=True, timeout=5)
    report = json.loads(result.stdout)
    if (result.returncode == 0) != report["passed"]:
        raise ValueError("auditor exit/verdict disagreement")
    return report


def policy_decision(root, binary, observation, compatibility="compatible", human="none", evidence="valid"):
    blob_restore(binary, root / "haskell-bin/admission", executable=True)
    outcome = run_process(["docker", "run", "--rm", "-i", "--network=none", "--read-only",
                               "--user", f"{os.getuid()}:{os.getgid()}",
                               "--mount", f"type=bind,source={root / 'haskell-bin'},target=/policy,readonly",
                               ENVIRONMENTS["haskell"], "/policy/admission"],
                              input=f"v1\t{evidence}\t{observation}\t{compatibility}\t{human}\n",
                              text=True, capture_output=True, timeout=15)
    if outcome.returncode:
        raise ValueError(outcome.stderr[-1000:])
    return json.loads(outcome.stdout)


def policy_tests(root, inputs):
    cases = [("healthy", "compatible", "none", "valid", "promote"),
             ("regression", "compatible", "none", "valid", "rollback"),
             ("regression", "incompatible", "approve", "valid", "contain_and_escalate"),
             ("security", "compatible", "approve", "valid", "contain_and_escalate"),
             ("unknown", "compatible", "approve", "valid", "investigate"),
             ("healthy", "compatible", "approve", "invalid", "reject")]
    records = []
    for observation, compatibility, human, evidence, expected in cases:
        result = policy_decision(root, inputs["haskell"]["binary"], observation, compatibility, human, evidence)
        records.append({"observation": observation, "compatibility": compatibility, "human": human,
                        "evidence": evidence, "expected": expected, "actual": result})
    return {"passed": all(row["expected"] == row["actual"]["decision"] for row in records), "cases": records}


def migration(root):
    database = root / "migration-state"
    database.mkdir()
    db = database / "app.db"
    with sqlite3.connect(db) as connection:
        connection.executescript("CREATE TABLE reservations(id TEXT PRIMARY KEY,tenant TEXT,creator TEXT,quantity INTEGER,status TEXT,seq INTEGER UNIQUE); CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); INSERT INTO meta VALUES('schema_version','1'); INSERT INTO reservations VALUES('historical','alpha','alpha-maker',3,'reserved',1);")
    observations = []
    for profile in ("current", "next", "current"):
        with World(root, ENVIRONMENTS[profile], database=database, provider=False) as world:
            status, row = request(world.urls["api"], "/reservations/historical", token="alpha-maker")
            facts = command(["docker", "exec", f"{world.prefix}-api", "python", "-c",
                             "import sys,sqlite3,json; print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version}))"])
            observations.append({"profile": profile, "status": status, "row": row, "runtime": json.loads(facts)})
    return {"passed": all(row["status"] == 200 and row["row"].get("quantity") == 3 for row in observations),
            "observations": observations, "scope": "populated v1->v2 and interpreter transition; not arbitrary destructive migrations"}


def make_actions(root, profile):
    environment = ENVIRONMENTS[profile]
    current_source = {name: file_hash(ROOT / name) for name in CONTROL_FILES}
    if current_source != EXECUTOR_SOURCE:
        raise ValueError("trusted executor source changed during this process; restart before issuing evidence")
    recipe = digest(current_source)
    app = tree(root / "app")
    client = tree(root / "client", exclude=IGNORED)
    dependencies = digest(dependency_tree(root / "client/node_modules"))
    trusted = {"contract": file_hash(root / "verifier/contract.json"), "controls": tree(root / "verifier")}
    def action(inputs, env, run, deps=(), restore=None):
        return {"inputs": inputs, "environment": {"runtime": env, "uid": os.getuid(), "gid": os.getgid(),
                "observer_python": sys.version, "observer_platform": platform.platform()},
                "recipe": recipe, "run": run, "deps": list(deps), "restore": restore}
    return {
        "client": action({"source": client, "dependencies": dependencies}, environment["node"],
                         lambda _: compile_client(root, environment), restore=lambda result: restore_client(root, result)),
        "package": action({"app": app, "client": client, "dependencies": dependencies, **trusted}, environment,
                          lambda _: {"passed": True, "artifact": artifact_digest(root)}, deps=["client"]),
        "python": action(app, environment["python"], lambda _: python_unit(root, environment)),
        "rust": action(tree(root / "auditors"), {"compiler": command(["rustc", "--version"]), "platform": platform.platform()},
                       lambda _: compile_rust(root)),
        "haskell": action(tree(root / "auditors"), ENVIRONMENTS["haskell"], lambda _: compile_haskell(root)),
        "migration": action(app, {key: ENVIRONMENTS[key]["python"] for key in ("current", "next")}, lambda _: migration(root)),
        "behavior": action({"app": app, **trusted}, environment, lambda _: behavior(root, environment), deps=["package"]),
        "browser": action({"app": app, "browser": file_hash(ROOT / "browser.mjs"), "node": command(["node", "--version"]),
                           "playwright": file_hash(root / "client/node_modules/@playwright/test/package.json")},
                          environment, lambda _: browser(root, environment), deps=["package"]),
        "ledger": action({"contract": trusted["contract"]}, {"platform": platform.platform()}, lambda inputs: audit_ledger(root, inputs), deps=["behavior", "rust"]),
        "policy": action({"policy_cases": recipe}, ENVIRONMENTS["haskell"], lambda inputs: policy_tests(root, inputs), deps=["haskell"]),
    }


def check(*, profile="current", jobs=4, reuse=True, overrides=None):
    started = time.perf_counter()
    root = freeze(overrides)
    capture_seconds = time.perf_counter() - started
    authority = Authority(OUT / "operator")
    store = Store(OUT / "receipts", authority)
    actions = make_actions(root, profile)
    graph = run_graph(actions, store, jobs=jobs, reuse=reuse)
    receipts = {name: item["receipt"] for name, item in graph["actions"].items()}
    # Recompute from captured bytes after execution: a mutated input must not inherit its old pass.
    required = expected_keys(make_actions(root, profile), receipts)
    artifact = artifact_digest(root)
    decision = admit(receipts, required, authority, artifact=artifact, environment=ENVIRONMENTS[profile],
                     policy=digest({name: action["recipe"] for name, action in actions.items()}), artifact_action="package")
    report = {"format": "continuum.run.v1", "profile": profile, "jobs": jobs, "reuse": reuse,
              "seconds": time.perf_counter() - started, "graph": graph, "admission": decision,
              "capture_seconds": capture_seconds,
              "preparation_included": False,
              "limits": ["trusted operator host and Docker daemon", "synthetic identities and workload",
                         "not deterministic OS scheduling", "hosted queue and image/dependency preparation not measured"]}
    (root / "report.json").write_bytes(canonical(report))
    trace = {"traceEvents": [{"name": name, "cat": "verification", "ph": "X", "pid": 1, "tid": name,
                              "ts": item["start_seconds"] * 1_000_000, "dur": item["observed_seconds"] * 1_000_000,
                              "args": {"cache": item["cache"], "dependencies": item["dependencies"],
                                       "action_key": item["receipt"]["payload"]["action_key"]}}
                             for name, item in graph["actions"].items()]}
    (root / "trace.json").write_bytes(canonical(trace))
    return report, root


def concise(report):
    return {"seconds": round(report["seconds"], 3), "graph_seconds": round(report["graph"]["seconds"], 3),
            "decision": report["admission"]["decision"], "cache_hits": report["graph"]["cache_hits"],
            "capture_seconds": round(report.get("capture_seconds", 0), 3),
            "actions": {name: {"cache": item["cache"], "seconds": round(item["observed_seconds"], 3),
                               "start_seconds": round(item["start_seconds"], 3), "dependencies": item["dependencies"],
                               "evidence_execution_seconds": round(item["receipt"]["payload"]["execution_seconds"], 3),
                               "passed": item["receipt"]["payload"]["result"]["passed"],
                               "error": item["receipt"]["payload"]["result"].get("error")}
                        for name, item in report["graph"]["actions"].items()}}


def prepare():
    started = time.perf_counter()
    images = {ENVIRONMENTS[profile][language] for profile in ("current", "next") for language in ("python", "node")}
    images.add(ENVIRONMENTS["haskell"])
    for image in sorted(images):
        command(["docker", "pull", image], timeout=240)
    command(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=ROOT / "client", timeout=180)
    command(["node", "node_modules/@playwright/test/cli.js", "install", "chromium"], cwd=ROOT / "client", timeout=180)
    return {"preparation_seconds": time.perf_counter() - started, "images": sorted(images)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "check"], default="check", nargs="?")
    parser.add_argument("--profile", choices=["current", "next"], default="current")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if args.mode == "prepare":
        print(json.dumps(prepare(), indent=2))
        return
    report, _ = check(profile=args.profile, jobs=args.jobs, reuse=not args.no_cache)
    print(json.dumps(concise(report), indent=2))
    raise SystemExit(0 if report["admission"]["decision"] == "admit" else 1)


if __name__ == "__main__":
    main()
