#!/usr/bin/env bash
set -u

# Simple interactive entry point for Vertex AI / Cloud Shell.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -z "${INPUT_XLSX:-}" ]]; then
  shopt -s nullglob
  files=("$ROOT_DIR"/*.xlsx "$HOME"/*.xlsx)
  shopt -u nullglob
  if [[ ${#files[@]} -eq 0 ]]; then
    while IFS= read -r file; do files+=("$file"); done < <(find "$HOME" -maxdepth 4 -type f -iname '*.xlsx' 2>/dev/null)
  fi
  if [[ ${#files[@]} -eq 1 ]]; then
    INPUT_XLSX="${files[0]}"
  elif [[ ${#files[@]} -eq 0 ]]; then
    echo "No Excel workbook was found in Cloud Shell."
    echo "Use Cloud Shell menu: three dots -> Upload file, then upload your .xlsx file."
    echo "After upload, run: bash run_vertex.sh"
    echo "A Windows path such as C:\\Users\\... is not available inside Cloud Shell."
    exit 1
  else
    echo "More than one Excel file was found. Set INPUT_XLSX to the file you want."
    printf '  %s\n' "${files[@]}"
    exit 1
  fi
fi

export INPUT_XLSX
exec bash "$ROOT_DIR/vertex_run.sh"
