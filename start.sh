#!/usr/bin/env bash
set -u

# Single Cloud Shell command: fetch the newest code, then run the full VOIS check.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "[VOIS] Fetching latest code from origin/main..."
if ! git pull --rebase origin main; then
  echo "[VOIS] ERROR: Git pull failed. Fix Git authentication/conflicts, then retry."
  exit 1
fi

echo "[VOIS] Starting Vertex validation and application..."
exec bash run_vertex.sh
