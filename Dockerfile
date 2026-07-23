FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY age_rater.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# State (cache, proposals.csv, log) is written here — mount a volume at /data
ENV AGE_RATER_DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["./entrypoint.sh"]
