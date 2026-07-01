# Aegis CFO — production image.
# One gunicorn worker: the Hermes model serializes requests, so extra workers
# only queue. The audit council runs in a background thread within the worker.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Create the instance dir + seed a demo DB at build time (idempotent at runtime).
RUN mkdir -p instance

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# debug stays OFF (FLASK_DEBUG unset); gunicorn is the WSGI server.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "120", "run:app"]
