FROM python:3.12-slim

# HOME is /data on purpose: the SQLite database, the API-key store, the generated
# session secret and the downloaded datasets all resolve under ~, so pointing HOME at
# the volume keeps every piece of state in one place that survives a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data \
    TRIVET_DB=/data/cache.db

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --uid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin trivet \
 && mkdir -p /data/.cache /data/.config \
 && chown -R 10001:10001 /data /app
USER 10001

VOLUME ["/data"]
EXPOSE 8952

# /healthz is unauthenticated by design, which is what makes it usable as a probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8952/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python", "app.py", "--host", "0.0.0.0", "--port", "8952"]
