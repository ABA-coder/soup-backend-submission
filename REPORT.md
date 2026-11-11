# Failure analysis and production verdict

**DON'T SHIP at 100 requests per second.** This is a single-host prototype with a tested transactional local-result protocol. Correctness checks do not establish production latency, availability, operational recovery, or safe external training execution.

## Evidence ledger

The generated [EVIDENCE_INDEX.md](EVIDENCE_INDEX.md) lists **every** chaos/load run and pytest transcript, including failed or interrupted experiments. Individual request responses, retries, worker logs, summaries, inputs, source hashes and platform metadata are under `evidence/`. Timestamps in evidence directory names are UTC. No failed run is hidden or edited into a pass.

The authoring host is Windows 11, Python 3.12.14, SQLite 3.53.1, with 12 logical CPUs reported. The tests use a real loopback HTTP server, separate OS worker processes, local filesystem SQLite, and synthetic 20-60 ms jobs. No Linux distribution or Docker runtime was available. Thus **zero Linux SIGKILL runs and zero Docker startup runs were executed here**. The supplied Linux harness and CI workflow need to be run on an appropriate machine before treating that part of the brief as verified.

## Failed experiment and correction

The initial chaos invocation was deliberately aborted at about 79 seconds. Every sampled killed-worker claim was empty even while work was running. Inspection found that `Popen.pid` pointed to the Windows virtual-environment redirector while workers reported a different interpreter PID. That made the experiment's kill targeting/coverage invalid; the apparent absence of lost work could not count as evidence. The API was manually stopped to force the run to fail and shut down. `intervention.json` documents the intervention and its reason. The recorded connection failures after that point were induced by stopping the API, not an unexplained queue failure.

The correction follows Python's own Windows multiprocessing launcher approach: directly launch the base interpreter with `__PYVENV_LAUNCHER__` set to the venv executable. A regression test asserts that a child's reported PID is the PID being killed. The harness now checks worker identities before loading jobs, and records whether each chosen victim had an observed active claim. The replacement mechanism uses the same corrected launcher. The invalid run's earlier 503 responses remain meaningful evidence of contention, but its kill count is not credited as valid chaos coverage.

The run was being stopped while the harness source was updated; Python's traceback source-line rendering therefore refers to some newer lines. The original start record preserves the old source hashes. The original JSON events and request logs are retained unchanged.

## Silent failures: protection and check

| Risk | Protection | Executed check / remaining limitation |
|---|---|---|
| Double claim | `BEGIN IMMEDIATE` obtains the writer lock before candidate selection; claim and attempt record commit together | Eight simultaneous claimants for one job yield one owner/attempt; large run reconciles attempt counts |
| Expired zombie | Worker identity + monotonically increasing token + running state + live lease gate renewal and completion | Old worker rejected before and after reassignment; retry of a completed token rejected |
| Cancel/complete race | Both operations serialize on the writer lock; effect and final state share a transaction | Both deterministic orders plus 30 true concurrent races; accepted cancellations checked against zero effects |
| Request retry creates duplicates | Unique permanent key and canonical validated payload comparison in the same write transaction | Sixteen concurrent same-key requests, default normalization, payload conflict, and 5,000 HTTP repeats in a completed full chaos run |
| Wrong isolation/partial result | Explicit transactions; no dirty reads; result insertion and terminal update are indivisible | Independent reader cannot see uncommitted effect; second writer blocked; process hard-killed before and after result commit |
| Clock skew | Lease timestamps come from SQLite after acquiring writer lock; workers use monotonic time only for sleep/renewal scheduling | Python wall clock patched to extreme values; database clock seam jumped forward/backward; old token still fenced |
| Worker death / frozen computation | Expired running job is reclaimable; renewed long jobs retain ownership | Large run hard kills and reclaims; long-job heartbeat test; old-generation zombie test. Arbitrarily long host clock reversal can delay recovery |
| Misleading chaos coverage | Verify actual worker PIDs, meaningful killed claims, cancellation exercise, and full input reconciliation | Initial invalid experiment detected and preserved; corrected run cannot pass solely by counting kill calls |

`effects.job_id` is unique as defense in depth. Duplicate writes are not hidden by `INSERT OR IGNORE`: a violation is an error. The worker crashes on unexpected database errors, and the harness treats unexplained worker exits as failures. The oracle independently checks counts, outcomes, tokens and payload digests rather than trusting worker success logs.

## What the tests do not establish

10,000 inputs are a workload size, not a proof bound. Seeds select workload/victims but cannot enumerate thread or process schedules. Process termination does not simulate a machine power cut or a storage controller lying about `fsync`. The targeted transaction-boundary tests exercise rollback and lost completion acknowledgement; they do not prove every disk-failure path. The large harness keeps the API/database host alive and does not crash the entire host. No disk-full/corruption, network partition, multi-host clocks, malicious clients, production training workload, external side-effect protocol, backup restoration or long-duration soak was tested. The clock tests use controlled injection, not real NTP changes. Health only checks database readability, not worker progress.

The first unit invocation passed 14 tests; the PID regression test increased the suite to 15. The full transcript retains a Starlette deprecation warning about the currently supported `httpx` TestClient adapter. The real HTTP harness also uses `httpx`, independently of TestClient. A warning is not represented as an unqualified clean run.

## Fix first

1. **Control contention and define acceptance criteria.** The observed 503s and long tails rule out claiming reliable 100 RPS. Add bounded admission control and queue-depth/age limits, expose lock-wait and lease-expiry metrics, and establish latency/error/backlog budgets. Compare a single-host optimized SQLite design with a Postgres implementation on the intended hardware; do not assume changing the database alone proves capacity.
2. **Run the requested deployment and failure environment.** Execute `docker compose up`, two Linux SIGKILL seeds, a sustained open-loop 100-RPS mixed workload, and longer soaks. Gate on errors, tail latency, arrival scheduling lag, backlog growth and eventual recovery, not just final row counts. Include whole-service restart and storage faults.
3. **Prepare operation and real execution.** Add authentication/authorization with tenant-scoped idempotency, resource quotas, worker liveness alerts, backup/restore drills and graceful rollout behavior. Decide whether one-host availability is acceptable. Give real training execution a cancellation protocol and give external effects a destination-enforced idempotency/fencing or transactional outbox design. The current local result transaction does not make an external action exactly once.

## Reflection

The most surprising finding was that a chaos test could appear to be killing workers while addressing a virtual-environment launcher PID instead of the interpreter PID. Checking the worker's own identity exposed the faulty assumption before the run was accepted, and the failed experiment was retained. The remaining concern is the difference between correct final rows and a usable service: SQLite lock contention produced retries and long waits, while Windows process-kill evidence leaves the Linux/container and power-loss behavior unverified. The result transaction is a useful guarantee, but it does not make real training execution, external writes, or 100-RPS operation production-ready.

## References checked during implementation

- [SQLite isolation](https://www.sqlite.org/isolation.html): independent connections hide uncommitted writes; writers serialize; `BEGIN IMMEDIATE` starts a write transaction before reading.
- [SQLite WAL and its WAL-reset fix](https://www.sqlite.org/wal.html): WAL has deployment/version constraints. This prototype deliberately uses rollback journal mode instead of depending on a particular system SQLite WAL fix.
- [Python subprocess process termination](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.kill): POSIX SIGKILL and Windows TerminateProcess are distinct platform mechanisms. Windows venv launching was also checked against the installed `multiprocessing.popen_spawn_win32` implementation.
- [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/): validated request models; the actual installed package versions are locked with hashes.
- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/): service startup, health checks, and volumes. Configuration is supplied; container execution remains unverified.
