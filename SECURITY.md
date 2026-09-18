# Experimental trust boundary

Run manifests only from a trusted checkout: they execute arbitrary argv commands.
Assay is not a process sandbox, remote attestation service, or authorization
system. Do not use its local cache as authoritative evidence from an untrusted
contributor. The JSON digest detects corruption, not malicious replacement.

Captured stdout/stderr and structured event data can contain whatever the task
prints. Review artifacts before publishing them. Assay does not automatically
redact secrets or personal data. Use synthetic or appropriately minimized data.
The included demonstration contains only newly generated sample queue payloads.

The source scanner excludes generated directories documented in DESIGN.md. Do
not put source inputs in those directories. Symlink inputs are refused. Runtime
dependencies outside the checkout are not completely captured. Source and policy
are checked before/after execution, not atomically frozen.

Wall-clock timings are observations. No claim of complete schedule exploration,
flakeless execution, authenticated provenance, or mathematical proof is made.
