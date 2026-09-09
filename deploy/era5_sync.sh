#!/usr/bin/env bash
# Host cron -> zhangmy-dev -> durable one-shot sync. No changes to server cron here.
set -euo pipefail
: "${ERA5_START:?Set ERA5_START=YYYYMM explicitly}"
CONTAINER="${CONTAINER:-zhangmy-dev}"
PROJECT_DIR="${ERA5_PROJECT_DIR:-/workspace/climate_reanalysis}"
CONFIG="${ERA5_CONFIG:-config/settings.yaml}"
# All invocations of this config use sync's in-container producer lock.
# Do not use a short timeout: it can kill a completed submit before its receipt is saved.
if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER")" != true ]]; then
    echo "ERA5 container not running: $CONTAINER" >&2
    exit 1
fi
args=(--config "$CONFIG" sync --start "$ERA5_START")
if [[ "${ERA5_REFRESH:-0}" == 1 ]]; then args+=(--refresh); fi
docker exec -w "$PROJECT_DIR" "$CONTAINER" uv run --no-sync climate_reanalysis "${args[@]}"
