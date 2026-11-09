FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable state: SQLite cache, key store, datasets. Mount a volume here.
ENV TRIVET_DB=/data/cache.db
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001

EXPOSE 8952
HEALTHCHECK --interval=30s --timeout=3s CMD python -c \
  "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8952/healthz', timeout=2).status == 200 else 1)"

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8952"]
