# Protected oracle boundary

Run the real matrix from the Assay root:

```sh
scratch="$(mktemp -d)"
KIKKULI_CONFINEMENT_SCRATCH="$scratch" python3 experiments/confinement/run.py
```

The runner resolves `python:3.12-slim-bookworm` to the host-architecture
registry digest recorded in `evidence.json`, then executes each candidate in a
disposable container. The host reads candidate bytes but never imports or
mounts the host oracle, synthetic canary, or cache. The trusted host computes
`2 + 3` and accepts only the exact JSON response `{"result": 5}`; candidate
`accepted` fields are not an attestation. A healthy candidate must pass, while
false attestation, empty output, wrong result, canary/cache access, network,
output flood, and hang candidates must be rejected. Timeout or cleanup
uncertainty is `not_green`, never success.

Normal candidates use no network, read-only root, an unprivileged UID, dropped
capabilities, `no-new-privileges`, memory/CPU/process/output/time limits, and
an exact owned CID cleanup path. Each retained container is inspected before
cleanup; the recorded decision checks the observed network mode, rootfs, user,
limits, capabilities, security option, and host mounts. Host canary visibility
and cache hashes are independent safety invariants, not response claims. A
deliberately exposed read-only canary probe must read the random canary and be
rejected, proving that the probe itself works. A separate owned Docker bridge
with no published ports runs a same-image TCP canary: the deliberate network
probe must reach it and be rejected, while normal candidates use observed
`NetworkMode=none`.

`evidence.json` records immutable image and source identities, pull/startup and
per-case timings, response classifications, observed controls, and the
separate expected timeout/cleanup result for the hang candidate.

This is a Docker process boundary, not a VM or complete side-channel defense:
the host kernel and Docker daemon are trusted, and the tiny add operation is a
boundary fixture rather than representative application coverage.
