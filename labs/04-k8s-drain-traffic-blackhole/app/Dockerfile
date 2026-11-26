FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser api.py loadclient.py /app/

USER 10001:10001

EXPOSE 8080

CMD ["python3", "/app/api.py"]
