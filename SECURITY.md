# Experimental trust boundaries

Run manifests only from a trusted checkout: they execute arbitrary argv commands.
Assay is not a process sandbox, remote attestation service, or authorization
system. Do not use its local cache as authoritative evidence from an untrusted
contributor. The JSON digest detects corruption, not malicious replacement.

Captured stdout/stderr and structured event data can contain whatever the task
prints. Review artifacts before publishing them. Assay does not automatically
redact secrets or personal data. Use synthetic or appropriately minimized data.
The fixtures contain synthetic data; other subjects are explicitly pinned public
projects. No private application code or production data is included.

The source scanner excludes generated directories documented in DESIGN.md. Do
not put source inputs in those directories. Symlink inputs are refused. Runtime
dependencies outside the checkout are not completely captured. Source and policy
are checked before/after execution, not atomically frozen.

Wall-clock timings are observations, not guarantees. The Cedar experiment uses
a symbolic checker for a narrow authorization model; that result assumes
authentic entity facts and does not prove application security. Database
experiments assume the stated role and connection configuration. The full-stack
harness runs public candidate code on the host and is not an adversarial sandbox.
Read each experiment's assumptions before treating evidence as an admission rule.

The cell experiment runs its candidate actor inside an observed, restricted
Docker container; only the actor is untrusted. Its host-side kernel, verifier,
public application, dependency installation, and locally generated baseline are
operator-trusted. `cell qualify` must never run as a candidate-controlled way to
authorize that same candidate. It is not a protected hosted admission service.
