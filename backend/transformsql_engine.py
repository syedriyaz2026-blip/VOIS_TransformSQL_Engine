from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import openpyxl

try:
    from api_errors import TransformSQLError, correlation_id, error_payload
    from config import load_settings
    from sql_safety import assert_read_only
except ImportError:  # pragma: no cover - supports package-style imports
    from .api_errors import TransformSQLError, correlation_id, error_payload
    from .config import load_settings
    from .sql_safety import assert_read_only

ROOT = Path(__file__).resolve().parent
HEADERS = [
    "source table name", "source column name", "source column data type",
    "transformation type", "transformation rule", "target table name",
    "target column name", "target column data type",
]
ALIASES = {
    "source table name": ["source table name", "src table name", "source table", "input table", "input source table", "source entity"],
    "source column name": ["source column name", "src column name", "source attribute", "source column", "source field", "source field name", "input column", "input attribute", "input field"],
    "source column data type": ["source column data type", "src column data type", "source data type", "source datatype", "input data type", "input datatype"],
    "transformation type": ["transformation type", "transform type", "transformation category", "mapping type", "rule type", "nature", "nature groupby aggregate over dim"],
    "transformation rule": ["transformation rule", "transform rule", "transformation logic", "business rule", "mapping rule", "derivation logic", "expression", "transformation expression"],
    "target table name": ["target table name", "target_table_name", "target table", "output table", "destination table", "target entity", "output entity"],
    "target column name": ["target column name", "target attribute", "target column", "output column", "destination column", "target field", "output field", "output attribute"],
    "target column data type": ["target column data type", "target attribute data type", "target data type", "target datatype", "output data type", "destination data type"],
}

OPTIONAL_ALIASES = {
    "source directory path": ["source directory path", "source directory", "source path", "source file path", "source folder"],
    "source datatype changed": ["source datatype changed", "source data type changed", "datatype changed", "data type changed"],
    "target dataset": ["target dataset", "destination dataset", "output dataset"],
    "comment": ["comment", "comments", "note", "notes", "additional comment"],
}

CONTRACT_FIELD_ALIASES = {
    "source table name": ["source table", "source table name", "input table", "input source table", "source entity"],
    "source column name": ["source column", "source column name", "source field", "source attribute", "input column", "input field"],
    "source column data type": ["source data type", "source datatype", "input data type", "input datatype"],
    "target table name": ["target table", "target table name", "output table", "destination table", "target entity", "output entity"],
    "target column name": ["target column", "target column name", "output column", "destination column", "target field", "output field", "target attribute"],
    "target column data type": ["target data type", "target datatype", "output data type", "destination data type"],
    "transformation rule": ["transformation logic", "transformation rule", "business rule", "mapping rule", "derivation logic", "expression"],
    "transformation type": ["nature groupby aggregate over dim", "transformation type", "transform type", "transformation category", "mapping type", "nature"],
}


