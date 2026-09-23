"""A real parallel-worker batching merge queue — a working scheduler, not a
simulation of one.

Real `git worktree` directories give each worker its own real checkout of
the same repo, so real concurrent `pytest` subprocesses never race on one
working tree. A "broken PR" is a real, executed one-line mutation to
`src/itsdangerous/signer.py`'s real `verify_signature` (negating
`hmac.compare_digest`, so every real signature check really fails) applied
to a real checkout before real tests run — real code, really broken, not a
boolean flag standing in for one. Bisection on a real batch failure is a
real recursive sequence of real `pytest` subprocess runs against real
sub-range checkouts, re-applying the mutation only when the mutated PR's
real index falls inside that sub-range.
"""
from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

# A venv's `pip install -e .` editable install resolves imports back to the
# clone it was installed from — independent of `cwd`. With several real
# `git worktree` checkouts of the same repo running concurrently, that
# resolves every worker's `import itsdangerous` to the SAME original clone,
# silently ignoring each worker's own real (possibly mutated) checkout. The
# fix is a real one: point `PYTHONPATH` at the worktree's own `src/` so the
# package actually imported is the one actually checked out there.

MUTATION_TARGET = "src/itsdangerous/signer.py"
REAL_LINE = "        return hmac.compare_digest(sig, self.get_signature(key, value))"
MUTATED_LINE = "        return not hmac.compare_digest(sig, self.get_signature(key, value))"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def add_worktree(repo: Path, worktree_dir: Path, sha: str) -> None:
    git_ok(repo, "worktree", "remove", "--force", str(worktree_dir))
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(worktree_dir), sha],
                    check=True, capture_output=True)


def remove_worktree(repo: Path, worktree_dir: Path) -> None:
    git_ok(repo, "worktree", "remove", "--force", str(worktree_dir))


def checkout(worktree_dir: Path, sha: str) -> None:
    subprocess.run(["git", "-C", str(worktree_dir), "checkout", "-q", "-f", sha], check=True)


def apply_mutation(worktree_dir: Path) -> bool:
    """Really edits the real checked-out file. Returns False (and mutates
    nothing) if the real line isn't present at this commit, so a caller
    never silently "succeeds" at breaking nothing."""
    target = worktree_dir / MUTATION_TARGET
    if not target.exists():
        return False
    text = target.read_text()
    if REAL_LINE not in text:
        return False
    target.write_text(text.replace(REAL_LINE, MUTATED_LINE, 1))
    return True


def run_pytest(worktree_dir: Path, python: str, target: str = "tests/test_itsdangerous") -> tuple[bool, float]:
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree_dir / "src")
    start = time.perf_counter()
    result = subprocess.run([python, "-m", "pytest", "-q", target], cwd=worktree_dir, capture_output=True, text=True, env=env)
    return result.returncode == 0, time.perf_counter() - start


def run_real_job(worktree_dir: Path, python: str, sha: str, mutated: bool) -> dict:
    checkout(worktree_dir, sha)
    mutation_applied = apply_mutation(worktree_dir) if mutated else False
    passed, duration = run_pytest(worktree_dir, python)
    return {"sha": sha, "mutated_requested": mutated, "mutation_applied": mutation_applied,
            "passed": passed, "duration_s": duration}


