# Soup training-job queue

A FastAPI service with a durable SQLite queue, lease recovery, fencing tokens, transactional result writes, and four independent workers by default. Choose the Backend Engineer task only.

**Production verdict: DON'T SHIP at 100 requests per second yet.** See [REPORT.md](REPORT.md) for measured results, all run counts, the failed experiment, and the remaining evidence gaps. This repository is a working prototype with an explicit failure model, not a production-readiness claim.

## Run

Requires Docker Engine and Docker Compose v2:

```sh
docker compose up --build
```

The API is at `http://localhost:8000`; interactive API documentation is at `http://localhost:8000/docs`. Compose runs one API and a supervisor with **four actual worker processes**. SQLite persists in the `queue-data` named volume. `docker compose down` preserves that data. Do not mount the database on NFS or share it across hosts.

Docker is not installed in the authoring environment. The Compose configuration and Linux verification workflow are supplied, but a successful container build/start is **not claimed**. Native Windows tests and the HTTP harness were actually executed; see the evidence.

Local alternative (Python 3.12+):

```sh
python -m venv .venv
# POSIX: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.txt
python -m uvicorn queue_service.api:app --host 127.0.0.1 --port 8000
```

In another terminal with the same environment:

```sh
python -m queue_service.supervisor --workers 4
```

`QUEUE_DB` selects the database path (default `data/queue.db`). Keep the API and workers on the same database. A direct worker can set `--lease-ms` (default 3000, minimum 300). Lease tuning must accommodate storage contention and runtime pauses.

## API contract

| Operation | Request | Response |
|---|---|---|
| Create | `POST /jobs`, `Idempotency-Key` header, JSON payload | 201 on insert; 200 on identical replay; 409 on changed payload |
| Get | `GET /jobs/{id}` | 200 job or 404 |
| List | `GET /jobs?after=0&limit=100` | `items`, `snapshot`, `next_after` |
| Next page | `GET /jobs?after={next_after}&snapshot={snapshot}&limit=100` | Same ID-bounded membership; current statuses |
| Cancel | `POST /jobs/{id}/cancel` | 200 cancelled/repeated cancel; 409 if completion won; 404 if absent |
| Health | `GET /health` | 200 if the database is readable; does not prove worker liveness |

Keys contain 1-200 printable, non-space ASCII characters. Keys are global and retained permanently. Request defaults are normalized before comparison. Unknown fields, wrong types, and invalid values return 422. Database lock timeouts return 503 with `Retry-After: 1`; retry creation with the original key. Unexpected database errors are not converted into success.

Payload: `name` is 1-100 characters (default `training`), `duration_ms` is an integer 0-60000 (default 100), and `fail` is a boolean (default false). The emulator waits, then atomically writes a result containing a payload digest and an outcome. `fail: true` deliberately produces a failed job with one failed-result effect. These are synthetic jobs; no arbitrary code is executed.

```sh
curl -i -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-001' \
  -d '{"name":"example","duration_ms":2000,"fail":false}'
curl http://localhost:8000/jobs/1
curl 'http://localhost:8000/jobs?limit=10'
curl -X POST http://localhost:8000/jobs/1/cancel
```

On Windows use `curl.exe` and your shell's JSON quoting, or use `/docs`.

## Guarantee

**At-least-once attempts; exactly one committed local result for succeeded/failed jobs; zero results for cancelled jobs.** Attempt records count claims, which may include a process killed before useful computation. Result rows count the actual emulated durable effect. These two counts intentionally differ. This does not promise exactly-once CPU/GPU execution or a single external HTTP/file side effect.

All state-changing operations acquire `BEGIN IMMEDIATE` before reading state. A completion must hold the current worker identity, generation token, running state, and live lease. The effect and final state commit together. Cancellation and completion serialize; the first commit determines the result. See the one-page [design note](DESIGN.md), recorded in the repository's first commit before code.

## Reproduce the experiments

```sh
python -m harness.run_tests
python -m harness.chaos --seed 1
python -m harness.chaos --seed 2
python -m harness.chaos --mode load --jobs 3000 --rate 100
```

The chaos defaults are **10,000 unique input keys, eight worker processes, 5,000 repeated POSTs, 2,000 attempted cancellations, and a random worker hard-killed every 1.8-2.2 seconds**. Delays from scheduling/lock contention can stretch the interval and are visible in the raw timestamps. Each killed worker is replaced. Every run gets a new directory and a fresh database; failures are retained, never overwritten. The deterministic seed selects inputs and victims, not OS scheduling.

On Linux, the harness calls `os.kill(pid, signal.SIGKILL)` (the same signal as `kill -9`). On Windows it calls `TerminateProcess` through `Popen.kill`; that is separately identified in every run. The harness verifies that each initial worker's self-reported PID equals its process handle, preventing the Windows venv-launcher mistake documented in the report.

Inside Docker (the harness launches its own API and eight workers):

```sh
mkdir -p evidence
docker compose --profile test run --build --rm --user "$(id -u):$(id -g)" chaos
```

The `--user` argument is for POSIX bind-mount ownership. On Docker Desktop for Windows, use `docker compose --profile test run --build --rm chaos`. Run successive seeds by appending `python -m harness.chaos --seed 2 --output /evidence` to the service command. A bind-mounted evidence directory preserves logs even when the test exits unsuccessfully.

The GitHub Actions workflow runs targeted tests, two Linux chaos seeds, a 100-RPS experiment, and a Compose startup smoke check. It uploads evidence even on failure. The workflow is provided but **has not been run on GitHub**.

## Evidence and boundaries

Each run contains `inputs.jsonl`, `requests.jsonl` with individual responses/latencies/retries, `run.jsonl`, `summary.json`, API/worker logs, and a retained database snapshot. Source hashes and the Python/SQLite/platform versions appear in the initial run record. `harness.run_tests` retains a new complete pytest transcript per invocation. Database snapshots are included in the distribution archive but ignored by Git to avoid treating runtime databases as source.

The independent oracle reconciles every intended key and payload, POST response identity, pagination membership, terminal state, accepted/late cancellation, attempt count, effect count/outcome/digest/token, and SQLite integrity/foreign keys. A run fails on timeouts, unexpected worker exits, missing inputs, mismatches, or lack of meaningful kill coverage. `PASS` means those checks passed for that run; it does not imply a latency SLO or 100-RPS production approval.

The harness does not simulate power failure, disk corruption/full disk, API/database restarts during the large run, malicious requests, network partitions, external effects, months of data growth, multi-host operation, or every possible scheduler interleaving. Separate targeted tests cover commit-boundary kills, expired zombies, clock jumps, long-job heartbeats, and cancellation races. Cancellation of actual GPU jobs and real training outputs would need a different execution adapter.

See [AI_USAGE.md](AI_USAGE.md) for the actual use of AI and [SUBMISSION_EMAIL.txt](SUBMISSION_EMAIL.txt) for a draft reply. No submission email has been sent.
