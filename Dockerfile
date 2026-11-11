FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QUEUE_DB=/data/queue.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 queue \
    && useradd --uid 10001 --gid queue --create-home queue \
    && mkdir /data /evidence && chown queue:queue /data /evidence
COPY --chown=queue:queue queue_service ./queue_service
COPY --chown=queue:queue harness ./harness
COPY --chown=queue:queue tests ./tests
COPY --chown=queue:queue pyproject.toml .
USER queue
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "queue_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
