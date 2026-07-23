FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY age_rater.py db.py app.py ./
COPY templates ./templates

# State (SQLite db, log) lives here — mount a volume at /data
ENV AGE_RATER_DATA_DIR=/data
ENV WEB_PORT=8080
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "app.py"]
