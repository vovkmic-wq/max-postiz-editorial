#!/bin/bash
# Explicitly refuses a nonempty destination; never overwrites a live database.
set -euo pipefail
cd /opt/max-postiz
restore() {
  local container=$1 user=$2 database=$3 dump=$4
  local count
  count=$(docker exec "$container" psql -U "$user" -d "$database" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
  test "$count" -eq 0 || { echo "Refusing nonempty $database" >&2; exit 1; }
  docker exec -i "$container" pg_restore --exit-on-error --no-owner --no-privileges \
    -U "$user" -d "$database" < "$dump"
}
restore max-postiz-hub-postgres-1 hub_admin editorial_hub /home/editorial/hub.dump
restore max-postiz-postiz-postgres-1 postiz-user postiz-db-local /home/editorial/postiz.dump
echo 'Both private databases restored into empty destinations'
