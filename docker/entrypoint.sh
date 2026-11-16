#!/bin/bash
# PASJONSFRUKT_MODE selects what this container runs:
#   web     (default) only the HTTP server: no crontab, no cron daemon
#   harvest only cron with the mounted crontab (/etc/cron.d/pasjonsfrukt-crontab), no server
# Running the two as separate containers keeps the bulk writes of a harvest to the
# share out of the server's process tree, cgroup and I/O queue.
set -e

MODE="${PASJONSFRUKT_MODE:-web}"

case "$MODE" in
  web)
    echo "Mode: web. Starting server..."
    exec pasjonsfrukt serve "$@"
    ;;
  harvest)
    echo "Mode: harvest. Installing crontab..."
    crontab -r 2>/dev/null || true
    # cron starts jobs with an empty environment, so the staging directory is
    # written into the crontab itself (the crontab line sets cd /app and PATH).
    {
      echo "STAGING_DIR=${STAGING_DIR:-/app/staging}"
      cat /etc/cron.d/pasjonsfrukt-crontab
    } | crontab -
    crontab -l
    mkdir -p "${STAGING_DIR:-/app/staging}"
    # show the harvest log in `docker logs`
    tail -F /var/log/pasjonsfrukt.log &
    echo "Starting cron in the foreground..."
    exec cron -f
    ;;
  *)
    echo "Unknown PASJONSFRUKT_MODE '$MODE' (use web or harvest)" >&2
    exit 64
    ;;
esac
