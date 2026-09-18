# Node transport probe

This is a local positive-control experiment for the hosted `socket hang up`.
It uses Node 22 and the pinned public fixture Axios package; it never contacts
the hosted runner or changes the application.

Run:

```sh
node experiments/transport/probe.mjs \
  --axios-root out/lab/fullstack/node_modules/axios \
  --output experiments/transport/results.json
```

The probe runs each mode with one keep-alive agent and one fresh-connection
agent, recording Axios request errors, `request.reusedSocket`, client ports,
and server connection/request identities. In `stale-keepalive`, the local
server intentionally resets a connection when a second request reuses it;
fresh connections must pass. In `server-pressure`, the server resets every
request after the first, so both arms must fail. These are causal controls,
not a diagnosis of Uvicorn. The hosted incident needs the same fields from
the real request and server log to classify it. The retained local run passes
both controls: stale reuse fails 5/10 requests versus 0/10 with fresh connections;
server pressure fails 9/10 in both arms. Request counts, not sleeps, cause resets.

The original hosted failure remains **unclassified**: its retained server tail
and client exception do not establish which connection failed. Later successful
two-worker runs do not prove that lowering concurrency fixed it. The full-stack
workflow records transport metadata with `--observe-http`; another occurrence
can be correlated without adding retries. These controls are complete; diagnosing
that historical incident is not.
