#!/usr/bin/env bash
set -u

# Simple interactive entry point for Vertex AI / Cloud Shell.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -z "${INPUT_XLSX:-}" ]]; then
  shopt -s nullglob
  files=("$ROOT_DIR"/*.xlsx "$HOME"/*.xlsx)
  shopt -u nullglob
  if [[ ${#files[@]} -eq 1 ]]; then
    INPUT_XLSX="${files[0]}"
  elif [[ ${#files[@]} -eq 0 ]]; then
    echo "Upload one .xlsx workbook to Cloud Shell, then run this command again."
    exit 1
  else
    echo "More than one Excel file was found. Set INPUT_XLSX to the file you want."
    printf '  %s\n' "${files[@]}"
    exit 1
  fi
fi

if [[ -z "${BIGQUERY_SOURCE_DATASET:-}" ]]; then
  read -r -p "Source BigQuery dataset: " BIGQUERY_SOURCE_DATASET
  export BIGQUERY_SOURCE_DATASET
fi
if [[ -z "${BIGQUERY_TARGET_DATASET:-}" ]]; then
  read -r -p "Target BigQuery dataset: " BIGQUERY_TARGET_DATASET
  export BIGQUERY_TARGET_DATASET
fi

export INPUT_XLSX
exec bash "$ROOT_DIR/vertex_run.sh"
