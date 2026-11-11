# AI use

Codex was used for the design note, implementation, test and chaos harness code, Docker/CI configuration, documentation, and analysis of the executed test results. The SQLite lease/fencing design and the transaction that couples the result effect with the terminal state were adopted.

Verification performed during this AI-assisted session included real HTTP requests, separate worker processes, targeted concurrency tests, hard termination at transaction boundaries, payload/idempotency reconciliation, and raw-log inspection. Exact run counts and outcomes are in REPORT.md. A Windows virtual-environment launcher assumption was found to be unreliable: the launched PID differed from the worker PID. The launch code was changed and a PID assertion/regression test added; the invalid run was preserved. Docker/Linux execution and production readiness were not inferred from Windows results.

This disclosure describes work actually performed by the assistant. It does not claim that the applicant independently wrote or personally reviewed the implementation. The applicant should review the code and evidence before submitting or making any such claim.