def bisect_real(worktree_dir: Path, python: str, prs: list[dict]) -> tuple[list[dict], float, int]:
    """Real binary search over a contiguous real batch. Each probe checks out
    that sub-range's real terminal commit, re-applies the mutation only if a
    mutated PR's real index falls in-range, and really runs pytest. Returns
    (verdicts for every PR, extra real seconds spent, extra real jobs run)."""
    if len(prs) == 1:
        pr = prs[0]
        result = run_real_job(worktree_dir, python, pr["sha"], pr["mutated"])
        return [{"index": pr["index"], "passed": result["passed"]}], 0.0, 0

    mid = len(prs) // 2
    left, right = prs[:mid], prs[mid:]
    verdicts: list[dict] = []
    extra_s = 0.0
    extra_jobs = 0

    left_mutated = any(p["mutated"] for p in left)
    left_result = run_real_job(worktree_dir, python, left[-1]["sha"], left_mutated)
    extra_s += left_result["duration_s"]
    extra_jobs += 1
    if left_result["passed"]:
        verdicts.extend({"index": p["index"], "passed": True} for p in left)
    else:
        sub_verdicts, sub_s, sub_jobs = bisect_real(worktree_dir, python, left)
        verdicts.extend(sub_verdicts)
        extra_s += sub_s
        extra_jobs += sub_jobs

    right_mutated = any(p["mutated"] for p in right)
    right_result = run_real_job(worktree_dir, python, right[-1]["sha"], right_mutated)
    extra_s += right_result["duration_s"]
    extra_jobs += 1
    if right_result["passed"]:
        verdicts.extend({"index": p["index"], "passed": True} for p in right)
    else:
        sub_verdicts, sub_s, sub_jobs = bisect_real(worktree_dir, python, right)
        verdicts.extend(sub_verdicts)
        extra_s += sub_s
        extra_jobs += sub_jobs

    return verdicts, extra_s, extra_jobs


class RealBatchQueue:
    """Real worker pool: each worker is a real `git worktree`, dispatched
    through a real `ThreadPoolExecutor` so batches genuinely execute
    concurrently (the GIL is released for the whole subprocess wait)."""

    def __init__(self, repo: Path, base_dir: Path, workers: int, python: str, tip_sha: str):
        self.repo = repo
        self.python = python
        self.worktrees = [base_dir / f"worker-{i}" for i in range(workers)]
        for wt in self.worktrees:
            add_worktree(repo, wt, tip_sha)
        self._lock = threading.Lock()
        self._free = list(self.worktrees)

    def close(self) -> None:
        for wt in self.worktrees:
            remove_worktree(self.repo, wt)

    def _acquire(self) -> Path:
        while True:
            with self._lock:
                if self._free:
                    return self._free.pop()
            time.sleep(0.01)

    def _release(self, wt: Path) -> None:
        with self._lock:
            self._free.append(wt)

    def _run_batch(self, batch: list[dict], t0: float) -> dict:
        wt = self._acquire()
        try:
            start = time.time() - t0
            any_mutated = any(p["mutated"] for p in batch)
            result = run_real_job(wt, self.python, batch[-1]["sha"], any_mutated)
            jobs = 1
            wall_s = result["duration_s"]
            if result["passed"]:
                verdicts = [{"index": p["index"], "passed": True} for p in batch]
            else:
                verdicts, extra_s, extra_jobs = bisect_real(wt, self.python, batch)
                wall_s += extra_s
                jobs += extra_jobs
            finish = time.time() - t0
            return {"batch_indices": [p["index"] for p in batch], "start_s": round(start, 4),
                    "finish_s": round(finish, 4), "real_cpu_s": round(wall_s, 4), "jobs": jobs, "verdicts": verdicts}
        finally:
            self._release(wt)

    def run(self, prs: list[dict], batch_size: int) -> dict:
        """Dispatches every real batch to the real pool at once (burst
        arrival — the whole backlog is already queued) and blocks for real
        wall-clock results. Convergence time per PR is the real timestamp its
        batch resolved, from a shared t0."""
        t0 = time.time()
        batches = [prs[i:i + batch_size] for i in range(0, len(prs), batch_size)]
        with ThreadPoolExecutor(max_workers=len(self.worktrees)) as pool:
            futures: list[Future] = [pool.submit(self._run_batch, b, t0) for b in batches]
            results = [f.result() for f in futures]
        makespan = max(r["finish_s"] for r in results) if results else 0.0
        total_cpu_s = sum(r["real_cpu_s"] for r in results)
        total_jobs = sum(r["jobs"] for r in results)
        return {"batches": results, "makespan_s": round(makespan, 4), "total_real_cpu_s": round(total_cpu_s, 4),
                "total_jobs": total_jobs}
