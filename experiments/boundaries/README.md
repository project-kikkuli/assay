# Types agree; the system disagrees

These probes find three boundary mismatches in the **unmodified** pinned
FastAPI application. They are not seeded mutations.

| Explicit expectation | Observation |
|---|---|
| Invalid title updates return a client error | Generated TypeScript accepts `{title: null}`; the API returns **500** against the database's non-null constraint. The stored title survives. |
| Invalid pagination input returns a client error | `skip=-1` returns **500**. |
| Table pagination reaches every owned item | PostgreSQL has **150** items; the API exposes all 150 across pages; the browser stops at **100**. |
| Stored titles cannot execute JavaScript | An SVG-shaped title remains inert text in this probe. |

The first three expectations are product judgments stated before checking;
types cannot decide them. Authentication is a supplied fixture, not verified here.

After [preparing the subject](../fullstack/README.md):

```sh
uv run experiments/boundaries/run.py --subject "$SUBJECT" --enforce
```

Exit 1 means an expectation failed, 2 means the observation is incomplete.
Omit `--enforce` to collect a counterexample without treating the demo as a gate.
The JSON links actual TypeScript compilation, wire responses, and every observed
browser page. This is one dataset and one browser, not exhaustive verification.

## A flaky wait becomes an event-order counterexample

`--probe signup` holds the registration request at an explicit network barrier.
The existing test helper navigates away before registration finishes; password
recovery then returns 200 for a user who does not yet exist. Waiting for the
application's success navigation closes this particular ordering gap. The trace
includes real backend responses and login checks before and after release.
There are no sleeps or retry-to-green behavior. This demonstrates an admissible
failure schedule, not a claim that every observed email failure has this cause.
