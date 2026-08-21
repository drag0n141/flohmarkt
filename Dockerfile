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
# without needing an extra emptyDir volume just for that one file.
#
# --no-control-socket: gunicorn 25.1+ creates a control socket for the
# `gunicornc` CLI tool by default, under $XDG_RUNTIME_DIR or
# $HOME/.gunicorn (e.g. /home/appuser/.gunicorn) — another write outside any
# mounted volume, and one we don't use anyway. Disabling it avoids
# "Control server error: [Errno 30] Read-only file system" under a
# read-only root.
#
# The only paths this app itself writes to are DB_PATH and static/uploads,
# both of which are already expected to be mounted volumes in the
# Deployment.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--worker-tmp-dir", "/dev/shm", "--no-control-socket", "--access-logfile", "-", "app:app"]
