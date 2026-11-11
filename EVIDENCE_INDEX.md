# Evidence index

Every experiment invoked during this implementation is listed below. A FAIL/aborted run remains part of the record. Windows results are not Linux SIGKILL evidence.

Experiment invocations: **3**; outcomes: **{'FAIL': 1, 'PASS': 2}**. Pytest invocations: **3**. Linux SIGKILL invocations: **0**. Container startup validation is tracked separately in REPORT.md.

## Targeted test transcripts

- [tests-20261111T185748Z-431eea.log](evidence/tests-20261111T185748Z-431eea.log): 14 passed, 1 warning in 11.25s; EXIT_CODE=0
- [tests-20261111T185923Z-972f46.log](evidence/tests-20261111T185923Z-972f46.log): 15 passed, 1 warning in 7.97s; EXIT_CODE=0
- [tests-20261111T190610Z-bc3d4e.log](evidence/tests-20261111T190610Z-bc3d4e.log): 15 passed, 1 warning in 7.78s; EXIT_CODE=0

## 20261111T185803Z-chaos-seed1-4949f2

**FAIL** | chaos | 10,000 intended jobs | 8 workers | seed 1 | Windows TerminateProcess

[Raw events](evidence/20261111T185803Z-chaos-seed1-4949f2/run.jsonl) | [Every HTTP response](evidence/20261111T185803Z-chaos-seed1-4949f2/requests.jsonl) | [Summary](evidence/20261111T185803Z-chaos-seed1-4949f2/summary.json) | [Input manifest](evidence/20261111T185803Z-chaos-seed1-4949f2/inputs.jsonl)

```json
{
  "invariants": "FAIL",
  "error": "AssertionError('POST seed-1-job-3918: 0 None')",
  "kills": 38,
  "elapsed_s": 78.781
}
```

[Manual intervention record](evidence/20261111T185803Z-chaos-seed1-4949f2/intervention.json). This run is invalid as worker-kill coverage; see REPORT.md.

Initial POST attempts (before retries): **5750**. HTTP statuses: `{201: 5612, 503: 56, 200: 66, 0: 16}`. Latency in ms: `{'p50': 16.0, 'p95': 640.0, 'p99': 5812.0, 'max': 6063.0}`.

Observed kill intervals: mean 1.983s; min 1.812s; max 2.172s.

## 20261111T185939Z-chaos-seed1-4e5ed6

**PASS** | chaos | 10,000 intended jobs | 8 workers | seed 1 | Windows TerminateProcess

[Raw events](evidence/20261111T185939Z-chaos-seed1-4e5ed6/run.jsonl) | [Every HTTP response](evidence/20261111T185939Z-chaos-seed1-4e5ed6/requests.jsonl) | [Summary](evidence/20261111T185939Z-chaos-seed1-4e5ed6/summary.json) | [Input manifest](evidence/20261111T185939Z-chaos-seed1-4e5ed6/inputs.jsonl)

```json
{
  "jobs": 10000,
  "states": {
    "failed": 218,
    "succeeded": 7815,
    "cancelled": 1967
  },
  "effects": 8033,
  "attempts": 8364,
  "jobs_retried": 303,
  "kills": 195,
  "kills_with_observed_claim": 94,
  "accepted_cancels": 1967,
  "late_cancels": 33,
  "http_statuses": {
    "201": 10000,
    "409": 33,
    "200": 6997,
    "503": 153
  },
  "request_retries": 153,
  "latency_ms": {
    "p50": 16.0,
    "p95": 406.0,
    "p99": 5578.0,
    "max": 6078.0
  },
  "elapsed_s": 398.125,
  "invariants": "PASS"
}
```

Initial POST attempts (before retries): **15000**. HTTP statuses: `{201: 9896, 503: 118, 200: 4986}`. Latency in ms: `{'p50': 16.0, 'p95': 328.0, 'p99': 5219.0, 'max': 6078.0}`.

Observed kill intervals: mean 2.024s; min 1.797s; max 2.281s.

## 20261111T190632Z-load-seed3-bdeb49

**PASS** | load | 3,000 intended jobs | 8 workers | seed 3 | Windows TerminateProcess

[Raw events](evidence/20261111T190632Z-load-seed3-bdeb49/run.jsonl) | [Every HTTP response](evidence/20261111T190632Z-load-seed3-bdeb49/requests.jsonl) | [Summary](evidence/20261111T190632Z-load-seed3-bdeb49/summary.json) | [Input manifest](evidence/20261111T190632Z-load-seed3-bdeb49/inputs.jsonl)

```json
{
  "jobs": 3000,
  "states": {
    "failed": 82,
    "succeeded": 2918
  },
  "effects": 3000,
  "attempts": 3066,
  "jobs_retried": 60,
  "kills": 0,
  "kills_with_observed_claim": 0,
  "accepted_cancels": 0,
  "late_cancels": 0,
  "http_statuses": {
    "201": 3000,
    "503": 84,
    "200": 23
  },
  "request_retries": 84,
  "latency_ms": {
    "p50": 46.0,
    "p95": 3453.0,
    "p99": 5922.0,
    "max": 6046.0
  },
  "elapsed_s": 85.672,
  "invariants": "PASS"
}
```

Initial POST attempts (before retries): **3000**. HTTP statuses: `{201: 2917, 503: 83}`. Latency in ms: `{'p50': 46.0, 'p95': 3563.0, 'p99': 5922.0, 'max': 6046.0}`.

Open-loop arrival schedule (includes any client scheduling lag):

```json
{
  "elapsed_s": 34.92200000002049,
  "event": "arrival_schedule",
  "target_rps": 100.0,
  "max_lag_ms": 31.000000017229468
}
```
