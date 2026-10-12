#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${ARC_PLAYWRIGHT_BROWSERS_PATH:-}" && -d "$ARC_PLAYWRIGHT_BROWSERS_PATH" ]] \
    && [[ ! -e "$PLAYWRIGHT_BROWSERS_PATH" && ! -L "$PLAYWRIGHT_BROWSERS_PATH" ]] \
    && PLAYWRIGHT_BROWSERS_PATH="$ARC_PLAYWRIGHT_BROWSERS_PATH" timeout 10s bash "$GITHUB_ACTION_PATH/verify.sh"; then
  ln -s "$ARC_PLAYWRIGHT_BROWSERS_PATH" "$PLAYWRIGHT_BROWSERS_PATH"
  echo "Using baked Chromium and OS dependencies without a download"
  echo "ready=true" >> "$GITHUB_OUTPUT"
else
  echo "ready=false" >> "$GITHUB_OUTPUT"
fi
