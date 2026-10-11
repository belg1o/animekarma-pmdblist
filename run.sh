#!/bin/bash
# Cron wrapper: loads .env, runs the given jobs, appends output to run.log.
#   ./run.sh karma
#   ./run.sh recent popular
#   ./run.sh trending
# Example crontab (times are the server's timezone; the trend feed has a full 24h at 18:00 UTC):
#   30 6  * * 1  /path/to/animekarma-pmdblist/run.sh karma
#   30 6  * * *  /path/to/animekarma-pmdblist/run.sh recent popular
#   15 18 * * *  /path/to/animekarma-pmdblist/run.sh trending
set -a; source "$(dirname "$0")/.env"; set +a
cd "$(dirname "$0")"
python3 -m animelists run "$@" >> run.log 2>&1
