"""A durable SQLite queue and one bounded background worker."""
import json
import logging
import os
import sqlite3
import threading
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.pdfgen import canvas

log = logging.getLogger(__name__)


class Recipient(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Name must contain printable text')
        # The predefined PDF template uses built-in Latin fonts.
        try:
            value.encode('cp1252')
        except UnicodeEncodeError:
            raise ValueError('This template supports Latin-script names only')
        return value


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    organization: str = Field(min_length=1, max_length=160)
    issued_on: date
    recipients: list[dict] = Field(min_length=1, max_length=10000)

    @field_validator('title', 'organization')
    @classmethod
    def clean_text(cls, value):
        return Recipient.clean_name(value)


def now():
    return datetime.now(timezone.utc).isoformat()


def render_certificate(name, title, organization, issued_on, certificate_id):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)
    pdf.setTitle(f'Certificate - {name}')
    pdf.setStrokeColor(colors.HexColor('#244963'))
    pdf.setLineWidth(3)
    pdf.rect(28, 28, width - 56, height - 56)

    def line(text, y, size=24):
        # Shrink long fields to fit without clipping.
        while pdf.stringWidth(text, 'Helvetica', size) > width - 100:
            size -= 0.5
        pdf.setFont('Helvetica', size)
        pdf.drawCentredString(width / 2, y, text)

    line('CERTIFICATE OF COMPLETION', 465, 30)
    line('Presented to', 390, 18)
    line(name, 330, 32)
    line(f'For completing: {title}', 265, 22)
    line(organization, 195, 22)
    line(f'Issued on {issued_on}', 140, 16)
    line(f'Certificate ID: {certificate_id}', 70, 10)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, organization TEXT NOT NULL,
                issued_on TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                completed_at TEXT);
            CREATE TABLE IF NOT EXISTS certificates (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                position INTEGER NOT NULL, name TEXT, email TEXT, status TEXT NOT NULL,
                error TEXT, pdf BLOB, UNIQUE(job_id, position));
            CREATE INDEX IF NOT EXISTS certificates_job ON certificates(job_id, position);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def process_one(self, renderer):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if job is None:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
        with self.connect() as db:
            rows = db.execute("SELECT id, name FROM certificates WHERE job_id=? AND status='pending' ORDER BY position", (job['id'],)).fetchall()
        for row in rows:
            try:
                data = renderer(row['name'], job['title'], job['organization'], job['issued_on'], row['id'])
                with self.connect() as db:
                    db.execute("UPDATE certificates SET status='succeeded', pdf=? WHERE id=?", (data, row['id']))
            except Exception:
                log.exception('Certificate generation failed: %s', row['id'])
                with self.connect() as db:
                    db.execute("UPDATE certificates SET status='failed', error='Certificate generation failed' WHERE id=?", (row['id'],))
        with self.connect() as db:
            counts = dict(db.execute('SELECT status, COUNT(*) FROM certificates WHERE job_id=? GROUP BY status', (job['id'],)).fetchall())
            succeeded = counts.get('succeeded', 0)
            failed = counts.get('failed', 0)
            status = 'completed' if not failed else ('partial_failure' if succeeded else 'failed')
            db.execute('UPDATE jobs SET status=?, completed_at=? WHERE id=?', (status, now(), job['id']))
        return True


