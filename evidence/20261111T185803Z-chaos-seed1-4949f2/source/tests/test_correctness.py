from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from queue_service.api import create_app
from queue_service.storage import Conflict, Store


PAYLOAD = {'name': 'test', 'duration_ms': 0, 'fail': False}


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.initialize()
    return store


def test_concurrent_idempotency_and_payload_conflict(store):
    barrier = threading.Barrier(16)

    def create(i):
        barrier.wait()
        return store.create('same-key', PAYLOAD)

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(create, range(16)))
    assert len({job['id'] for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with pytest.raises(Conflict):
        store.create('same-key', {**PAYLOAD, 'fail': True})


def test_eight_simultaneous_claims_have_one_owner(store):
    store.create('one-job', PAYLOAD)
    barrier = threading.Barrier(8)

    def claim(i):
        barrier.wait()
        return store.claim(f'worker-{i}', 10000)

    with ThreadPoolExecutor(8) as pool:
        jobs = list(pool.map(claim, range(8)))
    assert sum(job is not None for job in jobs) == 1
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1


class ClockStore(Store):
    now = 100000

    def _now(self, db):
        return self.now


def test_expired_zombie_cannot_renew_or_finish_even_before_reclaim(store):
    clock = ClockStore(store.path)
    clock.create('zombie', PAYLOAD)
    old = clock.claim('old', 1000)
    clock.now += 1000
    assert not clock.renew(old, 1000)
    assert not clock.finish(old)
    new = clock.claim('new', 1000)
    assert new['token'] > old['token']
    assert not clock.finish(old)
    assert not clock.renew(old, 1000)
    assert clock.finish(new)
    assert not clock.finish(new)
    with clock.read() as db:
        assert db.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 1


def test_worker_clock_skew_is_ignored_and_host_clock_jump_is_fenced(store):
    store.create('local-clock', PAYLOAD)
    with patch('time.time', return_value=-10**12):
        job = store.claim('worker', 5000)
    with patch('time.time', return_value=10**12):
        assert store.renew(job, 5000)
        assert store.finish(job)

    clock = ClockStore(store.path)
    clock.create('host-clock', PAYLOAD)
    old = clock.claim('old', 1000)
    clock.now -= 5000
    assert clock.claim('other', 1000) is None  # Backward jump delays recovery.
    clock.now += 10000
    new = clock.claim('new', 1000)  # Forward jump can reclaim early.
    clock.now -= 4000
    assert not clock.finish(old)  # Restoring the clock cannot restore an old token.
    assert clock.finish(new)


@pytest.mark.parametrize('first', ['cancel', 'complete'])
def test_cancel_completion_order_is_explicit(store, first):
    job_id = store.create('race', PAYLOAD)[0]['id']
    job = store.claim('worker', 10000)
    if first == 'cancel':
        assert store.cancel(job_id)['state'] == 'cancelled'
        assert not store.finish(job)
        assert not store.renew(job, 10000)
        assert store.cancel(job_id)['state'] == 'cancelled'
    else:
        assert store.finish(job)
        with pytest.raises(Conflict):
            store.cancel(job_id)


def test_real_cancel_complete_races_remain_consistent(store):
    for i in range(30):
        job_id = store.create(f'race-{i}', PAYLOAD)[0]['id']
        job = store.claim('worker', 10000)
        barrier = threading.Barrier(2)

        def cancel():
            barrier.wait()
            try:
                return store.cancel(job_id)['state']
            except Conflict:
                return 'conflict'

        def finish():
            barrier.wait()
            return store.finish(job)

        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(cancel), pool.submit(finish)
            cancelled, completed = a.result(), b.result()
        assert (cancelled, completed) in [('cancelled', False), ('conflict', True)]
        with store.read() as db:
            effects = db.execute('SELECT COUNT(*) FROM effects WHERE job_id=?', (job_id,)).fetchone()[0]
        assert effects == int(completed)


def test_isolation_hides_partial_effect_and_locks_other_writer(store):
    store.create('isolation', PAYLOAD)
    job = store.claim('one', 10000)
    with store.write() as writer:
        store._insert_effect(writer, job, 'succeeded', store._now(writer))
        with store.read() as reader:
            assert reader.execute('PRAGMA read_uncommitted').fetchone()[0] == 0
            assert reader.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
            assert reader.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
        contender = Store(store.path, busy_ms=25)
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            contender.cancel(job['id'])
        # Roll back the deliberately partial transaction, including the effect.
        writer.rollback()
    assert store.finish(job)


def crash_at_commit_boundary(path, ready, after_commit):
    class PausedStore(Store):
        def _insert_effect(self, db, job, outcome, now):
            super()._insert_effect(db, job, outcome, now)
            if not after_commit:
                ready.set()
                time.sleep(120)

    store = PausedStore(path)
    job = store.claim('crash-worker', 500)
    store.finish(job)
    ready.set()
    time.sleep(120)


@pytest.mark.parametrize('after_commit', [False, True])
def test_hard_kill_at_effect_transaction_boundary(store, after_commit):
    job_id = store.create('crash', PAYLOAD)[0]['id']
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=crash_at_commit_boundary, args=(store.path, ready, after_commit))
    process.start()
    try:
        assert ready.wait(15), 'child did not reach commit boundary'
        process.kill()
        process.join(10)
        assert not process.is_alive()
        if not after_commit:
            assert store.get(job_id)['state'] == 'running'
            with store.read() as db:
                assert db.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
            time.sleep(0.55)
            recovered = store.claim('recovery', 5000)
            assert recovered['attempts'] == 2 and store.finish(recovered)
        else:
            assert store.claim('recovery', 5000) is None
        assert store.get(job_id)['state'] == 'succeeded'
        with store.read() as db:
            assert db.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 1
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)


