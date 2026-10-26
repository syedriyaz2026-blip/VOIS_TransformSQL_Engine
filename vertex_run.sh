#!/usr/bin/env bash
set -uo pipefail

# Vertex/Cloud Shell one-command runner for VOIS TransformSQL.
# It pulls fixes, validates the application, starts the services, records a
# non-secret report, and pushes the report so failures are reproducible here.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
REPORT_DIR="$ROOT_DIR/run-reports"
REPORT_FILE="$REPORT_DIR/$(date -u +%Y%m%dT%H%M%SZ).md"
LATEST_FILE="$REPORT_DIR/latest.md"
BACKEND_LOG="$ROOT_DIR/backend.log"
FRONTEND_LOG="$ROOT_DIR/frontend.log"
INPUT_XLSX="${INPUT_XLSX:-}"
START_APP="${START_APP:-1}"
TEST_BIGQUERY="${TEST_BIGQUERY:-1}"
FAILURES=0
WARNINGS=0
CHECKS=()

record() { CHECKS+=("$1|$2|$3"); }
pass() { record "PASS" "$1" "$2"; }
warn() { WARNINGS=$((WARNINGS + 1)); record "WARN" "$1" "$2"; }
fail() { FAILURES=$((FAILURES + 1)); record "FAIL" "$1" "$2"; }

mkdir -p "$REPORT_DIR"

if git diff --quiet && git diff --cached --quiet; then
  if git pull --rebase origin main >/tmp/vois_git_pull.log 2>&1; then
    pass "Git pull" "Latest origin/main fetched before validation."
  else
    fail "Git pull" "Could not pull origin/main. See /tmp/vois_git_pull.log."
  fi
else
  fail "Git pull" "Local uncommitted changes detected; runner will not overwrite them."
fi

if python3 -m pip install -r backend/requirements.txt >/tmp/vois_pip.log 2>&1; then
  pass "Dependencies" "Backend requirements installed."
else
  fail "Dependencies" "Dependency installation failed. See /tmp/vois_pip.log."
fi

if python3 -m py_compile backend/transformsql_engine.py backend/config.py backend/sql_safety.py; then
  pass "Backend syntax" "Python compilation passed."
else
  fail "Backend syntax" "Python compilation failed."
fi

if command -v node >/dev/null 2>&1; then
  if node --check frontend/app.js; then
    pass "Frontend syntax" "JavaScript syntax check passed."
  else
    fail "Frontend syntax" "JavaScript syntax check failed."
  fi
else
  warn "Frontend syntax" "Node.js is not installed; JavaScript syntax check skipped."
fi

if [[ -z "$INPUT_XLSX" ]]; then
  shopt -s nullglob
  candidates=("$ROOT_DIR"/*.xlsx)
  shopt -u nullglob
  [[ ${#candidates[@]} -eq 1 ]] && INPUT_XLSX="${candidates[0]}"
fi

if [[ -n "$INPUT_XLSX" && -f "$INPUT_XLSX" ]]; then
  if INPUT_XLSX="$INPUT_XLSX" python3 - <<'PY'
import os
import sys
from pathlib import Path
sys.path.insert(0, "backend")
import transformsql_engine as engine
path = Path(os.environ["INPUT_XLSX"])
result = engine.build_result(path)
summary = result["summary"]
assert summary["row_count"] > 0, "No mapping rows detected"
print(summary)
PY
  then
    pass "Workbook regression" "Parsed $INPUT_XLSX and generated mapping SQL."
  else
    fail "Workbook regression" "Workbook parsing or SQL generation failed for $INPUT_XLSX."
  fi
else
  warn "Workbook regression" "No .xlsx supplied. Set INPUT_XLSX=/path/to/workbook.xlsx for a workbook test."
fi

if [[ "$START_APP" == "1" ]]; then
  pkill -f "transformsql_engine.py --serve --port 8081" >/dev/null 2>&1 || true
  pkill -f "http.server 8080 --directory frontend" >/dev/null 2>&1 || true
  nohup python3 backend/transformsql_engine.py --serve --port 8081 >"$BACKEND_LOG" 2>&1 &
  nohup python3 -m http.server 8080 --directory frontend >"$FRONTEND_LOG" 2>&1 &
  sleep 3
  if curl -fsS http://localhost:8081/healthz >/tmp/vois_health.json; then
    pass "Backend health" "Backend is listening on port 8081."
  else
    fail "Backend health" "Backend health check failed. See backend.log."
  fi
  if curl -fsS http://localhost:8080 >/dev/null; then
    pass "Frontend health" "Frontend is listening on port 8080."
  else
    fail "Frontend health" "Frontend health check failed. See frontend.log."
  fi
fi

if [[ "$TEST_BIGQUERY" == "1" ]]; then
  if curl -fsS http://localhost:8081/readyz >/tmp/vois_ready.json; then
    pass "BigQuery runtime" "Vertex/Google ADC readiness check passed."
  else
    fail "BigQuery runtime" "BigQuery runtime is not ready. Run gcloud auth application-default login and verify IAM/table access."
  fi
fi

{
  echo "# VOIS TransformSQL Vertex run"
  echo
  echo "- Timestamp (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- Git commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "- Workbook: ${INPUT_XLSX:-not supplied}"
  echo "- Failures: $FAILURES"
  echo "- Warnings: $WARNINGS"
  echo
  echo "| Status | Check | Details |"
  echo "|---|---|---|"
  for item in "${CHECKS[@]}"; do
    IFS='|' read -r status name details <<<"$item"
    details=${details//|/\\|}
    echo "| $status | $name | $details |"
  done
  echo
  echo "## Runtime evidence"
  [[ -f /tmp/vois_health.json ]] && { echo '```json'; cat /tmp/vois_health.json; echo '```'; }
  [[ -f /tmp/vois_ready.json ]] && { echo '```json'; cat /tmp/vois_ready.json; echo '```'; }
  if [[ -f "$BACKEND_LOG" ]]; then
    echo "## Recent backend log"
    echo '```text'
    tail -n 80 "$BACKEND_LOG"
    echo '```'
  fi
  if [[ -f "$FRONTEND_LOG" ]]; then
    echo "## Recent frontend log"
    echo '```text'
    tail -n 80 "$FRONTEND_LOG"
    echo '```'
  fi
  echo
  echo "Logs remain local in backend.log and frontend.log; secrets are not written to this report."
} >"$REPORT_FILE"
cp "$REPORT_FILE" "$LATEST_FILE"

git add run-reports
git commit -m "Record Vertex run report" >/tmp/vois_git_commit.log 2>&1 || true
if git push origin main >/tmp/vois_git_push.log 2>&1; then
  echo "Git report pushed to origin/main."
else
  echo "Git report push failed. See /tmp/vois_git_push.log."
  FAILURES=$((FAILURES + 1))
fi

echo "Report: $REPORT_FILE"
echo "Failures: $FAILURES | Warnings: $WARNINGS"
echo "---------------- VOIS RUN REPORT ----------------"
cat "$REPORT_FILE"
echo "-------------- END VOIS RUN REPORT --------------"
[[ "$FAILURES" -eq 0 ]] && exit 0 || exit 1
