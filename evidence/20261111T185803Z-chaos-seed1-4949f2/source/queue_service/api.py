from contextlib import asynccontextmanager
import os
import sqlite3
from typing import Annotated

from fastapi import FastAPI, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from queue_service.storage import Conflict, Missing, Store, is_busy


class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(default='training', min_length=1, max_length=100)
    duration_ms: int = Field(default=100, ge=0, le=60000)
    fail: bool = False


def create_app(path: str | None = None) -> FastAPI:
    store = Store(path or os.environ.get('QUEUE_DB', 'data/queue.db'))

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(title='Soup training queue', version='1.0.0', lifespan=lifespan)

    @app.exception_handler(Missing)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={'detail': 'Job not found'})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    async def operational(request, exc):
        if is_busy(exc):
            return JSONResponse(status_code=503, content={'detail': 'Database busy; retry with the same key'},
                                headers={'Retry-After': '1'})
        raise exc

    @app.get('/health')
    def health():
        with store.read() as db:
            db.execute('SELECT id FROM jobs LIMIT 1').fetchone()
        return {'status': 'ok'}

    @app.post('/jobs', status_code=201)
    def create_job(payload: JobInput, response: Response,
                   idempotency_key: Annotated[str, Header(min_length=1, max_length=200, pattern=r'^[\x21-\x7E]+$')]):
        job, created = store.create(idempotency_key, payload.model_dump())
        response.status_code = 201 if created else 200
        response.headers['Location'] = f"/jobs/{job['id']}"
        return job

    @app.get('/jobs')
    def list_jobs(after: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=1000)] = 100,
                  snapshot: Annotated[int | None, Query(ge=0)] = None):
        return store.list(after, limit, snapshot)

    @app.get('/jobs/{job_id}')
    def get_job(job_id: int):
        return store.get(job_id)

    @app.post('/jobs/{job_id}/cancel')
    def cancel_job(job_id: int):
        return store.cancel(job_id)

    return app


app = create_app()
