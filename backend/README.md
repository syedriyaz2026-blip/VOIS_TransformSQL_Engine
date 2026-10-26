# TransformSQL backend engine

`transformsql_engine.py` is intentionally self-contained. It contains workbook scanning, mapping detection, transformation classification, SQL generation, CLI execution, and the HTTP API.

Install and run:

```powershell
pip install -r requirements.txt
python transformsql_engine.py --serve --port 8081
```

Generate a file:

```powershell
python transformsql_engine.py input.xlsx --dialect bigquery --output generated.sql
```

The API result includes grouped `queries` and `row_queries`. Each row query includes source and target metadata, transformation type, original rule, generated expression, `READY`/`REVIEW` status, and executable SQL text.

The scanner supports both the canonical mapping headers (`Source Table Name`, `Target Column Name`, and related fields) and the contract workbook layout used by `GR_CONTRACT_IBRO_STM_V0.7_5653.xlsx`: `SOURCE TABLE`, `SOURCE COLUMN`, duplicated `DATA TYPE` columns, `TARGET TABLE`, `TARGET COLUMN`, `TRANSFORMATION LOGIC`, and `Nature (GroupBy/ Aggregate over Dim)`. Header matching is case-, whitespace-, punctuation-, line-break-, and column-order-independent. The duplicate data-type columns are resolved by position, and blank source-table cells fall back to the row's target table when the mapping layout requires it.

The default dialect is BigQuery Standard SQL. Use `--dialect bigquery` explicitly in automated jobs.

## Run in BigQuery

Install the BigQuery client:

```powershell
pip install -r requirements.txt
```

In Vertex AI / Workbench, the client uses the environment's active Google Application Default Credentials. The standard VOIS project and source/target datasets are preconfigured in the engine and shown in the UI; environment variables may override them for another deployment. No service-account JSON is uploaded, displayed, or required by the VOIS UI. Never commit `.env` or a service-account JSON file.

After generating a row query in the UI, click **Run in BigQuery**. The result table is returned to the same interface. The backend limits displayed rows using `BIGQUERY_MAX_ROWS` and can enforce a cost ceiling with `BIGQUERY_MAX_BYTES_BILLED`.

Table qualification is resolved per mapping row. The fixed VOIS source dataset is applied to source tables and the fixed VOIS target dataset is applied to target tables; the generated references therefore have the form `` `vf-gr-mc2-nonlive.dataset.table` ``. The engine still supports environment overrides for another deployment.

After a successful run, the UI automatically compares the generated result with the selected row's target table and target column. The comparison SQL is generated and executed privately by the backend and is never displayed or returned to the browser. The dashboard reports `PASS` or `FAIL`, row counts, mismatched values, and deviations.

Generated query result rows and comparison summaries are saved as JSON objects under `gs://narasimha-test-bucket/vois-transformsql/` using the same Vertex/Google ADC. The runtime identity needs `roles/storage.objectCreator` on that bucket in addition to the BigQuery permissions.

The frontend reads `/api/config` at startup. Demo credentials are enabled only when `ENV=local`; staging and production must set `DEMO_LOGIN_ENABLED=false` and use a real authentication layer before external deployment. The Settings page is informational in Vertex mode: it reports the active runtime authentication and project, and provides a connection test without asking users to configure credentials.

For repeatable Vertex validation, run `../vertex_run.sh` from the repository root. It pulls `origin/main`, checks backend/frontend syntax, optionally parses `INPUT_XLSX`, starts both local services, checks `/readyz`, and stores a non-secret Markdown run report in `run-reports/` before pushing it to Git.
