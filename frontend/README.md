# TransformSQL frontend

Static browser UI. It supports login, workbook upload, sheet discovery, SQL preview, direct/derived filtering, row approval, BigQuery execution, private target comparison, result/deviation display, copy, and downloads.

The standard runner serves this folder from the backend on the same origin, avoiding Cloud Shell cross-port preview failures. Set `window.TRANSFORMSQL_API_BASE` only when the backend is intentionally hosted separately.

## Local startup

Start the combined backend/frontend server on port `8080`:

```powershell
python ..\backend\transformsql_engine.py --serve --port 8080
```

Open `http://localhost:8080` and use `admin` / `transform123` for the demo login.

The frontend calls:

- `POST /api/generate` to scan the workbook and create row-level SQL.
- `POST /api/run-bigquery` to execute the selected generated query.
- `POST /api/compare-bigquery` to compare the generated output with the target table column.

BigQuery execution and comparison require the same-origin backend and configured Google credentials.
