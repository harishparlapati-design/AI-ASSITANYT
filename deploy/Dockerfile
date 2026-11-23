# Hosted Harry: Flask + gunicorn on a small Linux image.
#
#   docker build -f deploy/Dockerfile -t harry .
#   docker run --rm -p 8000:8000 --env-file deploy/.env -v harry-data:/data harry
#
# Local models (Ollama, diffusers) are NOT in this image: hosted inference goes
# through the provider adapters configured in config.json / environment.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HARRY_HOSTED=1 \
    HARRY_HTTPS=1 \
    HARRY_DOCUMENTS_DIR=/data/documents \
    HARRY_MEDIA_DIR=/data/media \
    HARRY_ACCOUNTS_DB=/data/accounts.db

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY harry_core ./harry_core
COPY tools ./tools
COPY wsgi.py config.example.json ./

# Writable state lives on a mounted volume, never in the image.
RUN mkdir -p /data && useradd --create-home --uid 10001 harry \
 && chown -R harry:harry /app /data
USER harry
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# One worker process: the job queue is in-process. Parallelism comes from
# threads, which is right for an SSE dashboard and CPU-light request handlers.
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "8", \
     "--timeout", "300", "--graceful-timeout", "30", \
     "-b", "0.0.0.0:8000", "wsgi:application"]
