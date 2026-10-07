# Bulk Certificate Generator

FastAPI + SQLite backend that accepts up to 10,000 recipients in one request, creates PDFs from one predefined ReportLab template, and exposes progress and downloads. Python 3.11+ required.

## Setup and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
uvicorn app.asgi:app --host 127.0.0.1 --port 8000 --workers 1
```

Visit http://127.0.0.1:8000/docs for interactive OpenAPI documentation. The database is created automatically at `data/certificates.sqlite3`; override its path with `DATABASE_PATH`. Use one application process: startup recovery assumes exclusive ownership of this database. Avoid `--reload` while generating jobs.

For exact versions used during verification, install `requirements.lock` before the editable project:

```bash
pip install -r requirements.lock
pip install -e '.[test]'
python -m pytest -q
```

## Submit, poll, and download

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' --data-binary @examples/request.json
```

A `202 Accepted` response contains `id`, `status_url`, and `results_url`. This confirms durable acceptance, not completion. Save the returned job ID:

```bash
JOB_ID=replace-with-returned-id
curl "http://127.0.0.1:8000/jobs/$JOB_ID"
curl "http://127.0.0.1:8000/jobs/$JOB_ID/certificates?offset=0&limit=100"
```

Status includes `total`, `pending`, `processed`, `succeeded`, `failed`, `progress_percent`, and timestamps. Poll every second until `completed`, `partial_failure`, or `failed`. Result pages contain each input's zero-based `position`, certificate ID, validated name/email, status, error, and download URL. Advance `offset` by `limit` until the returned page is shorter than `limit` (default 100, maximum 1,000).

Use a successful result's `download_url`:

```bash
CERTIFICATE_ID=replace-with-returned-certificate-id
curl -f "http://127.0.0.1:8000/jobs/$JOB_ID/certificates/$CERTIFICATE_ID" \
  -o certificate.pdf
```

Downloads have `application/pdf` content type and an attachment filename. A missing job/certificate returns 404; pending or failed certificates return 409. All-invalid jobs are accepted, then finish as `failed` with per-recipient explanations.

## Validation and behavior

The request requires `title`, `organization`, an ISO date `issued_on`, and a nonempty list of recipient objects. Each recipient requires `name` and `email`; unknown fields are rejected. Names are trimmed, capped at 120 characters, and checked for blank/control characters. Title and organization are capped at 160 characters. Email syntax is validated without requiring DNS availability. Duplicate recipients are treated as distinct input entries.

Malformed envelope data (including non-object recipients) returns 422 without creating a job. Invalid fields inside recipient objects produce failed result entries while valid entries continue. Invalid entries retain their input position and field errors, rather than storing arbitrary invalid input. The predefined template uses built-in Latin fonts, so unsupported names/text are explicitly rejected; multilingual support would require an embedded font and script shaping.

## Design decisions

- A database-backed queue and one polling thread keep HTTP submission independent of generation without a broker. SQLite is a relational database, uses foreign keys and an indexed result table, and runs in WAL mode. All SQL input is parameterized.
- Job creation and recipient insertion are one transaction. Queue claiming uses `BEGIN IMMEDIATE`; the worker renders one PDF at a time and commits each result separately. A failing renderer records a generic error, logs details server-side, and continues with the next recipient. A systemic database failure is logged; restart recovers interrupted work.
- PDFs are stored as database BLOBs so the certificate bytes and successful status commit atomically. This avoids filesystem paths supplied by users and simplifies backup. Jobs survive process restarts: startup requeues `running` jobs, processing only still-pending entries. Completed entries are never regenerated during recovery.
- Terminal states: `completed` means all succeeded, `partial_failure` means both successes and failures, and `failed` means no successes. `processed = succeeded + failed`, including validation failures, so a queued job can already have nonzero progress. Recipient states are `pending`, `succeeded`, and `failed`.
- One worker bounds active rendering concurrency. Submission is O(n); result retrieval is paginated and excludes PDF data. The 10,000-recipient cap bounds individual requests, but there is no global queue quota or HTTP body-size limit. PDFs and historical jobs remain until manually removed; SQLite BLOB storage is appropriate for this assignment, not unlimited archival storage.
- There is no authentication or tenant isolation. UUIDs identify resources but are not access control. Keep the demo on localhost. A deployed service should add authentication/authorization, proxy body limits, retention, rate limits, migrations, and a separate durable worker with leases before scaling to multiple application processes. Repeated POSTs create new jobs; idempotency is intentionally outside scope.

This background approach addresses the in-process task limitation described in [FastAPI's background-task documentation](https://fastapi.tiangolo.com/tutorial/background-tasks/): the queue is persisted and startup resumes incomplete work. It is still a single-process design, not a distributed task system.

## Code and tests

`app/main.py` contains request schemas, the PDF renderer, SQLite store/queue processor, and application factory; `app/asgi.py` exposes the runnable app. The renderer is injectable so tests can force a recipient failure without adding a special production API field. Tests use isolated temporary databases and verify creation, envelope and recipient validation, PDF text, progress while processing, failure isolation, pagination, cross-job download checks, restart recovery, ZIP contents, and selective retry. CI runs the suite on Python 3.11 and 3.13.

To change the design, edit `render_certificate`; to add recipient fields, update `Recipient`, the table/schema, insertion, and renderer. Existing databases require a migration when changing table definitions because initial schema creation does not alter existing tables.

## Bulk ZIP download

Once a job finishes, download all successful PDFs in one request:

```bash
curl -f "http://127.0.0.1:8000/jobs/$JOB_ID/archive" -o certificates.zip
```

The ZIP contains PDFs named by certificate UUID and `manifest.json`, which includes the job metadata and every recipient's ID, input position, name/email, status, error, and PDF filename (null for failures). All-invalid jobs return a manifest-only ZIP. Active jobs return 409; unknown jobs return 404. Each archive is built from a consistent database snapshot and streamed from a temporary disk file in 64 KiB chunks. Only one PDF at a time is loaded, plus manifest metadata; ensure temporary disk space is available for the archive. Archives contain personal information and should be shared accordingly.

## Retry generation failures

If a temporary rendering problem has been fixed, retry failed generation without resubmitting successful recipients:

```bash
curl -X POST "http://127.0.0.1:8000/jobs/$JOB_ID/retry"
```

A 202 response includes `retried`, the number of requeued certificates. The same job and certificate IDs are preserved. Successful PDFs remain available; validation errors remain failed because they need corrected input submitted in a new job. Job status resets to `queued`, `completed_at` clears, and progress can decrease until processing finishes again. Retry does not happen automatically and no retry history is stored.

Retry uses a write transaction so overlapping retry requests cannot enqueue a job twice. Queued/running jobs and jobs with no eligible generation failures return 409; unknown jobs return 404. A retried renderer can fail again, and the normal per-recipient failure handling still applies.
