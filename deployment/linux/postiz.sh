#!/bin/bash
set -euo pipefail
cd /opt/max-postiz
exec docker compose --project-name max-postiz --env-file deployment/postiz/.env \
  -f deployment/postiz/compose.yaml -f deployment/postiz/images.lock.yaml \
  -f deployment/postiz-max/compose.bridge.yaml -f deployment/linux/compose.yaml "$@"
