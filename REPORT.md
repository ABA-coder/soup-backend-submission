# Training queue: results and what I would fix next

**My verdict: DON'T SHIP at 100 requests per second.** The completed tests show that the queue can recover from worker failures without losing jobs or committing duplicate results. However, the load test also produced database lock timeouts, slow responses, and a growing backlog. I would not call that production-ready just because all jobs eventually finished.

I used AI assistance for implementation and testing, as described in [AI_USAGE.md](AI_USAGE.md). The conclusions below are based on the recorded test results.

## What was tested

I have included every chaos/load run and pytest transcript in [EVIDENCE_INDEX.md](EVIDENCE_INDEX.md), including the first failed experiment. The `evidence/` directory contains individual HTTP responses, retries, worker logs, inputs, summaries, source hashes, and environment details. Directory timestamps are in UTC. Failed results have not been removed or rewritten as successful ones.

The available machine ran Windows 11, Python 3.12.14, and SQLite 3.53.1, with 12 logical CPUs reported. The tests used a real HTTP server on loopback, separate worker processes, a local SQLite database, and synthetic jobs lasting 20-60 ms. Docker and a Linux distribution were not available, so **no Linux SIGKILL or Docker startup runs were executed**. I have included the Linux harness and CI workflow, but I would still need those runs before claiming that part of the brief is verified.

There were **three experiment invocations: one invalid/failed chaos run, one completed chaos run, and one completed load run**. There were also **three pytest invocations**, passing 14, 15, and 15 tests respectively. These are all the runs; there were no additional seeds or unreported experiments.

| Completed experiment | Final outcomes | Attempts / effects | Request evidence |
|---|---|---|---|
| Chaos: 10,000 jobs, eight workers, seed 1, 398.125 s | 7,815 succeeded; 218 deliberately failed; 1,967 cancelled; zero queued/running | 8,364 attempts; 8,033 effects; 303 jobs retried | 195 kills, 94 with an observed claim; 5,000 repeated POSTs; 2,000 cancel requests; 153 HTTP 503s |
| Load: 3,000 creates offered at 100 RPS, eight workers, seed 3, 85.672 s to drain/check | 2,918 succeeded; 82 deliberately failed; zero queued/running | 3,066 attempts; 3,000 effects; 60 jobs retried despite no kills | 83 of 3,000 initial POSTs returned 503 (2.77%); 84 total 503s including retries; initial-POST p95 3,563 ms, p99 5,922 ms |

In both completed runs, the final checks accounted for every input key. Each succeeded or failed job had exactly one matching result row, cancelled jobs had none, and no job remained queued or running. Attempt counts, fencing tokens, SQLite integrity, and foreign keys also matched. The failed jobs in the table were deliberately configured to fail; they were not lost jobs.

The load result is the main reason for my verdict. New POSTs were scheduled over approximately 30 seconds, with a maximum scheduling lag of 31 ms. All successful responses, including retries, arrived about 34.9 seconds after submission began. At the next progress sample, **2,484 jobs were still queued**. The backlog eventually drained, but I would not describe that as sustained capacity at 100 RPS. Also, 60 jobs were retried even though no workers were deliberately killed in this run. That is consistent with storage contention outlasting the 1.5-second test lease. Fencing protected the committed results, but it did not prevent repeated attempts. This was a POST-only load test; a sustained mix of creates, reads, cancellations, and worker failures remains untested.

One logging detail is worth clarifying: in the saved load run, `arrival_schedule.elapsed_s` measures the submission phase, while other events measure time since harness start. The final code calls that field `workload_elapsed_s` to avoid confusion. This only changes the field name; the raw log and recorded measurements are unchanged, and the experiment was not rerun for that rename. Each run includes its original, hash-verified source in `source/`.

## What went wrong in the first run

The first chaos run was stopped at about 79 seconds because its kill coverage was invalid. The warning sign was that every sampled killed process appeared to hold no job, even while workers were processing jobs. Inspection showed that `Popen.pid` referred to the Windows virtual-environment launcher, while the worker reported a different interpreter PID. I could not use a run with that mismatch as evidence that worker failures were handled correctly.

The API was manually stopped to make the experiment fail and shut down. `intervention.json` records why. The connection errors after that intervention were caused by stopping the API, so I would not attribute them to an unexplained queue failure.

