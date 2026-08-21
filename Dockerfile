FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

RUN useradd -u 1000 -m appuser \
    && mkdir -p /data /app/static/uploads \
    && chown -R appuser:appuser /app /data

USER appuser

ENV DB_PATH=/data/flohmarkt.db
# No bytecode cache to write under a read-only root filesystem; stdout/stderr
# logging stays unbuffered so `kubectl logs` shows output immediately.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# --worker-tmp-dir /dev/shm: gunicorn's worker heartbeat file normally lives
# under /tmp, which is part of the container's root filesystem. Pointing it
# at /dev/shm (a tmpfs Kubernetes mounts separately from the root fs, present
# on every pod by default) lets this run with readOnlyRootFilesystem: true
# without needing an extra emptyDir volume just for that one file. The only
# other paths this app writes to are DB_PATH and static/uploads, both of
# which are already expected to be mounted volumes in the Deployment.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "app:app"]
