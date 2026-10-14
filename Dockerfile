FROM python:3.12-slim-bookworm
ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/London \
    APP_VERSION=$VERSION \
    HOME=/tmp \
    AGE_RATER_DATA_DIR=/data \
    WEB_PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY age_rater.py db.py app.py ./
COPY templates ./templates
COPY static ./static

# Runs as PUID 1003 / PGID 1006 on my server (my app standard). Compose sets it via user:;
# this default keeps the image non-root when run without compose.
RUN mkdir -p /data && chown 1003:1006 /data
USER 1003:1006
VOLUME ["/data"]
EXPOSE 8000
LABEL org.opencontainers.image.title="Kavita Age-Rater" org.opencontainers.image.version="$VERSION"
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "app.py"]
