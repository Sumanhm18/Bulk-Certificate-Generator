from io import BytesIO
import time

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.main import create_app, render_certificate


def payload(recipients=None):
    return {'title': 'Python Fundamentals', 'organization': 'Learning Academy', 'issued_on': '2026-10-07', 'recipients': recipients if recipients is not None else [{'name': 'Ada Lovelace', 'email': 'ada@example.com'}]}


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / 'test.sqlite', start_worker=False)
    with TestClient(app) as client:
        yield client, app.state.store


def test_create_progress_and_download(setup):
    client, store = setup
    response = client.post('/jobs', json=payload())
    assert response.status_code == 202
    job = response.json()['id']
    before = client.get(f'/jobs/{job}').json()
    assert (before['status'], before['pending'], before['processed']) == ('queued', 1, 0)
    item = client.get(f'/jobs/{job}/certificates').json()['items'][0]
    url = f"/jobs/{job}/certificates/{item['id']}"
    assert client.get(url).status_code == 409
    assert store.process_one(render_certificate)
    after = client.get(f'/jobs/{job}').json()
    assert (after['status'], after['succeeded'], after['progress_percent']) == ('completed', 1, 100)
    pdf = client.get(url)
    assert pdf.status_code == 200
    assert pdf.headers['content-type'] == 'application/pdf'
    text = PdfReader(BytesIO(pdf.content)).pages[0].extract_text()
    for expected in ['Ada Lovelace', 'Python Fundamentals', 'Learning Academy', '2026-10-07', item['id']]:
        assert expected in text


@pytest.mark.parametrize('change', [{'recipients': []}, {'issued_on': 'bad'}, {'title': '   '}, {'organization': None}, {'recipients': ['bad']}, {'recipients': [{}] * 10001}, {'extra': 1}])
def test_envelope_validation(setup, change):
    client, _ = setup
    assert client.post('/jobs', json={**payload(), **change}).status_code == 422


def test_invalid_recipients_are_isolated(setup):
    client, store = setup
    job = client.post('/jobs', json=payload([{'name': 'Ada', 'email': 'ada@example.com'}, {'name': '', 'email': 'bad'}, {}, {'name': '张伟', 'email': 'a@example.com'}])).json()['id']
    store.process_one(render_certificate)
    status = client.get(f'/jobs/{job}').json()
    assert (status['status'], status['succeeded'], status['failed'], status['processed']) == ('partial_failure', 1, 3, 4)
    items = client.get(f'/jobs/{job}/certificates').json()['items']
    assert items[1]['error'] and items[1]['download_url'] is None
    assert [x['position'] for x in items] == [0, 1, 2, 3]
    page = client.get(f'/jobs/{job}/certificates?offset=1&limit=1').json()
    assert len(page['items']) == 1 and page['items'][0]['position'] == 1


def test_generation_failure_continues(setup):
    client, store = setup
    job = client.post('/jobs', json=payload([{'name': 'Fail', 'email': 'f@example.com'}, {'name': 'Pass', 'email': 'p@example.com'}])).json()['id']
    observed = []

    def renderer(name, *args):
        observed.append(client.get(f'/jobs/{job}').json())
        if name == 'Fail':
            raise RuntimeError('private filesystem details')
        return render_certificate(name, *args)

    store.process_one(renderer)
    assert observed[0]['status'] == 'running'
    assert observed[1]['processed'] == 1
    assert client.get(f'/jobs/{job}').json()['succeeded'] == 1
    items = client.get(f'/jobs/{job}/certificates').json()['items']
    assert items[0]['error'] == 'Certificate generation failed'
    assert client.get(items[1]['download_url']).status_code == 200


def test_all_invalid_and_missing(setup):
    client, store = setup
    job = client.post('/jobs', json=payload([{}])).json()['id']
    store.process_one(render_certificate)
    assert client.get(f'/jobs/{job}').json()['status'] == 'failed'
    for path in ['/jobs/missing', '/jobs/missing/certificates', '/jobs/missing/certificates/missing']:
        assert client.get(path).status_code == 404
    assert client.get(f'/jobs/{job}/certificates?limit=0').status_code == 422


def test_worker_restart_recovery(tmp_path):
    path = tmp_path / 'db.sqlite'
    app = create_app(path, start_worker=False)
    with TestClient(app) as client:
        job = client.post('/jobs', json=payload()).json()['id']
    with app.state.store.connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
    resumed = create_app(path)
    with TestClient(resumed) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f'/jobs/{job}').json()
            if status['status'] == 'completed':
                break
            time.sleep(0.02)
        assert status['status'] == 'completed'
        item = client.get(f'/jobs/{job}/certificates').json()['items'][0]
        assert client.get(item['download_url']).content.startswith(b'%PDF')


def test_certificate_cannot_be_downloaded_under_another_job(setup):
    client, store = setup
    a = client.post('/jobs', json=payload()).json()['id']
    b = client.post('/jobs', json=payload()).json()['id']
    store.process_one(render_certificate)
    item = client.get(f'/jobs/{a}/certificates').json()['items'][0]
    assert client.get(f"/jobs/{b}/certificates/{item['id']}").status_code == 404


