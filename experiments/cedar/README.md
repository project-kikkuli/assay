# Cedar symbolic gate

This is a real Cedar Lean symbolic-compilation experiment. It does not implement
an SMT compiler. The runner invokes `cedar-lean-cli`'s `validate`, `evaluate`,
and `symcc check-implies` commands, with CVC5 doing the SMT check.

Build the image from public pinned sources (requires `git`, Docker, and
public network access; no host credentials are mounted):

```sh
CEDAR_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/cedar-build.XXXXXX")"
python3 experiments/cedar/prepare.py --scratch "$CEDAR_SCRATCH" --build \
  --tag kikkuli-cedar-lean-cli:acb0db7-slim
```

The preparation step checks out both exact commits, copies the committed Cargo
lockfiles and checked-in Lake manifest into an isolated Docker context. The
Dockerfile then uses `cargo build --locked` and does not run upstream's
`lake update`.

Recipe validation: a fresh scratch `prepare.py` run exited 0, lock/toolchain
provenance checks exited 0, and `docker build --check` exited 0 against that
fresh context. A full cold Docker build was not rerun after this recipe fix;
the runtime evidence below is from the previously built pinned image and is
not presented as proof that a fresh build completed.

Run the independent gate after building the image:

```sh
python3 experiments/cedar/run.py --write-evidence
```

The runner refuses a moving image tag: the measured runtime image is
`sha256:9f8a1324cbf6d7c80ff5de21942f3e6b19417930f8085ccee8490ff2f56c43bf`.
It mounts only this fixture directory read-only and uses `--network none`,
`--read-only`, `--cap-drop ALL`, `no-new-privileges`, and a small no-exec `/tmp`.
A per-call cidfile lets a timeout clean up the exact container it started. A
timeout is `unknown` and fails admission; process exit 0 is not interpreted as
proof without parsing an exact semantic summary line.

## Gate and evidence

The schema has `Tenant`, `User(tenant, roles)`, and `Document(tenant, owner,
public?)`, with one `read` action. `envelope.cedar` is the reviewed upper safety
envelope: it forbids cross-tenant access and permits only same-tenant public,
owner, or `reader` role access. `candidate.cedar` expresses the same positive
cases as separate policies.

The formal checks are bidirectional:

* candidate ⇒ envelope: `proved` (safety upper bound);
* envelope ⇒ candidate: `proved` (quantified availability lower bound);
* broken broad permit ⇒ envelope: `counterexample`;
* deny-all ⇒ envelope: `proved` vacuously, but envelope ⇒ deny-all:
  `counterexample`.

The last pair makes the positive obligation formal rather than a single happy
path. Runtime replay additionally proves positive access, cross-tenant denial,
the broken policy's cross-tenant allow, and implicit denial when optional
`public` is absent. The concrete cross-tenant request is a deliberately
human-chosen fixture replay; the CLI version used here reports a failing request
signature, not a decoded solver model, so it is not claimed as solver-extracted.

## Pins, measurements, and scope

The build used Cedar-spec commit
[`acb0db7`](https://github.com/cedar-policy/cedar-spec/tree/acb0db7daa838249d894d2af64c850e1c9bf0d7d),
Cedar dependency commit
[`9502ae0`](https://github.com/cedar-policy/cedar/tree/9502ae02564a23028c732f8c1f2311635394c34f),
Lean 4.34.0, Rust 1.91.1, and CVC5 1.2.1. `evidence.json` records fixture
SHA-256s, the image ID, raw CLI output, and the observed timings. The slim
runtime image is 483 MB. `--warm-repeat 30` retains thirty individual proof
samples, including container start, Lean encoding, CVC5, and teardown. The
container-start probe measures an already available image, not downloading or
building it. Solver-only and cold-build timings were not captured.

The checked-in build recipe and dependency locks were finalized after the
measured image was built. Fresh source preparation and Docker recipe checks
passed; a full cold rebuild of this finalized recipe has not been validated.
The runtime evidence therefore does not establish reproducibility of that build.

The image ID is a local artifact pin, not a claim that an independent rebuild
will produce the same ID. For an approved rebuild, inspect the resulting image
with `docker image inspect --format '{{.Id}}' IMAGE`, review the pinned source
and tool inputs above, replace the expected ID in `run.py`, and regenerate
`evidence.json`.

The proof scope is the Cedar schema's open `User/read/Document` request
environment and Cedar's formal evaluation semantics. It assumes the caller
supplies authentic principal/resource IDs, the schema is the deployed schema,
and entity facts are authoritative. It does not prove database isolation,
identity binding, transport authorization, policy deployment atomicity, or
actions outside `read`; those are separate application-security obligations.

Primary sources: [Cedar Lean CLI](https://github.com/cedar-policy/cedar-spec/tree/acb0db7daa838249d894d2af64c850e1c9bf0d7d/cedar-lean-cli),
[Cedar](https://github.com/cedar-policy/cedar/tree/9502ae02564a23028c732f8c1f2311635394c34f),
and [AutoCedar](https://arxiv.org/abs/2607.03656) (background only; no claims
from its headline evaluation are adopted here).
