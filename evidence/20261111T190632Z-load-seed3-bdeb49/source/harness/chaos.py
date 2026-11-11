"""Run against real HTTP and separate worker processes; retain failures and raw evidence."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

import httpx

from queue_service.storage import Store
from queue_service.processes import python_process


ROOT = Path(__file__).resolve().parents[1]


class Experiment:
    def __init__(self, args):
        self.args = args
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.directory = Path(args.output) / f'{stamp}-{args.mode}-seed{args.seed}-{uuid.uuid4().hex[:6]}'
        self.directory.mkdir(parents=True, exist_ok=False)
        self.raw = (self.directory / 'run.jsonl').open('x', encoding='utf-8', buffering=1)
        self.requests = (self.directory / 'requests.jsonl').open('x', encoding='utf-8', buffering=1)
        self.started = time.monotonic()
        self.store = Store(self.directory / 'snapshot.sqlite')
        self.children = []
        self.handles = []
        self.workers = []
        self.ids = {}
        self.accepted_cancels = set()
        self.conflicting_cancels = set()
        self.http_codes = Counter()
        self.latencies = []
        self.errors = []
        self.kills = 0
        self.kills_with_claim = 0
        self.request_retries = 0
        self.stop = asyncio.Event()
        self.cancel_queue = asyncio.Queue()
        rng = random.Random(args.seed)
        self.expected = {
            f'seed-{args.seed}-job-{i}': {
                'name': f'training-{i}',
                'duration_ms': rng.randint(args.duration_min, args.duration_max),
                'fail': i % 37 == 0,
            } for i in range(args.jobs)
        }
        with (self.directory / 'inputs.jsonl').open('x', encoding='utf-8') as f:
            for key, payload in self.expected.items():
                f.write(json.dumps({'key': key, 'payload': payload}) + '\n')

    def log(self, event, **data):
        line = json.dumps({'elapsed_s': round(time.monotonic() - self.started, 3), 'event': event, **data})
        print(line, flush=True)
        self.raw.write(line + '\n')

    def spawn(self, name, command, env=None):
        handle = (self.directory / f'{name}.log').open('x', encoding='utf-8')
        self.handles.append(handle)
        process = python_process(command[1:], cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        self.children.append(process)
        return process

    def worker(self, slot, generation):
        return self.spawn(f'worker-{slot}-{generation}', [
            sys.executable, '-u', '-m', 'queue_service.worker',
            '--db', str(Path(self.store.path).resolve()), '--lease-ms', str(self.args.lease_ms),
        ])

    async def request(self, client, method, path, key=None, payload=None, retry=True):
        attempts = 6 if retry else 1
        for attempt in range(attempts):
            started = time.monotonic()
            response = None
            error = None
            try:
                response = await client.request(method, path, json=payload,
                                                headers={'Idempotency-Key': key} if key else None)
                data = response.json()
            except (httpx.TransportError, ValueError) as exc:
                error = repr(exc)
                data = None
            elapsed = (time.monotonic() - started) * 1000
            self.latencies.append(elapsed)
            status = response.status_code if response is not None else 0
            self.http_codes[status] += 1
            self.requests.write(json.dumps({
                'elapsed_s': round(time.monotonic() - self.started, 4), 'method': method, 'path': path,
                'key': key, 'attempt': attempt, 'status': status, 'latency_ms': round(elapsed, 3),
                'response': data, 'error': error,
            }) + '\n')
            if status == 503 or error:
                self.request_retries += 1
                self.log('request_retry', path=path, key=key, status=status, error=error, attempt=attempt)
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.05 * 2**attempt, 1))
                    continue
            return status, data
        raise AssertionError('unreachable')

    async def post(self, client, key):
        status, data = await self.request(client, 'POST', '/jobs', key, self.expected[key])
        if status not in (200, 201):
            raise AssertionError(f'POST {key}: {status} {data}')
        job_id = data['id']
        if data['idempotency_key'] != key or data['payload'] != self.expected[key]:
            raise AssertionError(f'Wrong POST response for {key}')
        previous = self.ids.setdefault(key, job_id)
        if previous != job_id:
            raise AssertionError(f'Idempotency violation: {key}: {previous} != {job_id}')
        return job_id

    async def creator(self, client, keys):
        for key in keys:
            job_id = await self.post(client, key)
            if int(key.rsplit('-', 1)[1]) % 5 == 0:
                await self.cancel_queue.put(job_id)

    async def repeats(self, client):
        rng = random.Random(self.args.seed + 100)
        keys = list(self.expected)
        for _ in range(self.args.jobs // 2):
            await self.post(client, rng.choice(keys))
            await asyncio.sleep(0.002)

    async def canceller(self, client):
        while True:
            job_id = await self.cancel_queue.get()
            if job_id is None:
                return
            status, data = await self.request(client, 'POST', f'/jobs/{job_id}/cancel')
            if status == 200 and data['state'] == 'cancelled':
                self.accepted_cancels.add(job_id)
            elif status == 409:
                self.conflicting_cancels.add(job_id)
            else:
                raise AssertionError(f'Cancel {job_id}: {status} {data}')

    async def killer(self):
        rng = random.Random(self.args.seed + 200)
        generations = [0] * self.args.workers
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), rng.uniform(1.8, 2.2))
                return
            except TimeoutError:
                pass
            slot = rng.randrange(len(self.workers))
            process = self.workers[slot]
            if process.poll() is not None:
                raise AssertionError(f'Worker {process.pid} exited unexpectedly: {process.returncode}')
            with self.store.read() as db:
                claimed = [r[0] for r in db.execute(
                    "SELECT id FROM jobs WHERE state='running' AND owner LIKE ?", (f'{process.pid}-%',)
                )]
            if os.name == 'posix':
                os.kill(process.pid, signal.SIGKILL)
            else:
                process.kill()  # Windows TerminateProcess, explicitly not POSIX SIGKILL.
            await asyncio.to_thread(process.wait, 10)
            self.kills += 1
            self.kills_with_claim += bool(claimed)
            self.log('worker_killed', pid=process.pid, slot=slot, jobs=claimed, returncode=process.returncode)
            generations[slot] += 1
            self.workers[slot] = self.worker(slot, generations[slot])

    async def progress(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
                return
            except TimeoutError:
                self.log('progress', known_keys=len(self.ids), states=self.store.counts(), kills=self.kills)

    async def submit(self, client):
        keys = list(self.expected)
        cancellers = [asyncio.create_task(self.canceller(client)) for _ in range(2)]
        try:
            if self.args.mode == 'load':
                # Open-loop scheduling. Record lag so slow arrivals cannot masquerade as 100 RPS.
                started = time.monotonic()
                lags = []
                tasks = []
                for i, key in enumerate(keys):
                    due = started + i / self.args.rate
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    lags.append(max(0, time.monotonic() - due) * 1000)
                    tasks.append(asyncio.create_task(self.post(client, key)))
                await asyncio.gather(*tasks)
                self.log('arrival_schedule', target_rps=self.args.rate, max_lag_ms=max(lags),
                         elapsed_s=time.monotonic() - started)
            else:
                await asyncio.gather(
                    *(self.creator(client, keys[i::16]) for i in range(16)), self.repeats(client)
                )
            for _ in cancellers:
                await self.cancel_queue.put(None)
            await asyncio.gather(*cancellers)
        finally:
            for task in cancellers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*cancellers, return_exceptions=True)

    async def verify_api(self, client):
        observed = {}
        after = 0
        snapshot = None
        while True:
            path = f'/jobs?after={after}&limit=1000'
            if snapshot is not None:
                path += f'&snapshot={snapshot}'
            status, data = await self.request(client, 'GET', path)
            assert status == 200, (status, data)
            snapshot = data['snapshot']
            for job in data['items']:
                assert job['id'] not in observed, 'duplicate pagination entry'
                observed[job['id']] = job
            if data['next_after'] is None:
                break
            assert data['next_after'] > after
            after = data['next_after']
        assert set(observed) == set(self.ids.values()), 'API pagination lost or added jobs'
        for key, job_id in self.ids.items():
            job = observed[job_id]
            assert job['idempotency_key'] == key and job['payload'] == self.expected[key]
            assert job['state'] in ('succeeded', 'failed', 'cancelled')
        for job_id in list(observed)[::max(1, len(observed) // 20)]:
            status, job = await self.request(client, 'GET', f'/jobs/{job_id}')
            assert status == 200 and job == observed[job_id], 'GET/list disagree'

    def verify_database(self):
        # Independent SQL oracle; does not call Store.finish or mirror its guard.
        with self.store.read() as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            jobs = [dict(r) for r in db.execute('SELECT * FROM jobs')]
            effects = [dict(r) for r in db.execute('SELECT * FROM effects')]
            attempts = [dict(r) for r in db.execute('SELECT * FROM attempts')]
        assert {j['idempotency_key'] for j in jobs} == set(self.expected), 'missing/unexpected key'
        assert len(jobs) == self.args.jobs and len(self.ids) == self.args.jobs
        counts = Counter(e['job_id'] for e in effects)
        assert all(n == 1 for n in counts.values()), 'multiple effects'
        effect_by_job = {e['job_id']: e for e in effects}
        attempt_counts = Counter(a['job_id'] for a in attempts)
        attempt_tokens = {(a['job_id'], a['token']) for a in attempts}
        for job in jobs:
            job_id = job['id']
            key = job['idempotency_key']
            payload = self.expected[key]
            assert self.ids[key] == job_id and json.loads(job['payload']) == payload
            assert job['state'] in ('succeeded', 'failed', 'cancelled'), 'non-terminal job'
            assert job['owner'] is None and job['lease_until_ms'] is None and job['finished_at_ms'] is not None
            assert job['attempts'] == attempt_counts[job_id]
            if job['state'] == 'cancelled':
                assert counts[job_id] == 0 and job_id in self.accepted_cancels
            else:
                assert counts[job_id] == 1 and job['attempts'] >= 1
                effect = effect_by_job[job_id]
                assert effect['outcome'] == job['state'] == ('failed' if payload['fail'] else 'succeeded')
                assert effect['token'] == job['token']
                assert (job_id, effect['token']) in attempt_tokens
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                assert json.loads(effect['result']) == {'payload_sha256': digest, 'outcome': job['state']}
            if job_id in self.accepted_cancels:
                assert job['state'] == 'cancelled'
            if job_id in self.conflicting_cancels:
                assert job['state'] in ('succeeded', 'failed')
        if self.args.mode == 'chaos':
            assert self.kills > 0, 'chaos run was too short to kill a worker'
            assert self.kills_with_claim > 0, 'no killed worker held an observed claim'
            assert self.accepted_cancels, 'no cancellation was exercised'
        ordered = sorted(self.latencies)
        return {
            'jobs': len(jobs), 'states': dict(Counter(j['state'] for j in jobs)),
            'effects': len(effects), 'attempts': len(attempts),
            'jobs_retried': sum(j['attempts'] > 1 for j in jobs),
            'kills': self.kills, 'kills_with_observed_claim': self.kills_with_claim,
            'accepted_cancels': len(self.accepted_cancels), 'late_cancels': len(self.conflicting_cancels),
            'http_statuses': dict(self.http_codes), 'request_retries': self.request_retries,
            'latency_ms': {name: round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 3)
                           for name, p in [('p50', .50), ('p95', .95), ('p99', .99), ('max', 1)]},
            'elapsed_s': round(time.monotonic() - self.started, 3),
            'invariants': 'PASS',
        }

    async def execute(self):
        hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for folder in ('queue_service', 'harness', 'tests')
                  for p in (ROOT / folder).rglob('*.py')}
        self.log('start', arguments=vars(self.args), python=sys.version, sqlite=sqlite3.sqlite_version,
                 platform=platform.platform(), cpu_count=os.cpu_count(), source_sha256=hashes,
                 kill_method='SIGKILL' if os.name == 'posix' else 'Windows TerminateProcess')
        self.store.initialize()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, QUEUE_DB=str(Path(self.store.path).resolve()), PYTHONUNBUFFERED='1')
        api = self.spawn('api', [sys.executable, '-m', 'uvicorn', 'queue_service.api:app',
                                 '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], env)
        background = []
        try:
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=15,
                                         limits=httpx.Limits(max_connections=100)) as client:
                for _ in range(150):
                    if api.poll() is not None:
                        raise RuntimeError(f'API exited {api.returncode}; see api.log')
                    try:
                        if (await client.get('/health')).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise TimeoutError('API did not start')
                self.workers = [self.worker(i, 0) for i in range(self.args.workers)]
                # A kill test is invalid if a launcher PID differs from its worker.
                for slot, process in enumerate(self.workers):
                    log_path = self.directory / f'worker-{slot}-0.log'
                    for _ in range(150):
                        lines = log_path.read_text(encoding='utf-8').splitlines()
                        if lines:
                            first = json.loads(lines[0])
                            assert first['event'] == 'worker_started' and first['pid'] == process.pid, 'Worker PID mismatch'
                            break
                        if process.poll() is not None:
                            raise RuntimeError(f'Worker exited on startup: {process.returncode}')
                        await asyncio.sleep(0.1)
                    else:
                        raise TimeoutError('Worker failed to announce PID')
                self.log('worker_pids_verified', pids=[p.pid for p in self.workers])
                background.append(asyncio.create_task(self.progress()))
                if self.args.mode == 'chaos':
                    background.append(asyncio.create_task(self.killer()))
                async with asyncio.timeout(self.args.timeout):
                    await self.submit(client)
                    self.log('submissions_done', keys=len(self.ids))
                    while True:
                        for task in background:
                            if task.done() and task.exception():
                                raise task.exception()
                        for process in self.workers:
                            if process.poll() is not None:
                                raise AssertionError(f'Unexpected worker exit: {process.pid} {process.returncode}')
                        counts = self.store.counts()
                        if counts.get('queued', 0) + counts.get('running', 0) == 0:
                            break
                        await asyncio.sleep(0.2)
                self.stop.set()
                await asyncio.gather(*background)
                await self.verify_api(client)
                result = self.verify_database()
                self.log('result', **result)
                (self.directory / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        except BaseException as exc:
            self.log('FAILED', error=repr(exc), traceback=traceback.format_exc())
            (self.directory / 'summary.json').write_text(json.dumps({
                'invariants': 'FAIL', 'error': repr(exc), 'kills': self.kills,
                'elapsed_s': round(time.monotonic() - self.started, 3),
            }, indent=2), encoding='utf-8')
            raise
        finally:
            self.stop.set()
            for task in background:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            for process in self.children:
                if process.poll() is None:
                    process.terminate()
            for process in self.children:
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            for handle in self.handles:
                handle.close()
            self.raw.close()
            self.requests.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', type=int, default=10000)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--output', default='evidence')
    parser.add_argument('--lease-ms', type=int, default=1500)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--duration-min', type=int, default=20)
    parser.add_argument('--duration-max', type=int, default=60)
    parser.add_argument('--mode', choices=['chaos', 'load'], default='chaos')
    parser.add_argument('--rate', type=float, default=100)
    args = parser.parse_args()
    if not (args.jobs > 0 and args.workers > 0 and args.lease_ms >= 300 and args.rate > 0
            and 0 <= args.duration_min <= args.duration_max <= 60000):
        parser.error('Invalid jobs, workers, lease, rate, or duration range')
    asyncio.run(Experiment(args).execute())


if __name__ == '__main__':
    main()