def create_app(db_path=None, start_worker=True, renderer=render_certificate):
    store = Store(db_path or os.getenv('DATABASE_PATH', 'data/certificates.sqlite3'))
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                if store.process_one(renderer):
                    continue
            except Exception:
                log.exception('Worker error; unfinished jobs will resume after restart')
            stop.wait(0.25)

    @asynccontextmanager
    async def lifespan(app):
        thread = None
        if start_worker:
            # Single-process deployment: committed PDFs survive interruption.
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join()

    api = FastAPI(title='Bulk Certificate Generator', lifespan=lifespan)
    api.state.store = store

    def require_job(db, job_id):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Job not found')
        return dict(row)

    @api.post('/jobs', status_code=202)
    def create_job(body: JobRequest):
        job_id = str(uuid4())
        with store.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL)', (job_id, body.title, body.organization, body.issued_on.isoformat(), 'queued', now()))
            for position, raw in enumerate(body.recipients):
                try:
                    recipient = Recipient.model_validate(raw)
                    name, email, status, error = recipient.name, str(recipient.email), 'pending', None
                except ValidationError as exc:
                    name, email, status = None, None, 'failed'
                    error = json.dumps([{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()])
                db.execute('INSERT INTO certificates VALUES (?,?,?,?,?,?,?,NULL)', (str(uuid4()), job_id, position, name, email, status, error))
        return {'id': job_id, 'status': 'queued', 'status_url': f'/jobs/{job_id}', 'results_url': f'/jobs/{job_id}/certificates'}

    @api.get('/jobs/{job_id}')
    def job_status(job_id: str):
        with store.connect() as db:
            db.execute('BEGIN')  # Keep job state and counts in one read snapshot.
            job = require_job(db, job_id)
            counts = dict(db.execute('SELECT status, COUNT(*) FROM certificates WHERE job_id=? GROUP BY status', (job_id,)).fetchall())
        total = sum(counts.values())
        done = counts.get('succeeded', 0) + counts.get('failed', 0)
        return {**job, 'total': total, 'processed': done, 'succeeded': counts.get('succeeded', 0), 'failed': counts.get('failed', 0), 'pending': counts.get('pending', 0), 'progress_percent': round(done / total * 100, 2)}

    @api.get('/jobs/{job_id}/certificates')
    def results(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        with store.connect() as db:
            require_job(db, job_id)
            rows = db.execute('SELECT id, position, name, email, status, error FROM certificates WHERE job_id=? ORDER BY position LIMIT ? OFFSET ?', (job_id, limit, offset)).fetchall()
        return {'offset': offset, 'limit': limit, 'items': [{**dict(row), 'download_url': f"/jobs/{job_id}/certificates/{row['id']}" if row['status'] == 'succeeded' else None} for row in rows]}

    @api.get('/jobs/{job_id}/certificates/{certificate_id}')
    def download(job_id: str, certificate_id: str):
        with store.connect() as db:
            row = db.execute('SELECT status, pdf FROM certificates WHERE job_id=? AND id=?', (job_id, certificate_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Certificate not found')
        if row['status'] != 'succeeded':
            raise HTTPException(409, 'Certificate is not available')
        return Response(bytes(row['pdf']), media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="certificate-{certificate_id}.pdf"'})

    @api.post('/jobs/{job_id}/retry', status_code=202)
    def retry_failed(job_id: str):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = require_job(db, job_id)
            if job['status'] in ('queued', 'running'):
                raise HTTPException(409, 'Wait for the job to finish before retrying')
            # Validated rows have a name/email; invalid inputs need a new request.
            count = db.execute(
                "UPDATE certificates SET status='pending', error=NULL WHERE job_id=? "
                "AND status='failed' AND name IS NOT NULL AND email IS NOT NULL",
                (job_id,),
            ).rowcount
            if not count:
                raise HTTPException(409, 'No generation failures available to retry')
            db.execute("UPDATE jobs SET status='queued', completed_at=NULL WHERE id=?", (job_id,))
        return {'id': job_id, 'status': 'queued', 'retried': count, 'status_url': f'/jobs/{job_id}'}

    @api.get('/jobs/{job_id}/archive')
    def archive(job_id: str):
        # Disk-backed temporary storage avoids accumulating all PDFs in memory.
        output = tempfile.TemporaryFile()
        try:
            with store.connect() as db:
                db.execute('BEGIN')
                job = require_job(db, job_id)
                if job['status'] in ('queued', 'running'):
                    raise HTTPException(409, 'Wait for the job to finish before downloading the archive')
                manifest = []
                with ZipFile(output, 'w', compression=ZIP_DEFLATED) as zipped:
                    rows = db.execute('SELECT * FROM certificates WHERE job_id=? ORDER BY position', (job_id,))
                    for row in rows:
                        filename = f"certificate-{row['id']}.pdf" if row['status'] == 'succeeded' else None
                        manifest.append({key: row[key] for key in ('id', 'position', 'name', 'email', 'status', 'error')} | {'filename': filename})
                        if filename:
                            zipped.writestr(filename, row['pdf'])
                    zipped.writestr('manifest.json', json.dumps({'job': job, 'certificates': manifest}, indent=2))
            length = output.tell()
            output.seek(0)
        except BaseException:
            output.close()
            raise

        def chunks():
            try:
                while chunk := output.read(64 * 1024):
                    yield chunk
            finally:
                output.close()

        return StreamingResponse(chunks(), media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="certificates-{job["id"]}.zip"',
            'Content-Length': str(length),
        })

    return api
