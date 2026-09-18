# Public context and provenance

The experiments use newly written synthetic fixtures and explicitly identified
public subjects: the MIT-licensed FastAPI full-stack template, Apache-2.0 marimo,
and Apache-2.0 Cedar. The template repair patch derives from that public source;
its license is retained beside the patch. Each experiment records its revision
and toolchain. No private source, incidents, configuration, or datasets are
included. Workers received only public or synthetic inputs in scoped scratch
directories, without prior conversation.

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