The fix follows the approach in Python's Windows multiprocessing code: launch the base interpreter directly and set `__PYVENV_LAUNCHER__` to the virtual-environment executable. A new regression test checks that the child's reported PID matches the process being killed. The harness also verifies initial worker PIDs before submitting jobs and records whether a selected victim has an active claim. Replacement workers use the same launcher. I kept the failed run in the submission. Its earlier 503 responses still show contention, but I do not count its kill calls as valid worker-failure coverage.

There is one consequence of editing the harness while that run was stopping: some traceback source lines refer to the newer file. The initial log still contains the original source hashes. The pre-fix files were recovered from the edits and checked against those SHA-256 hashes; they are included in the failed run's `source/` directory. The JSON events and HTTP logs have not been changed.

## How the silent failure cases were checked

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

I kept a unique constraint on `effects.job_id` as an extra safeguard. A duplicate insert raises an error; it is not silently discarded with `INSERT OR IGNORE`. Unexpected database errors terminate the worker, and an unexplained worker exit fails the harness. The final database checks compare counts, outcomes, tokens, and payload digests independently of the worker's success messages.

## What I still cannot conclude

I do not think 10,000 jobs are enough to prove the service safe for every failure. A seed makes the chosen jobs and victims repeatable, but it does not reproduce every thread or process schedule. Killing a process also does not simulate a power cut or storage that fails to honour `fsync`. The transaction-boundary tests cover rollback and a lost completion acknowledgement, but not every possible storage failure.

The large run kept the API and database host alive. There were no tests for disk-full conditions, corruption, network partitions, clocks on multiple hosts, malicious clients, real training workloads, external effects, backup restoration, or a long soak. Clock jumps were injected through a test seam rather than real NTP changes. The health endpoint only proves that the database is readable; it does not establish that workers are making progress.

The first pytest run passed 14 tests. Adding the PID regression test brought that to 15, and the third run passed all 15 again after three unused imports were removed. Those import changes do not alter storage or worker behaviour, and the source hashes identify the tested versions. The transcripts also contain a Starlette deprecation warning for the `httpx` TestClient adapter. I have left that warning visible. The separate HTTP harness uses real network requests rather than TestClient.

## What I would fix first

1. **Address contention and set measurable limits.** I would start with bounded request admission, queue depth and age limits, and metrics for lock waits and expired leases. I would define acceptable response latency, error rate, and backlog growth before another capacity test. Then I would compare improvements to this SQLite setup with a Postgres implementation on the intended hardware. Switching databases alone would not prove the problem solved.
2. **Complete the deployment and failure checks.** I would run `docker compose up`, two Linux SIGKILL seeds, a sustained open-loop workload at 100 RPS with mixed operations, and longer soak tests. I would also test whole-service restarts and storage failures. The decision should depend on errors, tail latency, scheduling lag, backlog, and recovery, rather than final row counts alone.
3. **Prepare for real users and real training.** I would add authentication and authorization, tenant-scoped idempotency keys, quotas, worker-progress alerts, backup/restore checks, and graceful rollouts. I would also decide whether a single-host service meets the availability requirement. Real training needs its own cancellation mechanism. External effects need destination-enforced idempotency/fencing or a transactional outbox design; the local SQLite transaction does not make a remote action happen exactly once.

## Reflection

What surprised me most was how easy it was for the chaos harness itself to give misleading evidence: it was targeting a virtual-environment launcher PID rather than the worker's interpreter PID. Checking the worker's reported identity exposed the mistake, and I kept that failed run instead of presenting only the corrected result. My main remaining concern is that correct final rows do not necessarily mean a usable service. Lock contention caused retries, slow requests, and repeated attempts even without deliberate worker kills. The transaction around the result provides a useful guarantee, but I would still need Linux/container validation, storage-failure testing, and much better load behaviour before shipping this for real training jobs.

## References used during implementation

- [SQLite isolation](https://www.sqlite.org/isolation.html): independent connections hide uncommitted writes; writers serialize; `BEGIN IMMEDIATE` starts a write transaction before reading.
- [SQLite WAL and its WAL-reset fix](https://www.sqlite.org/wal.html): WAL has deployment/version constraints. This prototype deliberately uses rollback journal mode instead of depending on a particular system SQLite WAL fix.
- [Python subprocess process termination](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.kill): POSIX SIGKILL and Windows TerminateProcess are distinct platform mechanisms. Windows venv launching was also checked against the installed `multiprocessing.popen_spawn_win32` implementation.
- [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/): validated request models; the actual installed package versions are locked with hashes.
- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/): service startup, health checks, and volumes. Configuration is supplied; container execution remains unverified.
