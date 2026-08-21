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
EXPOSE 8000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--access-logfile", "-", "app:app"]
