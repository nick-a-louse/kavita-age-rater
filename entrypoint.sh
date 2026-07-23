#!/bin/sh
# With arguments -> run age_rater.py once with those args and exit.
#   e.g. docker compose run --rm rater scan --limit 10
#        docker compose run --rm rater apply
# With no arguments -> run RUN_MODE on a loop every INTERVAL_HOURS (the default
# long-running service started by `docker compose up -d`).
set -eu

if [ "$#" -gt 0 ]; then
    exec python age_rater.py "$@"
fi

MODE="${RUN_MODE:-scan}"
INTERVAL_HOURS="${INTERVAL_HOURS:-24}"
echo "kavita-age-rater: looping mode=${MODE} every ${INTERVAL_HOURS}h"

while true; do
    echo "--- $(date -u +%FT%TZ) running: ${MODE} ---"
    python age_rater.py "${MODE}" || echo "run failed; will retry next interval"
    echo "--- sleeping ${INTERVAL_HOURS}h ---"
    sleep "$(( INTERVAL_HOURS * 3600 ))"
done