def test_archive_contains_successful_pdfs_and_failure_manifest(setup):
    import json
    from zipfile import ZipFile

    client, store = setup
    job = client.post('/jobs', json=payload([{'name': 'Ada', 'email': 'ada@example.com'}, {}])).json()['id']
    assert client.get(f'/jobs/{job}/archive').status_code == 409
    store.process_one(render_certificate)
    response = client.get(f'/jobs/{job}/archive')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert int(response.headers['content-length']) == len(response.content)
    with ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        success, failure = manifest['certificates']
        assert set(archive.namelist()) == {'manifest.json', success['filename']}
        assert archive.read(success['filename']) == client.get(f"/jobs/{job}/certificates/{success['id']}").content
        assert failure['status'] == 'failed' and failure['filename'] is None
        assert manifest['job']['status'] == 'partial_failure'
    assert client.get('/jobs/missing/archive').status_code == 404


def test_retry_only_generation_failures_preserves_successes(setup):
    client, store = setup
    job = client.post('/jobs', json=payload([{'name': 'Fail', 'email': 'f@example.com'}, {'name': 'Pass', 'email': 'p@example.com'}, {}])).json()['id']
    assert client.post(f'/jobs/{job}/retry').status_code == 409

    def renderer(name, *args):
        if name == 'Fail':
            raise RuntimeError('temporary failure')
        return render_certificate(name, *args)

    store.process_one(renderer)
    before = client.get(f'/jobs/{job}/certificates').json()['items']
    pdf = client.get(before[1]['download_url']).content
    response = client.post(f'/jobs/{job}/retry')
    assert response.status_code == 202 and response.json()['retried'] == 1
    assert client.post(f'/jobs/{job}/retry').status_code == 409
    status = client.get(f'/jobs/{job}').json()
    assert status['completed_at'] is None and status['pending'] == 1
    generated = []

    def recovered(name, *args):
        generated.append(name)
        return render_certificate(name, *args)

    store.process_one(recovered)
    assert generated == ['Fail']
    assert client.get(before[1]['download_url']).content == pdf
    after = client.get(f'/jobs/{job}/certificates').json()['items']
    assert after[0]['id'] == before[0]['id'] and after[0]['error'] is None
    assert after[2]['error'] == before[2]['error']
    assert client.get(f'/jobs/{job}').json()['status'] == 'partial_failure'
    assert client.post(f'/jobs/{job}/retry').status_code == 409
    assert client.post('/jobs/missing/retry').status_code == 404


def test_all_invalid_archive_is_manifest_only(setup):
    import json
    from zipfile import ZipFile

    client, store = setup
    job = client.post('/jobs', json=payload([{}])).json()['id']
    store.process_one(render_certificate)
    assert client.post(f'/jobs/{job}/retry').status_code == 409
    with ZipFile(BytesIO(client.get(f'/jobs/{job}/archive').content)) as archive:
        assert archive.namelist() == ['manifest.json']
        assert json.loads(archive.read('manifest.json'))['certificates'][0]['error']


def test_frontend_and_static_assets_are_served(setup):
    client, _ = setup
    response = client.get('/')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert 'id="create-form"' in response.text
    assert 'id="results-body"' in response.text
    for path, content_type in [('/static/app.js', 'javascript'), ('/static/style.css', 'text/css')]:
        asset = client.get(path)
        assert asset.status_code == 200 and content_type in asset.headers['content-type']
    assert client.get('/static/missing.js').status_code == 404
    assert '/jobs' in client.get('/openapi.json').json()['paths']


def test_job_history_is_paginated_newest_first(setup):
    client, store = setup
    assert client.get('/jobs').json() == {'offset': 0, 'limit': 10, 'total': 0, 'items': []}
    first = client.post('/jobs', json=payload()).json()['id']
    second = client.post('/jobs', json=payload([{}, {}])).json()['id']
    with store.connect() as db:
        db.execute("UPDATE jobs SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (first,))
        db.execute("UPDATE jobs SET created_at='2026-02-01T00:00:00+00:00' WHERE id=?", (second,))
    recent = client.get('/jobs?limit=1').json()
    assert recent['total'] == 2 and recent['items'][0]['id'] == second
    assert recent['items'][0]['total'] == 2
    assert 'pdf' not in recent['items'][0] and 'recipients' not in recent['items'][0]
    assert client.get('/jobs?offset=1&limit=1').json()['items'][0]['id'] == first
    assert client.get('/jobs?offset=2').json()['items'] == []
    store.process_one(render_certificate)
    assert client.get('/jobs?offset=1').json()['items'][0]['status'] == 'completed'
    for query in ['limit=0', 'limit=101', 'offset=-1']:
        assert client.get('/jobs?' + query).status_code == 422


def test_csv_template_available(setup):
    client, _ = setup
    response = client.get('/static/recipients-template.csv')
    assert response.status_code == 200
    assert response.text.startswith('name,email\n')
