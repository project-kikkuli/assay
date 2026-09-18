# What a green suite misses

This runs an unmodified [FastAPI application](https://github.com/fastapi/full-stack-fastapi-template/tree/cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7)
(MIT): Python API, PostgreSQL migrations, React/TypeScript UI, generated client,
58 API tests, and 62 browser tests. It is a small template, not a large service.

Eight seeded API defects compare the upstream suite with seven separately
authored behavioral checks. The upstream suite catches three; the added checks
catch seven. An incorrect administrator count survives both. Existing TypeScript
types accept every mutation. These are hand-selected challenges, not an estimate
of escaped production defects. The added checks and initial faults share an
author; further independent challenges are needed.

## Run

Prepare the pinned upstream checkout with `uv sync --frozen --all-packages` and
`npm exec --yes --package=bun@1.3.12 -- bun install --frozen-lockfile`. Run a
disposable local PostgreSQL 18 server, then:

```sh
uv run experiments/fullstack/run.py --subject "$SUBJECT" --mode baseline
uv run experiments/fullstack/run.py --subject "$SUBJECT" --mode faults
```

The default database fixture is localhost:55439, user `postgres`, password
`assay-local-only`. Override with `ASSAY_PG_DSN`. The runner creates and removes
only its own UUID-named databases. Never point it at a real database.

Results in `out/fullstack.json` retain timings, actual test counts, failures,
source/verifier identities, and the fault matrix. Dependencies are warm; setup,
hosted dispatch, and production scale are not included. Candidates execute on
the host: this harness is not an untrusted-code sandbox.

## Parallelism needs causal waits

The same 62 browser tests took 33–36 seconds with one worker (3/3 green),
and 12–18 seconds with four (9/10 green). The failure was a password-reset
email timeout. The signup helper clicked Submit, then navigated away without
waiting for registration to finish. An [explicit request barrier](../boundaries/README.md)
reproduces that ordering: recovery can run before the account exists.

The [one-line helper patch](causal-signup.patch) waits for the application's
successful navigation. With it, 30/30 four-worker runs passed: median 12.29
seconds, range 11.95–15.49. These are repeated local observations under varying
machine load, not a zero-flake guarantee or proof of the earlier failure's cause.
All runs disable retries; [raw results](results/) include the failure.

For browser runs, install Playwright's Chromium and run Mailpit v1.29.2 with
HTTP on localhost:58027 and SMTP on localhost:51027. Use a separate checkout
for the helper variant:

```sh
uv run experiments/fullstack/run.py --subject "$SUBJECT" --mode browser --workers 4 --repeat 10
git -C "$VARIANT" apply "$ASSAY/experiments/fullstack/causal-signup.patch"
uv run experiments/fullstack/run.py --subject "$VARIANT" --mode browser --workers 4 --repeat 30
```
