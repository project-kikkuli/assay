# Assay experiment

This is an independent, synthetic tooling experiment. Use only this repository
and cited public sources. Do not import private code, data, traces, names, paths,
credentials, or examples from any other project.

The original runner uses Python 3.11+ and the standard library. New research
experiments may use pinned polyglot dependencies and public upstream projects;
record provenance, licenses, setup costs, and reproducible commands. Use
apply_patch for edits. Keep the
verification runner independent of the example application's business logic.
Tests use unittest. No wall-clock sleeps in correctness tests. Timings are
observations, not deterministic correctness assertions. Do not claim formal
proof, sandboxing, complete dependency inference, or production readiness.

Run `python3 -m unittest discover -s tests -v` and the documented demo before
handoff. Preserve provenance, replay inputs, negative results, and limitations.
Do not publish raw worker transcripts or environment dumps.
