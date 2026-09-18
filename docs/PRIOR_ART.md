# Public context and provenance

All source in this repository is newly written for a synthetic experiment.
No private source trees, production incidents, configurations, or datasets are
inputs to the implementation or demonstration. The example domain is a generic
leased job queue. Agent sessions were given only narrow synthetic tasks and
explicitly scoped scratch directories; prior conversation was not forwarded.

Public conceptual influences:

- [Stipulate](https://github.com/project-kikkuli/stipulate): executable backend
  invariants, action sequences, replay metadata, and counterexample shrinking.
- [Veriscope](https://github.com/project-kikkuli/veriscope): connect declared
  dependencies, assertions, causal operations, and human inspection.
- [Comb](https://github.com/project-kikkuli/comb): explicit event ordering and a
  graph representation shared between execution and inspection tools.
- [magic-trace](https://github.com/janestreet/magic-trace): motivate a drilldown
  from observed behavior to implementation cost. Assay does not implement or
  integrate its hardware instruction tracing.
- [Bazel hermeticity](https://bazel.build/basics/hermeticity): make inputs explicit
  before treating cached work as reusable. Assay does not claim Bazel-level
  execution isolation.
- [FoundationDB simulation](https://www.foundationdb.org/files/fdb-paper.pdf):
  run real implementation code under controlled events and preserve failures.
  The included queue harness is much smaller and has a narrower fault model.

These are attribution and research links, not dependencies or endorsements.
Assay does not copy framework implementations from those projects.