def test_heartbeats_keep_a_long_job_owned(store):
    job_id = store.create('long', {**PAYLOAD, 'duration_ms': 1400})[0]['id']
    process = subprocess.Popen([sys.executable, '-m', 'queue_service.worker', '--db', store.path,
                                '--lease-ms', '300'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = store.get(job_id)
            if job['state'] == 'succeeded':
                break
            if job['state'] == 'running':
                assert store.claim('competitor', 300) is None
            time.sleep(0.04)
        else:
            pytest.fail('long job did not finish')
        assert job['attempts'] == 1
    finally:
        process.kill()
        process.communicate(timeout=10)


def test_api_retry_defaults_errors_and_stable_pagination(tmp_path):
    with TestClient(create_app(str(tmp_path / 'api.db'))) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/jobs', json={}).status_code == 422
        assert client.post('/jobs', headers={'Idempotency-Key': 'x'}, json={'duration_ms': -1}).status_code == 422
        assert client.post('/jobs', headers={'Idempotency-Key': 'x'}, json={'fail': 'false'}).status_code == 422
        first = client.post('/jobs', headers={'Idempotency-Key': 'one'}, json={})
        assert first.status_code == 201
        duplicate = client.post('/jobs', headers={'Idempotency-Key': 'one'},
                                json={'name': 'training', 'duration_ms': 100, 'fail': False})
        assert duplicate.status_code == 200 and duplicate.json()['id'] == first.json()['id']
        assert client.post('/jobs', headers={'Idempotency-Key': 'one'}, json={'fail': True}).status_code == 409
        for key in ['two', 'three']:
            assert client.post('/jobs', headers={'Idempotency-Key': key}, json={}).status_code == 201
        page = client.get('/jobs?limit=2').json()
        assert len(page['items']) == 2 and page['next_after'] == 2
        client.post('/jobs', headers={'Idempotency-Key': 'later'}, json={})
        next_page = client.get(f"/jobs?limit=2&after={page['next_after']}&snapshot={page['snapshot']}").json()
        assert [j['idempotency_key'] for j in next_page['items']] == ['three']
        assert next_page['next_after'] is None
        job_id = first.json()['id']
        assert client.post(f'/jobs/{job_id}/cancel').json()['state'] == 'cancelled'
        assert client.post(f'/jobs/{job_id}/cancel').status_code == 200
        assert client.get(f'/jobs/{job_id}').json()['state'] == 'cancelled'
        assert client.get('/jobs/999999').status_code == 404
        assert client.post('/jobs/999999/cancel').status_code == 404
        assert client.get('/jobs?limit=1001').status_code == 422


def test_failed_job_has_one_failure_effect(store):
    store.create('failed', {**PAYLOAD, 'fail': True})
    job = store.claim('worker', 5000)
    assert store.finish(job)
    assert not store.finish(job)
    assert store.get(job['id'])['state'] == 'failed'
    assert store.get(job['id'])['result']['outcome'] == 'failed'


def test_wrong_worker_and_token_cannot_publish(store):
    store.create('fencing', PAYLOAD)
    job = store.claim('correct', 5000)
    assert not store.finish({**job, 'owner': 'wrong'})
    assert not store.finish({**job, 'token': job['token'] + 1})
    assert not store.renew({**job, 'token': job['token'] + 1}, 5000)
    assert store.finish(job)
