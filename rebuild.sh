#!/bin/bash
# Rebuild and redeploy the Plexbie Discord bot.
#
# This delegates to docker compose on purpose. The previous version of this
# script ran `docker run --name plexbie` by hand, which had drifted badly from
# the compose file and would have silently broken the deployment:
#
#   * it mounted 3 volumes where compose mounts 10, dropping every /watch and
#     /library path - bookshelf processing would stop working entirely, and the
#     four bind-mounted plugin paths would revert to whatever is baked into the
#     image
#   * it published -p 8081:8081 instead of using network_mode: host, which
#     re-exposes every /webhook/* route to the whole LAN. Those routes pass
#     unauthenticated requests through whenever the matching secret is unset.
#   * it wrote logs to plexbie/logs instead of the appdata logs directory
#
# Keeping one source of truth (docker-compose.yml) removes that whole class of
# drift. Never reintroduce `docker run` here.

set -euo pipefail

PROJECT_DIR="/mnt/user/appdata/plexbie"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
SOURCE_DIR="$PROJECT_DIR/plexbie"
SERVICE="plexbie"

compose() {
    docker compose -f "$COMPOSE_FILE" --project-directory "$PROJECT_DIR" "$@"
}

echo "=============================================="
echo "Plexbie rebuild"
echo "=============================================="

# Tag the current image so there is always a way back.
STAMP="$(date +%Y%m%d-%H%M%S)"
if docker image inspect plexbie:latest >/dev/null 2>&1; then
    docker tag plexbie:latest "plexbie:pre-rebuild-$STAMP"
    echo "Rollback tag: plexbie:pre-rebuild-$STAMP"
fi

echo
echo "Building image from $SOURCE_DIR ..."
docker build -t plexbie:latest "$SOURCE_DIR"

echo
echo "Recreating the $SERVICE service ..."
# --no-deps so nothing else in the project is touched.
compose up -d --no-deps --force-recreate "$SERVICE"

echo
echo "Waiting for the bot to report ready ..."
for _ in $(seq 1 60); do
    if docker logs "$SERVICE" --since 2m 2>&1 | grep -q "PLEXBIE DISCORD BOT - READY"; then
        echo "Ready."
        break
    fi
    sleep 2
done

echo
compose ps

echo
echo "Recent errors (none expected):"
docker logs "$SERVICE" --since 2m 2>&1 | grep -E '"level": "(ERROR|CRITICAL)"' | tail -5 || true

echo
echo "Startup summary:"
docker logs "$SERVICE" --since 2m 2>&1 \
    | grep -oE "Plex: Connected to [^\"]*|Redis: (Connected|Not connected)[^\"]*|Loaded [0-9]+/[0-9]+ available plugins|Webhook server listening on [^\"]*|Unauthenticated webhook routes[^\"]*|PLEXBIE DISCORD BOT - READY" \
    || true

echo
echo "If this rebuild is bad:  docker tag plexbie:pre-rebuild-$STAMP plexbie:latest && $0"
