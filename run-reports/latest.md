# VOIS TransformSQL Vertex run

- Timestamp (UTC): 2026-10-26T19:07:00Z
- Git commit: ff6f689
- Workbook: /home/sameer_shaik3/VOIS_TransformSQL_Engine/GR_CONTRACT_IBRO_STM_V0.7.xlsx
- Failures: 0
- Warnings: 0

| Status | Check | Details |
|---|---|---|
| PASS | Git pull | Latest origin/main fetched before validation. |
| PASS | Dependencies | Backend requirements installed. |
| PASS | Backend syntax | Python compilation passed. |
| PASS | Frontend syntax | JavaScript syntax check passed. |
| PASS | Workbook regression | Parsed /home/sameer_shaik3/VOIS_TransformSQL_Engine/GR_CONTRACT_IBRO_STM_V0.7.xlsx and generated mapping SQL. |
| PASS | Backend health | Backend is listening on port 8081. |
| PASS | Frontend health | Frontend is listening on port 8080. |
| PASS | BigQuery runtime | Vertex/Google ADC readiness check passed. |

## Runtime evidence
```json
{"status": "ok", "version": "1.0.0"}```
```json
{"status": "ready", "checks": {"configuration": "ok", "auth_source": "Vertex/Google Application Default Credentials", "project_discovered": true}}```
## Recent backend log
```text
127.0.0.1 - - [26/Oct/2026 19:07:00] "GET /healthz HTTP/1.1" 200 -
127.0.0.1 - - [26/Oct/2026 19:07:00] "GET /readyz HTTP/1.1" 200 -
```
## Recent frontend log
```text
nohup: ignoring input
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/usr/lib/python3.12/http/server.py", line 1314, in <module>
    test(
  File "/usr/lib/python3.12/http/server.py", line 1261, in test
    with ServerClass(addr, HandlerClass) as httpd:
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/socketserver.py", line 457, in __init__
    self.server_bind()
  File "/127.0.0.1 - - [26/Oct/2026 19:07:00] "GET / HTTP/1.1" 200 -
d
    return super().server_bind()
           ^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/http/server.py", line 136, in server_bind
    socketserver.TCPServer.server_bind(self)
  File "/usr/lib/python3.12/socketserver.py", line 473, in server_bind
    self.socket.bind(self.server_address)
OSError: [Errno 98] Address already in use
```

Logs remain local in backend.log and frontend.log; secrets are not written to this report.
