#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -n "${PORT:-}" ]; then
    exec .venv/bin/hub-agent serve --port "$PORT"
else
    exec .venv/bin/hub-agent serve --port 8090
fi
