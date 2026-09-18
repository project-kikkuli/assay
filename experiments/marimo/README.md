# marimo benchmark

## Outcome

At revision `e8009f0f50220af0825fc920ba48af90fdddf617`, direct fresh Vitest ran 6,505/6,505 tests in 101.305s. Dependency-aware closure ran 3,379 tests in 82.642s; the narrow `runs.ts` gate ran 56 tests in 9.010s in the seeded-fault profile. A separate minimal Node project passed only the 19 reducer/Jotai tests (two shuffle seeds); it is not a whole-suite speed claim.

The full optional Python run took 730.10s: 12,526 passed and 15 failed. Upstream-style preparation then made the 15 selected failures pass in 31.546s: real frontend assets (480 files, 26.7MB) and declared `dev` Ruff 0.16.7 were installed. This validates those selected causes, not the whole Python suite.

## Run

```sh
export MARIMO=/path/to/marimo
python3 experiments/marimo/measure.py --mode all
python3 experiments/marimo/measure.py --mode tests --python-group test-optional
python3 experiments/marimo/profile_vitest.py --repo-dir "$MARIMO"
python3 experiments/marimo/validate_python_selected.py --repo-dir "$MARIMO"
```

All scripts provide `--help`. Focused helpers cover worker pools, Turbo cache, minimal Node setup, affected Vitest timing, Python failure diagnosis, and the generated-JSON selector probe. Reports are sanitized and transient reports/caches stay outside this directory. `measure.py` uses an explicit environment allowlist, fresh positive Vitest reports, process-group timeouts, and rejects empty/malformed measurements as `unknown`.

This revision has no upstream `uv.lock`; the measured generated lock is preserved as `subject.uv.lock`. Frontend uses the checked-in `pnpm-lock.yaml`; Node 22.12 uses the local `npx pnpm@10.34.4` compatibility launcher. The JSON selector probe’s source-TS case exited 0 but selected zero tests, so it is unresolved/no coverage—not verification. E2E, Docker, secrets, paid services, and another full Python rerun are out of scope.
