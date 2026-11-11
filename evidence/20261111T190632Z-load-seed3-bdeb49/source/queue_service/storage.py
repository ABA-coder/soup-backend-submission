from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterator


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    token INTEGER NOT NULL DEFAULT 0 CHECK (token >= 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    owner TEXT,
    lease_until_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    CHECK ((state = 'running' AND owner IS NOT NULL AND lease_until_ms IS NOT NULL)
        OR (state <> 'running' AND owner IS NULL AND lease_until_ms IS NULL)),
    CHECK ((state IN ('queued','running') AND finished_at_ms IS NULL)
        OR (state IN ('succeeded','failed','cancelled') AND finished_at_ms IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS queued_jobs ON jobs(id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS expired_jobs ON jobs(lease_until_ms,id) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS attempts (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    token INTEGER NOT NULL,
    worker TEXT NOT NULL,
    claimed_at_ms INTEGER NOT NULL,
    PRIMARY KEY(job_id,token)
);
CREATE TABLE IF NOT EXISTS effects (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    token INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed')),
    result TEXT NOT NULL,
    committed_at_ms INTEGER NOT NULL,
    FOREIGN KEY(job_id,token) REFERENCES attempts(job_id,token)
);
"""


class Store:
    def __init__(self, path: str | Path, busy_ms: int = 5000):
        self.path = str(path)
        self.busy_ms = busy_ms

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.busy_ms / 1000, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA read_uncommitted=OFF')
        return db

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute('BEGIN')
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            # Acquire the writer lock BEFORE reading state or the clock.
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.executescript(SCHEMA)
        finally:
            db.close()

    def _now(self, db: sqlite3.Connection) -> int:
        return db.execute(
            "SELECT CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
        ).fetchone()[0]

    @staticmethod
    def _job(db: sqlite3.Connection, job_id: int) -> dict:
        row = db.execute(
            'SELECT j.*, e.result AS result_json FROM jobs j '
            'LEFT JOIN effects e ON e.job_id=j.id WHERE j.id=?', (job_id,)
        ).fetchone()
        if row is None:
            raise Missing(job_id)
        job = dict(row)
        job['payload'] = json.loads(job['payload'])
        job['result'] = json.loads(job.pop('result_json')) if job['result_json'] else None
        job.pop('result_json', None)
        return job

    def create(self, key: str, payload: dict) -> tuple[dict, bool]:
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with self.write() as db:
            existing = db.execute(
                'SELECT id,payload FROM jobs WHERE idempotency_key=?', (key,)
            ).fetchone()
            if existing is not None:
                if existing['payload'] != canonical:
                    raise Conflict('Idempotency key already belongs to a different payload')
                return self._job(db, existing['id']), False
            now = self._now(db)
            job_id = db.execute(
                'INSERT INTO jobs(idempotency_key,payload,created_at_ms,updated_at_ms) VALUES(?,?,?,?)',
                (key, canonical, now, now),
            ).lastrowid
            return self._job(db, job_id), True

    def get(self, job_id: int) -> dict:
        with self.read() as db:
            return self._job(db, job_id)

    def list(self, after: int, limit: int, snapshot: int | None) -> dict:
        with self.read() as db:
            if snapshot is None:
                snapshot = db.execute('SELECT COALESCE(MAX(id),0) FROM jobs').fetchone()[0]
            ids = [r[0] for r in db.execute(
                'SELECT id FROM jobs WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                (after, snapshot, limit + 1),
            )]
            return {
                'items': [self._job(db, i) for i in ids[:limit]],
                'snapshot': snapshot,
                'next_after': ids[limit - 1] if len(ids) > limit else None,
            }

    def cancel(self, job_id: int) -> dict:
        with self.write() as db:
            job = self._job(db, job_id)
            if job['state'] in ('succeeded', 'failed'):
                raise Conflict(f"Job is already {job['state']}")
            if job['state'] != 'cancelled':
                now = self._now(db)
                db.execute(
                    "UPDATE jobs SET state='cancelled',token=token+1,owner=NULL,lease_until_ms=NULL,"
                    'updated_at_ms=?,finished_at_ms=? WHERE id=?', (now, now, job_id)
                )
            return self._job(db, job_id)

    def claim(self, worker: str, lease_ms: int) -> dict | None:
        with self.write() as db:
            now = self._now(db)
            candidate = db.execute(
                "SELECT id FROM jobs WHERE state='running' AND lease_until_ms<=? "
                'ORDER BY lease_until_ms,id LIMIT 1', (now,)
            ).fetchone()
            if candidate is None:
                candidate = db.execute(
                    "SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
                ).fetchone()
            if candidate is None:
                return None
            job_id = candidate[0]
            db.execute(
                "UPDATE jobs SET state='running',owner=?,token=token+1,attempts=attempts+1,"
                'lease_until_ms=?,updated_at_ms=? WHERE id=?',
                (worker, now + lease_ms, now, job_id),
            )
            job = self._job(db, job_id)
            db.execute('INSERT INTO attempts VALUES(?,?,?,?)', (job_id, job['token'], worker, now))
            return job

    def renew(self, job: dict, lease_ms: int) -> bool:
        with self.write() as db:
            now = self._now(db)
            changed = db.execute(
                'UPDATE jobs SET lease_until_ms=?,updated_at_ms=? '
                "WHERE id=? AND state='running' AND owner=? AND token=? AND lease_until_ms>?",
                (now + lease_ms, now, job['id'], job['owner'], job['token'], now),
            ).rowcount
            return changed == 1

    def _insert_effect(self, db: sqlite3.Connection, job: dict, outcome: str, now: int) -> None:
        payload = json.dumps(job['payload'], sort_keys=True, separators=(',', ':'))
        result = {'payload_sha256': hashlib.sha256(payload.encode()).hexdigest(), 'outcome': outcome}
        db.execute(
            'INSERT INTO effects(job_id,token,outcome,result,committed_at_ms) VALUES(?,?,?,?,?)',
            (job['id'], job['token'], outcome, json.dumps(result, sort_keys=True), now),
        )

    def finish(self, job: dict) -> bool:
        with self.write() as db:
            now = self._now(db)
            current = db.execute(
                "SELECT * FROM jobs WHERE id=? AND state='running' AND owner=? "
                'AND token=? AND lease_until_ms>?',
                (job['id'], job['owner'], job['token'], now),
            ).fetchone()
            if current is None:
                return False
            # Use persisted input, not any result supplied by a stale worker.
            durable_job = dict(current)
            durable_job['payload'] = json.loads(current['payload'])
            outcome = 'failed' if durable_job['payload']['fail'] else 'succeeded'
            self._insert_effect(db, durable_job, outcome, now)
            db.execute(
                'UPDATE jobs SET state=?,owner=NULL,lease_until_ms=NULL,updated_at_ms=?,finished_at_ms=? '
                'WHERE id=?', (outcome, now, now, job['id'])
            )
            return True

    def counts(self) -> dict:
        with self.read() as db:
            return dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state'))


def is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, 'sqlite_errorcode', 0)
    return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
