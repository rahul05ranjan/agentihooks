#!/usr/bin/env bash
set -euo pipefail

image="$1"
shift
docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
digest="$(docker manifest inspect "$@" "$image" | python3 -c '
import json
import sys

index = json.load(sys.stdin)
print(next(manifest["digest"] for manifest in index["manifests"]
           if manifest.get("platform", {}).get("os") == "linux"
           and manifest["platform"].get("architecture") == "amd64"))
')"
for attempt in 1 2 3; do
  if docker --host "$docker_host" pull "$image@$digest"; then
    break
  else
    status=$?
  fi
  if (( attempt == 3 )); then
    exit "$status"
  fi
  printf 'Pull of %s failed on attempt %s of 3; retrying in 10 seconds\n' "$image" "$attempt" >&2
  sleep 10
done
docker --host "$docker_host" tag "$image@$digest" "$image"