def load_env_file():
    """Load simple KEY=VALUE settings without requiring python-dotenv."""
    for path in (ROOT / ".env", ROOT.parent / ".env"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_bigquery_project():
    """Resolve project from explicit env first, then Vertex/Google ADC context."""
    explicit = os.getenv("BIGQUERY_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if explicit:
        return explicit.strip()
    try:
        import google.auth
        _, detected = google.auth.default()
        return (detected or "").strip() or None
    except Exception:
        return None


def bigquery_client():
    """Create a client using the runtime's ADC; no credential file is required."""
    from google.cloud import bigquery
    project = resolve_bigquery_project()
    return bigquery.Client(project=project) if project else bigquery.Client()


def bigquery_auth_source():
    return "service-account environment variable" if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else "Vertex/Google Application Default Credentials"


def public_settings():
    """Return editable BigQuery configuration without exposing secret contents."""
    load_env_file()
    settings = load_settings()
    credential_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    variable_keys = [
        "ENV", "DEMO_LOGIN_ENABLED", "READ_ONLY_SQL_ENABLED", "TRANSFORMSQL_DRY_RUN_ENABLED",
        "TRANSFORMSQL_COST_DISPLAY_ENABLED", "TRANSFORMSQL_PII_MASKING_ENABLED",
        "TRANSFORMSQL_VERTEX_SUGGESTIONS_ENABLED", "TRANSFORMSQL_AUDIT_LOG_ENABLED",
        "CORS_ALLOWED_ORIGINS", "BIGQUERY_LOCATION", "BIGQUERY_MAX_ROWS",
        "BIGQUERY_MAX_BYTES_BILLED", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
        "BIGQUERY_PROJECT", "BQ_RETRY_COUNT", "BQ_RETRY_BACKOFF_SECONDS", "LOG_LEVEL",
        "LOG_SQL", "LOG_PII", "RATE_LIMIT_ENABLED", "RATE_LIMIT_PER_MINUTE",
        "CSRF_ENABLED", "SESSION_SECRET", "CSRF_SECRET",
    ]
    secret_keys = {"SESSION_SECRET", "CSRF_SECRET"}
    variables = []
    for key in variable_keys:
        value = os.getenv(key, "")
        if key in secret_keys and value:
            value = "••••••••"
        variables.append({"key": key, "value": value, "secret": key in secret_keys, "editable": True})
    return {
        "project": resolve_bigquery_project() or "",
        "location": settings.location or "US",
        "auth_source": bigquery_auth_source(),
        "environment_managed": not bool(credential_path),
        "max_rows": settings.max_rows,
        "max_bytes_billed": settings.max_bytes_billed or "",
        "credentials_path": credential_path,
        "credentials_configured": bool(credential_path and Path(credential_path).exists()),
        "credentials_file_name": Path(credential_path).name if credential_path else "",
        "env": settings.env,
        "demo_login_enabled": settings.demo_login_enabled,
        "variables": variables,
    }


def update_settings(values):
    """Persist editable local settings; never accepts or writes credential JSON content."""
    load_env_file()
    allowed = {
        "ENV": str(values.get("env", os.getenv("ENV", "local"))).strip().lower(),
        "DEMO_LOGIN_ENABLED": str(values.get("demo_login_enabled", os.getenv("DEMO_LOGIN_ENABLED", "true"))).strip().lower(),
        "READ_ONLY_SQL_ENABLED": str(values.get("read_only_sql_enabled", os.getenv("READ_ONLY_SQL_ENABLED", "true"))).strip().lower(),
        "TRANSFORMSQL_DRY_RUN_ENABLED": str(values.get("dry_run_enabled", os.getenv("TRANSFORMSQL_DRY_RUN_ENABLED", "true"))).strip().lower(),
        "TRANSFORMSQL_COST_DISPLAY_ENABLED": str(values.get("cost_display_enabled", os.getenv("TRANSFORMSQL_COST_DISPLAY_ENABLED", "true"))).strip().lower(),
        "TRANSFORMSQL_PII_MASKING_ENABLED": str(values.get("pii_masking_enabled", os.getenv("TRANSFORMSQL_PII_MASKING_ENABLED", "false"))).strip().lower(),
        "TRANSFORMSQL_VERTEX_SUGGESTIONS_ENABLED": str(values.get("vertex_suggestions_enabled", os.getenv("TRANSFORMSQL_VERTEX_SUGGESTIONS_ENABLED", "false"))).strip().lower(),
        "TRANSFORMSQL_AUDIT_LOG_ENABLED": str(values.get("audit_log_enabled", os.getenv("TRANSFORMSQL_AUDIT_LOG_ENABLED", "true"))).strip().lower(),
        "CORS_ALLOWED_ORIGINS": str(values.get("cors_allowed_origins", os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:8080"))).strip(),
        "BQ_RETRY_COUNT": str(values.get("bq_retry_count", os.getenv("BQ_RETRY_COUNT", "3"))).strip(),
        "BQ_RETRY_BACKOFF_SECONDS": str(values.get("bq_retry_backoff_seconds", os.getenv("BQ_RETRY_BACKOFF_SECONDS", "1"))).strip(),
        "LOG_LEVEL": str(values.get("log_level", os.getenv("LOG_LEVEL", "INFO"))).strip(),
        "LOG_SQL": str(values.get("log_sql", os.getenv("LOG_SQL", "false"))).strip().lower(),
        "LOG_PII": str(values.get("log_pii", os.getenv("LOG_PII", "false"))).strip().lower(),
        "RATE_LIMIT_ENABLED": str(values.get("rate_limit_enabled", os.getenv("RATE_LIMIT_ENABLED", "true"))).strip().lower(),
        "RATE_LIMIT_PER_MINUTE": str(values.get("rate_limit_per_minute", os.getenv("RATE_LIMIT_PER_MINUTE", "60"))).strip(),
        "CSRF_ENABLED": str(values.get("csrf_enabled", os.getenv("CSRF_ENABLED", "true"))).strip().lower(),
        "GOOGLE_CLOUD_PROJECT": str(values.get("project", "")).strip(),
        "BIGQUERY_LOCATION": str(values.get("location", "US")).strip() or "US",
        "BIGQUERY_MAX_ROWS": str(max(1, min(int(values.get("max_rows", os.getenv("BIGQUERY_MAX_ROWS", "500"))), 5000))),
        "BIGQUERY_MAX_BYTES_BILLED": str(values.get("max_bytes_billed", os.getenv("BIGQUERY_MAX_BYTES_BILLED", ""))).strip(),
        "GOOGLE_APPLICATION_CREDENTIALS": str(values.get("credentials_path", "")).strip(),
    }
    incoming_variables = values.get("variables") or {}
    if isinstance(incoming_variables, dict):
        allowed_keys = {"ENV", "DEMO_LOGIN_ENABLED", "READ_ONLY_SQL_ENABLED", "TRANSFORMSQL_DRY_RUN_ENABLED", "TRANSFORMSQL_COST_DISPLAY_ENABLED", "TRANSFORMSQL_PII_MASKING_ENABLED", "TRANSFORMSQL_VERTEX_SUGGESTIONS_ENABLED", "TRANSFORMSQL_AUDIT_LOG_ENABLED", "CORS_ALLOWED_ORIGINS", "BIGQUERY_LOCATION", "BIGQUERY_MAX_ROWS", "BIGQUERY_MAX_BYTES_BILLED", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "BIGQUERY_PROJECT", "BQ_RETRY_COUNT", "BQ_RETRY_BACKOFF_SECONDS", "LOG_LEVEL", "LOG_SQL", "LOG_PII", "RATE_LIMIT_ENABLED", "RATE_LIMIT_PER_MINUTE", "CSRF_ENABLED", "SESSION_SECRET", "CSRF_SECRET"}
        for key, value in incoming_variables.items():
            if key not in allowed_keys or (key in {"SESSION_SECRET", "CSRF_SECRET"} and not str(value).strip()):
                continue
            allowed[key] = str(value).strip()
    for key, value in allowed.items():
        if "\r" in value or "\n" in value:
            raise ValueError(f"Invalid newline in {key}")
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    env_path = ROOT / ".env"
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()
    existing.update({key: value for key, value in allowed.items() if value})
    for key in allowed:
        if not allowed[key]:
            existing.pop(key, None)
    env_path.write_text("# VOIS local BigQuery settings. Never commit this file.\n" + "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")
    return public_settings()


def run_bigquery(sql):
    """Execute validated SQL with ADC or GOOGLE_APPLICATION_CREDENTIALS."""
    load_env_file()
    if "TODO:" in sql:
        raise ValueError("Only complete SELECT statements can be run in BigQuery")
    if load_settings().read_only_sql_enabled:
        assert_read_only(sql)
    elif not re.search(r"\bSELECT\b", sql, re.I):
        raise ValueError("Only complete SELECT statements can be run in BigQuery")
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError("BigQuery support is not installed. Run: pip install -r requirements.txt") from exc
    project = os.getenv("BIGQUERY_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("BIGQUERY_LOCATION") or None
    max_rows = max(1, min(int(os.getenv("BIGQUERY_MAX_ROWS", "500")), 5000))
    max_billed = os.getenv("BIGQUERY_MAX_BYTES_BILLED", "") or None
    client = bigquery_client()
    config = bigquery.QueryJobConfig(use_legacy_sql=False)
    if max_billed:
        config.maximum_bytes_billed = int(max_billed)
    settings = load_settings()
    if settings.dry_run_enabled:
        dry_config = bigquery.QueryJobConfig(use_legacy_sql=False, dry_run=True, use_query_cache=False)
        if max_billed:
            dry_config.maximum_bytes_billed = int(max_billed)
        dry_job = client.query(sql, job_config=dry_config, location=location)
        dry_bytes = int(getattr(dry_job, "total_bytes_processed", 0) or 0)
        if settings.max_bytes_billed is not None and dry_bytes > settings.max_bytes_billed:
            raise TransformSQLError("BQ_BYTES_LIMIT_EXCEEDED", "Dry-run bytes exceed BIGQUERY_MAX_BYTES_BILLED", 413, {"bytes_processed": dry_bytes, "max_bytes_billed": settings.max_bytes_billed})
    started = time.perf_counter()
    job = client.query(sql, job_config=config, location=location)
    iterator = job.result()
    fields = [{"name": field.name, "type": field.field_type, "mode": field.mode} for field in iterator.schema]
    rows = []
    for row in iterator:
        if len(rows) >= max_rows:
            break
        rows.append({field["name"]: str(row[field["name"]]) if row[field["name"]] is not None else None for field in fields})
    return {"job_id": job.job_id, "project": client.project, "location": location, "row_count": len(rows),
            "schema": fields, "rows": rows, "bytes_processed": getattr(job, "total_bytes_processed", None),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "truncated": len(rows) >= max_rows}


def dry_run_bigquery(sql):
    """Validate and estimate a read-only query without executing it."""
    load_env_file()
    if "TODO:" in str(sql or ""):
        raise TransformSQLError("SQL_VALIDATION_FAILED", "The SQL contains an unresolved TODO expression", 400)
    if load_settings().read_only_sql_enabled:
        assert_read_only(sql)
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError("BigQuery support is not installed. Run: pip install -r requirements.txt") from exc
    settings = load_settings()
    client = bigquery_client()
    config = bigquery.QueryJobConfig(use_legacy_sql=False, dry_run=True, use_query_cache=False)
    if settings.max_bytes_billed is not None:
        config.maximum_bytes_billed = settings.max_bytes_billed
    job = client.query(sql, job_config=config, location=settings.location)
    bytes_processed = int(getattr(job, "total_bytes_processed", 0) or 0)
    if settings.max_bytes_billed is not None and bytes_processed > settings.max_bytes_billed:
        raise TransformSQLError("BQ_BYTES_LIMIT_EXCEEDED", "Dry-run bytes exceed BIGQUERY_MAX_BYTES_BILLED", 413, {"bytes_processed": bytes_processed, "max_bytes_billed": settings.max_bytes_billed})
    return {"valid": True, "bytes_processed": bytes_processed, "estimated_cost_usd": None, "statement_type": "SELECT", "warnings": [], "correlation_id": correlation_id()}


def strip_sql_comments(sql):
    return "\n".join(line for line in str(sql or "").splitlines() if not line.strip().startswith("--")).strip().rstrip(";").strip()


def compare_bigquery(generated_sql, target_table, target_column):
    """Compare generated and target-column values as multisets, independent of row order."""
    load_env_file()
    generated_sql = strip_sql_comments(generated_sql)
    if "TODO:" in generated_sql or not re.search(r"\bSELECT\b", generated_sql, re.I):
        raise ValueError("Only complete generated SELECT statements can be compared")
    if load_settings().read_only_sql_enabled:
        assert_read_only(generated_sql)
    if not target_table or not target_column:
        raise ValueError("target_table and target_column are required")
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError("BigQuery support is not installed. Run: pip install -r requirements.txt") from exc
    project = os.getenv("BIGQUERY_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("BIGQUERY_LOCATION") or None
    max_billed = os.getenv("BIGQUERY_MAX_BYTES_BILLED", "") or None
    client = bigquery_client()
    config = bigquery.QueryJobConfig(use_legacy_sql=False)
    if max_billed:
        config.maximum_bytes_billed = int(max_billed)
    target_ref = qselect(str(target_table).strip().strip("`"), "bigquery")
    target_col = qselect(str(target_column).strip().strip("`"), "bigquery")
    generated = f"SELECT {qselect(str(target_column).strip(), 'bigquery')} AS value FROM ({generated_sql}) AS generated_result"
    target = f"SELECT {target_col} AS value FROM {target_ref}"
    base = f"""WITH generated AS ({generated}), target AS ({target}),
g AS (SELECT COALESCE(TO_JSON_STRING(value), '__NULL__') AS value, COUNT(*) AS value_count FROM generated GROUP BY value),
t AS (SELECT COALESCE(TO_JSON_STRING(value), '__NULL__') AS value, COUNT(*) AS value_count FROM target GROUP BY value),
diff AS (SELECT COALESCE(g.value, t.value) AS value, COALESCE(g.value_count, 0) AS generated_count, COALESCE(t.value_count, 0) AS target_count FROM g FULL OUTER JOIN t USING (value))"""
    summary_sql = base + " SELECT (SELECT COUNT(*) FROM generated) AS generated_row_count, (SELECT COUNT(*) FROM target) AS target_row_count, (SELECT COUNT(*) FROM diff WHERE generated_count != target_count) AS mismatch_value_count"
    deviation_sql = base + " SELECT value, generated_count, target_count, generated_count - target_count AS deviation FROM diff WHERE generated_count != target_count ORDER BY ABS(generated_count - target_count) DESC LIMIT 100"
    started = time.perf_counter()
    summary_job = client.query(summary_sql, job_config=config, location=location)
    summary_row = next(iter(summary_job.result()), None)
    deviation_job = client.query(deviation_sql, job_config=config, location=location)
    deviations = [{"value": row["value"], "generated_count": row["generated_count"], "target_count": row["target_count"], "deviation": row["deviation"]} for row in deviation_job.result()]
    generated_count = int(summary_row["generated_row_count"] if summary_row else 0)
    target_count = int(summary_row["target_row_count"] if summary_row else 0)
    mismatch_count = int(summary_row["mismatch_value_count"] if summary_row else 0)
    return {"status": "PASS" if mismatch_count == 0 and generated_count == target_count else "FAIL",
            "target_table": target_table, "target_column": target_column,
            "generated_row_count": generated_count, "target_row_count": target_count,
            "mismatch_value_count": mismatch_count, "deviations": deviations,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "comparison_mode": "value counts, order independent"}


def norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def header_norm(value):
    """Normalize Excel header spelling, punctuation, and line breaks for matching."""
    text = str(value or "").replace("\xa0", " ").strip().lower()
    text = re.sub(r"[/_\\-]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def qident(value, dialect):
    value = str(value or "").strip()
    if dialect == "bigquery":
        return f"`{value.replace('`', '')}`"
    return f'"{value.replace(chr(34), "")}"'


def qselect(value, dialect):
    value = str(value or "").strip()
    if dialect == "bigquery" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    return qident(value, dialect)


def classify(row):
    kind, rule = norm(row.get("transformation type")), norm(row.get("transformation rule"))
    source = str(row.get("source column name") or "").strip()
    if "direct" in kind or rule in {"direct", "direct mapping"} or (not rule and (source or "derived" not in kind)):
        return "Direct"
    if "join" in kind or "left join" in rule or "inner join" in rule:
        return "Join / lookup"
    if "aggregate" in kind or "group by" in rule or "sum(" in rule or "count(" in rule:
        return "Aggregate"
    return "Derived"


def expression(row, dialect):
    source = str(row.get("source column name") or "").strip()
    rule = str(row.get("transformation rule") or "").strip()
    kind = classify(row)
    if kind == "Direct":
        if not source:
            return "NULL /* TODO: missing source column for direct mapping */"
        return qident(source or "source_expression", dialect)
    if kind == "Aggregate" and source and norm(rule).strip(" '") in {"direct", "direct mapping"}:
        return f"SUM({qident(source, dialect)})"
    if re.search(r"check\s+previous\s+month|previous\s+month\s+data", rule, re.I) and re.search(r"sub_subscription_cd_prev\s*=\s*prev\.sub_subscription_cd", rule, re.I):
        return "CASE\n    WHEN curr.F_ACTIVE = 1\n      AND curr.CUS_CUSTOMER_SUB_TYPE_GROUP <> 'SME'\n      AND prev.CUS_CUSTOMER_SUB_TYPE_GROUP = 'SME'\n      AND prev.unitcountable = 1\n    THEN 1\n    ELSE 0\nEND"
    if kind == "Join / lookup" and source:
        column = lookup_column(source)
        if column:
            return f"L.{qselect(column, dialect)}"
    normalized_rule = norm(rule).strip(" '")
    if normalized_rule in {"populate na", "na", "n/a", "null", "unknown", "populate null"} or normalized_rule.startswith(("populate na", "populate null")):
        return "NULL"
    literal = re.fullmatch(r"(?:populate\s+as\s+)?(?:'([^']*)'|\"([^\"]*)\")", rule.strip(), re.I)
    if literal:
        return "'" + (literal.group(1) if literal.group(1) is not None else literal.group(2)) + "'"
    if not source and re.fullmatch(r"[A-Za-z][A-Za-z ]{2,40}", rule.strip()) and len(rule.split()) <= 5:
        return "'" + rule.strip().replace("'", "''") + "'"
    if normalized_rule in {"populate 1", "1", "1.0"}:
        return "1"
    if normalized_rule in {"populate 0", "0", "0.0"}:
        return "0.0"
    if re.fullmatch(r"(?:current month|current)(?:\s+and\s+plan\s+group\s*<>\s*mbb)?", normalized_rule, re.I) and source:
        if "plan group" in normalized_rule:
            return f"CASE WHEN PLAN_GROUP <> 'MBB' THEN {qident(source, dialect)} ELSE NULL END"
        return qident(source, dialect)
    if normalized_rule.startswith("previous month") and source:
        previous = f"LAG({qident(source, dialect)}) OVER (PARTITION BY SUB_SUBSCRIPTION_CD ORDER BY DAT_MONTH_ID)"
        if "plan group" in normalized_rule:
            return f"CASE WHEN PLAN_GROUP <> 'MBB' THEN {previous} ELSE NULL END"
        return previous
    if source and normalized_rule and normalized_rule == norm(source):
        return qident(source, dialect)
    if source and re.search(r"\|\|\s*['\"]?01['\"]?$", rule, re.I):
        if dialect == "bigquery":
            return f"PARSE_DATE('%Y%m%d', CONCAT(CAST({qident(source, dialect)} AS STRING), '01'))"
        return f"CAST({qident(source, dialect)} || '01' AS DATE)"
    if source and re.search(r"convert.*(?:yyyy-mm-dd|yyyy/mm/dd)", normalized_rule, re.I):
        if dialect == "bigquery":
            return f"PARSE_DATE('%Y-%m-%d', CAST({qident(source, dialect)} AS STRING))"
        return f"CAST({qident(source, dialect)} AS DATE)"
    if source and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*\|\|\s*['\"].*['\"]", rule.strip()):
        literal = re.search(r"['\"](.*)['\"]", rule).group(1)
        return f"CONCAT({qident(source, dialect)}, '{literal}')" if dialect == "bigquery" else f"{qident(source, dialect)} || '{literal}'"
    concat_rule = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*\|\|\s*['\"](.*)['\"]", rule.strip())
    if concat_rule:
        column, literal = concat_rule.groups()
        return f"CONCAT({qselect(column, dialect)}, '{literal}')" if dialect == "bigquery" else f"{qselect(column, dialect)} || '{literal}'"
    if source and re.search(r"convert\s+it\s+into\s+yyyymm", normalized_rule, re.I):
        return f"FORMAT_DATE('%Y%m', {qident(source, dialect)})" if dialect == "bigquery" else f"TO_CHAR({qident(source, dialect)}, 'YYYYMM')"
    if source and "same subscriber from previous month" in normalized_rule:
        return f"LAG({qident(source, dialect)}) OVER (PARTITION BY SUB_SUBSCRIPTION_CD ORDER BY DAT_MONTH_ID)"
    if source and "current month" in normalized_rule and not re.search(r"\b(?:previous|prior)\b", normalized_rule):
        return qident(source, dialect)
    if not source and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rule.strip()):
        return qselect(rule.strip(), dialect)
    replace_match = re.match(r"replace\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)", rule, re.I)
    if replace_match:
        column, old, new = replace_match.groups()
        return f"REPLACE({qselect(column, dialect)}, '{old}', '{new}')"
    count_match = re.search(r"count\s+of\s+([A-Za-z_][A-Za-z0-9_]*)", rule, re.I)
    if count_match:
        return f"COUNT({qselect(count_match.group(1), dialect)}) OVER ()"
    first_match = re.search(r"subscriber\s+id.*first\s+time.*then\s+1.*subsequent.*0", rule, re.I | re.S)
    if first_match and source:
        partition = ", ".join(qselect(part.strip(), dialect) for part in source.split(","))
        return f"CASE WHEN ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY DAT_MONTH_ID) = 1 THEN 1 ELSE 0 END"
    if "populate data from sheet" in normalized_rule and source:
        return qident(source, dialect)
    if re.search(r"to_hex\s*\(\s*sha256\s*\(", rule, re.I):
        payload = re.sub(r"^.*?to_hex\s*\(\s*sha256\s*\((.*)\)\s*\)\s*.*$", r"\1", rule, flags=re.I)
        payload = re.sub(r"\s*\|\|\s*", ", ", payload).strip()
        payload = re.sub(r"\brpt_dt\b", "rpt_dt", payload, flags=re.I)
        if dialect == "bigquery":
            return f"TO_HEX(SHA256(CAST(CONCAT({payload}) AS BYTES)))"
        return f"MD5(CONCAT({payload}))"
    tenure_match = re.search(r"months_between\s*\(\s*\(?\s*to_date\s*\(\s*DAT_MONTH_ID\s*\|\|\s*'01'\s*,\s*'YYYYMMDD'\s*\)\s*\)?\s*,\s*\(?\s*to_date\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'DD/MM/YYYY'\s*\)", rule, re.I)
    if tenure_match and dialect == "bigquery":
        activation = tenure_match.group(1)
        month_date = "PARSE_DATE('%Y%m%d', CONCAT(CAST(DAT_MONTH_ID AS STRING), '01'))"
        activation_date = f"PARSE_DATE('%d/%m/%Y', CAST({activation} AS STRING))"
        return f"DATE_DIFF({month_date}, {activation_date}, MONTH) + 1"
    if re.search(r"convert into date", rule, re.I):
        if dialect == "bigquery":
            return f"PARSE_DATE('%Y%m%d', CONCAT(CAST({qident(source or 'source_expression', dialect)} AS STRING), '01'))"
        return f"CAST({qident(source or 'source_expression', dialect)} AS DATE)"
    if re.search(r"upper\s*\(", rule, re.I) and not re.search(r"\bcase\b|like\s+any", rule, re.I):
        return f"UPPER({qident(source or 'source_expression', dialect)})"
    if re.search(r"lower\s*\(", rule, re.I) and not re.search(r"\bcase\b|like\s+any", rule, re.I):
        return f"LOWER({qident(source or 'source_expression', dialect)})"
    cleaned = rule.replace("_x000D_", " ").replace("\xa0", " ").replace("‘", "'").replace("’", "'")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Mapping sheets sometimes append a prose example after the executable rule.
    cleaned = re.split(r"\bEx\s*:", cleaned, maxsplit=1, flags=re.I)[0].strip()
    cleaned = re.sub(r"\bNUMBER\b", "NUMERIC", cleaned, flags=re.I)
    def convert_substring(match):
        column, start, length = match.groups()
        return f"SUBSTR({qselect(column, dialect)}, {int(start) + 1}, {length})"
    cleaned = re.sub(r"\bsubstring\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", convert_substring, cleaned, flags=re.I)
    if "||" in cleaned and not re.search(r"\b(?:CASE|SELECT|FROM|WHERE|JOIN)\b", cleaned, re.I):
        parts = [part.strip() for part in cleaned.split("||")]
        if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z0-9_().,'%\-+ ]+", part) for part in parts):
            cleaned = "CONCAT(" + ", ".join(parts) + ")" if dialect == "bigquery" else " || ".join(parts)
    cleaned = re.sub(r"\belse\s+o\b", "ELSE 0", cleaned, flags=re.I)
    cleaned = re.sub(r"^\s*step\s*\d+\s*:\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b1\.\s*\(", "1 * (", cleaned)
    cleaned = re.sub(r"\bto_date\s*\(\s*([^,]+?)\s*\|\|\s*'01'\s*,\s*'YYYYMMDD'\s*\)", r"PARSE_DATE('%Y%m%d', CONCAT(CAST(\1 AS STRING), '01'))", cleaned, flags=re.I)
    cleaned = re.sub(r"\bto_date\s*\(\s*([^,]+?)\s*,\s*'DD/MM/YYYY'\s*\)", r"PARSE_DATE('%d/%m/%Y', CAST(\1 AS STRING))", cleaned, flags=re.I)
    formula_only = re.split(r"\s+where\s+", cleaned, maxsplit=1, flags=re.I)[0].strip()
    if dialect == "bigquery" and re.match(r"^(?:PARSE_DATE|PARSE_TIMESTAMP|CAST|SAFE_CAST|IFNULL|COALESCE|SUBSTR|CONCAT|CEIL|FLOOR|ROUND)\s*\(", cleaned, re.I) and cleaned.count("(") == cleaned.count(")"):
        return cleaned
    if re.fullmatch(r"[A-Za-z0-9_().\s+*/%-]+", formula_only) and re.search(r"[+*/%-]", formula_only) and not re.search(r"\b(?:select|from|join|then|else)\b", formula_only, re.I):
        return formula_only
    comma_if = re.match(r"^if\s*\(?(.*?)\s*,\s*(.*?)\s*,\s*(?:else\s+)?(.*?)\)?$", cleaned, re.I)
    if comma_if and not re.search(r"\b(?:select|join|group by)\b", cleaned, re.I):
        condition, true_value, false_value = comma_if.groups()
        return f"CASE WHEN {condition.strip().lstrip('(').rstrip(')')} THEN {true_value.strip()} ELSE {false_value.strip().rstrip(')')} END"
    transaction_case = re.search(r"transaction\s+type\s+is\s+((?:inflow|retention|outflow|base|closing\s+base)\s*(?:,\s*(?:inflow|retention|outflow|base|closing\s+base))*)\s+then\s+(?:use\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+else\s+(?:use\s+)?([A-Za-z_][A-Za-z0-9_]*)", cleaned, re.I)
    if transaction_case:
        values, yes_value, no_value = transaction_case.groups()
        choices = ", ".join("'" + value.strip().upper() + "'" for value in values.split(","))
        return f"CASE WHEN UPPER(TRANSACTION_TYPE) IN ({choices}) THEN {qselect(yes_value, dialect)} ELSE {qselect(no_value, dialect)} END"
    if re.search(r"months_between", cleaned, re.I) and dialect == "bigquery":
        match = re.search(r"months_between\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)", cleaned, re.I)
        if match:
            left, right = match.groups()
            return f"DATE_DIFF({left.strip()}, {right.strip()}, MONTH) + 1"
    any_match = re.search(r"(UPPER\s*\(\s*[A-Za-z_][\w.]*\s*\)|LOWER\s*\(\s*[A-Za-z_][\w.]*\s*\)|[A-Za-z_][\w.]*)\s+LIKE\s+any\s*\(([^)]*)\)", cleaned, re.I)
    if any_match:
        column, values = any_match.groups()
        patterns = [value.strip() for value in values.split(",") if value.strip()]
        replacement = "(" + " OR ".join(f"{column} LIKE {value}" for value in patterns) + ")"
        cleaned = cleaned[:any_match.start()] + replacement + cleaned[any_match.end():]
    if re.search(r"\bcase\s+when\b", cleaned, re.I) and re.search(r"\bend\b", cleaned, re.I):
        case_start = re.search(r"\bcase\s+when\b", cleaned, re.I)
        prefix = cleaned[:case_start.start()].strip()
        if not re.search(r"join|populate|step\s*\d|previous month", prefix, re.I):
            return pretty_case(cleaned[case_start.start():])
        if cleaned.lower().startswith("case"):
            return pretty_case(cleaned)
    partial_case = re.match(r"(.+?)\s+then\s+(.+?)\s+else\s+(.+)$", cleaned, re.I) if not re.search(r"join|populate|step\s*\d|previous month|where", cleaned, re.I) else None
    if partial_case:
        condition, true_value, false_value = partial_case.groups()
        return f"CASE WHEN {condition.strip()} THEN {true_value.strip()} ELSE {false_value.strip()} END"
    if re.match(r"^\s*if\s+", cleaned, re.I) and re.search(r"\bthen\b.*\belse\b", cleaned, re.I):
        cleaned = re.sub(r"^\s*if\s+", "CASE WHEN ", cleaned, flags=re.I)
        cleaned = re.sub(r"\bthen\b", "THEN", cleaned, flags=re.I)
        cleaned = re.sub(r"\belse\b", "ELSE", cleaned, flags=re.I)
        return pretty_case(cleaned + (" END" if not re.search(r"\bend\s*$", cleaned, re.I) else ""))
    arithmetic = re.match(r"^[A-Za-z0-9_().\s+*/%-]+$", cleaned) and re.search(r"[+*/%-]", cleaned) and not re.search(r"\b(?:select|from|join|where|then|else)\b", cleaned, re.I)
    if arithmetic:
        return cleaned
    safe_rule = rule.replace("*/", "* /").replace("\n", " ")[:800]
    return f"NULL /* TODO: {safe_rule} */"


def pretty_case(value):
    """Format a CASE rule as readable BigQuery Standard SQL."""
    text = re.sub(r"\s+", " ", value.replace("_x000D_", " ")).strip()
    match = re.match(r"CASE\s+WHEN\s+(.+?)\s+THEN\s+(.+?)\s+ELSE\s+(.+?)\s+END$", text, re.I)
    if not match:
        return text
    condition, true_value, false_value = match.groups()
    condition = re.sub(r"\bupper\s*\(", "UPPER(", condition, flags=re.I)
    condition = re.sub(r"\blower\s*\(", "LOWER(", condition, flags=re.I)
    if condition.startswith("(") and condition.endswith(")"):
        condition = condition[1:-1].strip()
    parts = [part.strip() for part in re.split(r"\s+OR\s+", condition, flags=re.I) if part.strip()]
    if len(parts) > 1:
        condition_sql = "\n".join([f"    WHEN {parts[0]}"] + [f"      OR {part}" for part in parts[1:]])
    else:
        condition_sql = f"    WHEN {condition.strip()}"
    return f"CASE\n{condition_sql}\n    THEN {true_value.strip()}\n    ELSE {false_value.strip()}\nEND"


def lookup_column(value):
    """Return the first usable SQL column from a descriptive mapping value."""
    match = re.search(r"(?:^|[,\s])([A-Za-z_][A-Za-z0-9_]*)\s*(?:,|$)", str(value or "").strip())
    return match.group(1) if match else ""


def join_from_clause(row, dialect):
    """Build a conservative LEFT JOIN clause for common mapping-sheet lookup text."""
    target = str(row.get("target table name") or "target_table").strip()
    source_table = str(row.get("source table name") or "").strip()
    rule = str(row.get("transformation rule") or "")
    lookup = ""
    match = re.search(r"(?:left\s+join|inner\s+join|join)\s+([A-Za-z0-9_.`*\-]+)", rule, re.I)
    if match:
        lookup = match.group(1).strip("`")
        lookup = lookup.replace("-*.*", "_*").replace("*.*", "*")
    if not lookup and source_table and source_table.lower() not in {"source_table", "aa_base"}:
        lookup = source_table.split(",")[0].strip().strip("`")
    if not lookup:
        return f"{qselect(target, dialect)} AS T"
    aliases = {"DAT_MONTH_ID", "SUB_SUBSCRIPTION_CD", "CUS_CUSTOMER_CD", "PRICEPLAN_DESC", "CUS_CUSTOMER_CD_HASH_GRP"}
    pairs = re.findall(r"\bT\.?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*T1\.?\s*([A-Za-z_][A-Za-z0-9_]*)", rule, re.I)
    keys = [left for left, right in pairs if left.upper() == right.upper()]
    if not keys:
        upper_rule = rule.upper()
        keys = [key for key in aliases if key in upper_rule]
    if not keys:
        keys = ["PRICEPLAN_DESC"] if "PRICEPLAN" in rule.upper() else ["DAT_MONTH_ID", "SUB_SUBSCRIPTION_CD"]
    condition = " AND ".join(f"T.{key} = L.{key}" for key in dict.fromkeys(keys))
    return f"{qselect(target, dialect)} AS T\nLEFT JOIN {qident(lookup, dialect)} AS L\n  ON {condition}"


def previous_month_from_clause(row, dialect):
    target = str(row.get("target table name") or "target_table").strip()
    table = qselect(target, dialect)
    return f"{table} AS curr\nLEFT JOIN {table} AS prev\n  ON curr.sub_subscription_cd_prev = prev.sub_subscription_cd"


def validate_sql(sql, row, kind):
    """Return structural validation errors before a row is labelled READY."""
    errors = []
    text = str(sql or "")
    executable = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    if "TODO:" in executable or "source_expression" in executable:
        errors.append("incomplete expression")
    if not re.search(r"\bSELECT\b", executable, re.I):
        errors.append("missing SELECT")
    if not re.search(r"\bFROM\b", executable, re.I):
        errors.append("missing FROM")
    if executable.count("(") != executable.count(")"):
        errors.append("unbalanced parentheses")
    if len(re.findall(r"\bCASE\b", executable, re.I)) != len(re.findall(r"\bEND\b", executable, re.I)):
        errors.append("incomplete CASE expression")
    if executable.count("'") % 2:
        errors.append("unbalanced string literal")
    if kind == "Join / lookup" and re.search(r"\bJOIN\b", executable, re.I) and not re.search(r"\bON\b", executable, re.I):
        errors.append("join is missing ON condition")
    if re.search(r"\b(?:None|nan|undefined)\b", executable, re.I):
        errors.append("invalid null token")
    return errors


def find_mapping(rows):
    best = None
    for index, row in enumerate(rows[:80]):
        normalized = [header_norm(value) for value in row]
        standard_score = sum(any(header_norm(alias) in normalized for alias in ALIASES[key]) for key in HEADERS)
        contract_score = sum(any(header_norm(alias) in normalized for alias in aliases) for aliases in CONTRACT_FIELD_ALIASES.values())
        if contract_score >= 5:
            score = contract_score + 100
            candidate = (score, index, normalized, "contract")
        elif standard_score >= 5:
            candidate = (standard_score, index, normalized, "standard")
        else:
            continue
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def scan_workbook(path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    sheets = []
    for worksheet in workbook.worksheets:
        rows = list(worksheet.iter_rows(values_only=True))
        header = find_mapping(rows)
        if not header:
            sheets.append({"name": worksheet.title, "reference": True, "rows": 0, "records": []})
            continue
        _, header_row, normalized, layout = header
        records = []
        for row in rows[header_row + 1:]:
            row_headers = {header_norm(value) for value in row if value not in (None, "")}
            known_headers = {header_norm(header) for header in HEADERS} | {header_norm(alias) for aliases in CONTRACT_FIELD_ALIASES.values() for alias in aliases} | {header_norm(alias) for values in ALIASES.values() for alias in values} | {header_norm(alias) for values in OPTIONAL_ALIASES.values() for alias in values}
            if len(row_headers.intersection(known_headers)) >= 3:
                continue
            if layout == "contract":
                positions = {}
                for key, aliases in CONTRACT_FIELD_ALIASES.items():
                    alias = next((alias for alias in aliases if header_norm(alias) in normalized), None)
                    if alias:
                        positions[key] = normalized.index(header_norm(alias))
                data_type_positions = [i for i, value in enumerate(normalized) if value == header_norm("data type")]
                # This workbook family uses DATA TYPE twice: once after SOURCE COLUMN
                # and once after TARGET COLUMN. Resolve both by their fixed relative
                # positions instead of treating the duplicate header as one field.
                if data_type_positions:
                    positions.setdefault("source column data type", data_type_positions[0])
                    positions.setdefault("target column data type", data_type_positions[-1])
                record = {key: (row[index] if index < len(row) else None) for key, index in positions.items()}
                logic = str(record.get("transformation rule") or "").strip()
                nature = str(record.get("transformation type") or "").strip()
                if norm(logic) in {"direct", "direct mapping"}:
                    record["transformation type"] = "Aggregate" if "aggregate" in norm(nature) else "Direct"
                elif "aggregate" in norm(nature):
                    record["transformation type"] = "Aggregate"
                else:
                    record["transformation type"] = "Derived"
                if not str(record.get("source table name") or "").strip() and str(record.get("target table name") or "").strip():
                    record["source table name"] = record["target table name"]
            else:
                record = {}
                for key in HEADERS:
                    alias = next(alias for alias in ALIASES[key] if header_norm(alias) in normalized)
                    index = normalized.index(header_norm(alias))
                    record[key] = row[index] if index < len(row) else None
            for key, aliases in OPTIONAL_ALIASES.items():
                alias = next((alias for alias in aliases if header_norm(alias) in normalized), None)
                if alias:
                    index = normalized.index(header_norm(alias))
                    record[key] = row[index] if index < len(row) else None
            # Some mapping sections omit the header row and use this compact layout:
            # source table, source column, rule, target table, target column, target type.
            # In that layout the normal header-position scan looks valid but shifts
            # every target field one place to the right.
            compact_target = str(record.get("transformation type") or "").strip()
            compact_target_col = str(record.get("transformation rule") or "").strip()
            compact_rule = str(record.get("source column data type") or "").strip()
            compact_type = str(record.get("target column name") or "").strip()
            if compact_target.lower().startswith(("stg_", "f_")) and re.search(r"(?:varchar|number|float|date|timestamp|string|boolean|int|decimal)", compact_type, re.I):
                record = {
                    "source table name": record.get("source table name"),
                    "source column name": record.get("source column name"),
                    "source column data type": None,
                    "transformation type": "Direct" if compact_rule.lower() in {"direct", "direct mapping"} else "Derived",
                    "transformation rule": compact_rule,
                    "target table name": compact_target,
                    "target column name": compact_target_col,
                    "target column data type": compact_type,
                }
            meaningful_fields = ("source table name", "source column name", "target table name", "target column name", "transformation rule")
            if any(record.get(key) not in (None, "") for key in meaningful_fields):
                records.append(record)
        sheets.append({"name": worksheet.title, "reference": False, "rows": len(records), "records": records, "header_row": header_row + 1})
    workbook.close()
    return sheets


def generate_queries(sheets, dialect="bigquery"):
    output = []
    for sheet in sheets:
        if sheet["reference"]:
            continue
        groups = {}
        for row in sheet["records"]:
            target = str(row.get("target table name") or f"{sheet['name']}_generated").strip()
            groups.setdefault(target, []).append(row)
        sql = [f"-- Generated from sheet: {sheet['name']}", f"-- Dialect: {dialect}", "-- Review TODO rules before deployment.", ""]
        for target, rows in groups.items():
            rows = [row for row in rows if str(row.get("target column name") or "").strip()]
            if not rows:
                continue
            source = next((str(row.get("source table name")).strip() for row in rows if row.get("source table name")), "source_table")
            columns = ",\n".join(f"  {qident(row['target column name'], dialect)}" for row in rows)
            expressions = ",\n".join(
                f"  {expression(row, dialect)} AS {qident(row['target column name'], dialect)} -- {classify(row)}"
                for row in rows
            )
            sql.extend([f"INSERT INTO {qident(target, dialect)} (", columns, ")", "SELECT", expressions, f"FROM {qident(source, dialect)};", ""])
        output.append({"sheet": sheet["name"], "rows": sheet["rows"], "sql": "\n".join(sql).strip()})
    return output


def generate_row_queries(sheets, dialect="bigquery"):
    """Create one auditable SQL statement per mapping row."""
    output = []
    row_number = 0
    for sheet in sheets:
        if sheet["reference"]:
            continue
        for row in sheet["records"]:
            target_table = str(row.get("target table name") or f"{sheet['name']}_generated").strip()
            target_column = str(row.get("target column name") or "").strip()
            if not target_column:
                continue
            source_table = str(row.get("source table name") or "source_table").strip()
            kind = classify(row)
            query_from = target_table if kind != "Direct" else source_table
            expr = expression(row, dialect)
            status = "READY" if "TODO:" not in expr else "REVIEW"
            row_number += 1
            source_select = qselect(str(row.get("source column name") or "").strip(), dialect) if row.get("source column name") else None
            pretty_expr = expr.replace("\n", "\n    ")
            select_lines = [f"    {source_select}," if source_select and kind != "Direct" else None, f"    {pretty_expr} AS {qselect(target_column, dialect)}"]
            select_lines = [line for line in select_lines if line]
            is_previous_join = re.search(r"check\s+previous\s+month|previous\s+month\s+data", str(row.get("transformation rule") or ""), re.I) and re.search(r"sub_subscription_cd_prev\s*=\s*prev\.sub_subscription_cd", str(row.get("transformation rule") or ""), re.I)
            if is_previous_join:
                from_clause = previous_month_from_clause(row, dialect)
            else:
                from_clause = join_from_clause(row, dialect) if kind == "Join / lookup" and "TODO:" not in expr else qselect(query_from, dialect)
            sql = "\n".join([
                "-- BigQuery Standard SQL",
                f"-- Sheet: {sheet['name']} | Mapping row: {row_number}",
                f"-- Transformation: {kind} | Status: {status}",
                f"SELECT",
                "\n".join(select_lines),
                f"FROM {from_clause};",
            ])
            validation_errors = validate_sql(sql, row, kind)
            status = "READY" if not validation_errors else "REVIEW"
            sql = sql.replace(f"Status: {'READY' if not validation_errors else 'REVIEW'}", f"Status: {status}")
            output.append({"id": row_number, "sheet": sheet["name"], "source_table": source_table,
                           "source_column": str(row.get("source column name") or ""),
                           "target_table": target_table, "target_column": target_column,
                           "transformation_type": kind, "rule": str(row.get("transformation rule") or ""),
                           "source_directory_path": str(row.get("source directory path") or ""),
                           "source_datatype_changed": str(row.get("source datatype changed") or ""),
                           "target_dataset": str(row.get("target dataset") or ""),
                           "comment": str(row.get("comment") or ""),
                           "query_from": query_from, "status": status, "validation_errors": validation_errors,
                           "expression": expr, "sql": sql})
    return output


def build_result(path, dialect="bigquery"):
    sheets = scan_workbook(path)
    queries = generate_queries(sheets, dialect)
    row_queries = generate_row_queries(sheets, dialect)
    rows = [row for sheet in sheets if not sheet["reference"] for row in sheet["records"]]
    direct = sum(classify(row) == "Direct" for row in rows)
    return {"sheets": [{k: v for k, v in sheet.items() if k != "records"} for sheet in sheets], "queries": queries, "row_queries": row_queries, "summary": {"sheet_count": len(sheets), "mapping_sheet_count": len(queries), "row_count": len(rows), "direct_count": direct, "rule_count": len(rows) - direct, "ready_count": sum(r["status"] == "READY" for r in row_queries), "review_count": sum(r["status"] == "REVIEW" for r in row_queries)}}


def parse_multipart(handler):
    content_type = handler.headers.get("Content-Type", "")
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        return {}
    boundary = ("--" + match.group(1).strip('"')).encode()
    body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    fields = {}
    for part in body.split(boundary):
        if b"Content-Disposition:" not in part:
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        disposition = head.decode("utf-8", "ignore")
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        value = value.rstrip(b"\r\n-")
        fields[name] = {"filename": filename_match.group(1) if filename_match else None, "value": value}
    return fields


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        settings = load_settings()
        origin = self.headers.get("Origin")
        origin_host = urlparse(origin).hostname if origin else None
        cloudshell_origin = bool(origin_host and (origin_host.endswith(".cloudshell.dev") or origin_host.endswith(".cloudshell.googleusercontent.com")))
        if origin and (origin in settings.cors_allowed_origins or cloudshell_origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        elif not origin and settings.env == "local":
            self.send_header("Access-Control-Allow-Origin", "http://localhost:8080")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token, X-Correlation-ID")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json({"status": "ok", "version": "1.0.0"})
            return
        if self.path == "/api/config":
            settings = load_settings()
            self.send_json({"env": settings.env, "demo_login_enabled": settings.demo_login_enabled})
            return
        if self.path == "/api/settings":
            self.send_json(public_settings())
            return
        if self.path == "/readyz":
            settings = load_settings()
            project = resolve_bigquery_project()
            ready = bool(project or settings.env == "local")
            self.send_json({"status": "ready" if ready else "not_ready", "checks": {"configuration": "ok" if ready else "failed", "auth_source": bigquery_auth_source(), "project_discovered": bool(project)}}, 200 if ready else 503)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_json(update_settings(payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if self.path == "/api/test-bigquery":
            try:
                result = dry_run_bigquery("SELECT 1 AS vois_connection_test")
                self.send_json({"status": "connected", "project": public_settings().get("project", ""), "location": public_settings().get("location", ""), "auth_source": bigquery_auth_source(), "bytes_processed": result.get("bytes_processed", 0)})
            except Exception as exc:
                self.send_json({"status": "failed", "error": str(exc)}, 503)
            return
        if self.path.startswith("/api/v1/"):
            request_id = self.headers.get("X-Correlation-ID") or correlation_id()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/v1/run-bigquery/dry-run":
                    result = dry_run_bigquery(str(request.get("sql") or ""))
                elif self.path == "/api/v1/run-bigquery":
                    result = run_bigquery(str(request.get("sql") or ""))
                    result["correlation_id"] = request_id
                elif self.path == "/api/v1/compare-bigquery":
                    result = compare_bigquery(request.get("generated_sql"), request.get("target_table"), request.get("target_column"))
                    result["correlation_id"] = request_id
                else:
                    self.send_json(error_payload(TransformSQLError("INVALID_REQUEST", "Unknown v1 endpoint", 404), request_id), 404)
                    return
                self.send_json(result)
            except Exception as exc:
                status = exc.status if isinstance(exc, TransformSQLError) else 503
                self.send_json(error_payload(exc, request_id), status)
            return
        if self.path == "/api/run-bigquery":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                sql = str(request.get("sql") or "").strip()
                if not sql:
                    self.send_json({"error": "sql is required"}, 400)
                    return
                self.send_json(run_bigquery(sql))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400 if isinstance(exc, ValueError) else 503)
            return
        if self.path == "/api/compare-bigquery":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                result = compare_bigquery(request.get("generated_sql"), request.get("target_table"), request.get("target_column"))
                self.send_json(result)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400 if isinstance(exc, ValueError) else 503)
            return
        if self.path != "/api/generate":
            self.send_error(404)
            return
        form = parse_multipart(self)
        upload = form.get("file")
        if not upload or not upload.get("filename"):
            self.send_error(400, "file is required")
            return
        filename = str(upload.get("filename") or "")
        if not filename.lower().endswith(".xlsx"):
            self.send_json({"error": "Only .xlsx Excel workbooks are supported", "code": "UNSUPPORTED_WORKBOOK_TYPE"}, 415)
            return
        if not upload.get("value"):
            self.send_json({"error": "The uploaded workbook is empty", "code": "EMPTY_WORKBOOK"}, 400)
            return
        temp_handle = tempfile.NamedTemporaryFile(dir=ROOT, suffix=".xlsx", delete=False)
        temp = Path(temp_handle.name)
        with temp_handle:
            temp_handle.write(upload["value"])
        dialect = (form.get("dialect") or {}).get("value", b"bigquery").decode("utf-8", "ignore") or "bigquery"
        try:
            payload = build_result(temp, dialect)
        except Exception as exc:
            self.send_json({"error": f"Excel workbook could not be read: {exc}", "code": "INVALID_WORKBOOK"}, 400)
            return
        finally:
            temp.unlink(missing_ok=True)
        self.send_json(payload)


def main():
    parser = argparse.ArgumentParser(description="TransformSQL backend")
    parser.add_argument("input", nargs="?", help="Excel workbook to process")
    parser.add_argument("--dialect", default="bigquery", choices=["bigquery", "postgres", "snowflake"])
    parser.add_argument("--output", default="generated_queries.sql")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.serve:
        load_env_file()
        print(f"TransformSQL backend listening on http://localhost:{args.port}")
        ThreadingHTTPServer(("localhost", args.port), Handler).serve_forever()
    elif args.input:
        result = build_result(Path(args.input), args.dialect)
        Path(args.output).write_text("\n\n-- ==================================================\n\n".join(item["sql"] for item in result["queries"]) + "\n", encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        print(f"SQL written to {args.output}")
    else:
        parser.error("provide an input workbook or use --serve")


if __name__ == "__main__":
    main()
