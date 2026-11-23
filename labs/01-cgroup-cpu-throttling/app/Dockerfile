FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc

WORKDIR /app

RUN pip install --no-cache-dir \
      gunicorn==23.0.0 \
      prometheus-client==0.21.1 \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser app.py gunicorn.conf.py entrypoint.sh /app/
RUN chmod 0555 /app/entrypoint.sh

USER appuser

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
