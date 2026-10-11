#!/bin/bash
# Lightweight cron entry point: the Python scheduler caches plans and locks ticks.
# Override PYTHON_BIN if your virtual environment is somewhere else.
set -eu
cd "$(dirname "$0")"
python_bin="${PYTHON_BIN:-$PWD/.venv/bin/python}"
"$python_bin" -m animelists tick "$@" >> run.log 2>&1
