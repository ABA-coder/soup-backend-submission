import argparse
import json
import os
import random
import signal
import sqlite3
import time
import uuid

from queue_service.storage import Store, is_busy


def emit(event: str, **data):
    print(json.dumps({'event': event, 'pid': os.getpid(), **data}), flush=True)


def run(path: str, lease_ms: int = 3000):
    if lease_ms < 300:
        raise ValueError('lease_ms must be at least 300')
    store = Store(path)
    store.initialize()
    worker = f'{os.getpid()}-{uuid.uuid4().hex}'
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    emit('worker_started', worker=worker, lease_ms=lease_ms)
    while not stopping:
        try:
            job = store.claim(worker, lease_ms)
            if job is None:
                time.sleep(random.uniform(0.03, 0.07))
                continue
            emit('claimed', job_id=job['id'], token=job['token'])
            end = time.monotonic() + job['payload']['duration_ms'] / 1000
            heartbeat = lease_ms / 3000
            lost = False
            while not stopping:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, heartbeat))
                if time.monotonic() < end and not store.renew(job, lease_ms):
                    lost = True
                    break
            if stopping:
                break  # Let the lease expire; no unguarded cleanup write.
            committed = not lost and store.finish(job)
            emit('finished' if committed else 'fenced', job_id=job['id'], token=job['token'])
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            emit('database_busy')
            time.sleep(random.uniform(0.03, 0.10))
    emit('worker_stopped')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.environ.get('QUEUE_DB', 'data/queue.db'))
    parser.add_argument('--lease-ms', type=int, default=3000)
    args = parser.parse_args()
    run(args.db, args.lease_ms)
