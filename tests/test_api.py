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
