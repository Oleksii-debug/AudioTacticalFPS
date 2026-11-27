"""Real managed-model batch import, review, durable resume and tamper refusal.

Requires the prepared real OCR runtime and pinned official Fast eng/osd files.
The source and installed Windows acceptance jobs prepare these prerequisites;
missing or different prerequisites fail explicitly instead of skipping evidence.
Only temporary synthetic documents, model copies and private libraries are used.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from PIL import Image, ImageDraw, ImageFont
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import LibraryApp
from carr_library.extract.model_catalog import REVISIONS
from carr_library.extract.ocr_model_store import OcrModelStore
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import load_request


GROUND_TRUTH = 'Managed import evidence'


@pytest.fixture
def managed_import(tmp_path, monkeypatch):
    runtime = discover_ocr_runtime('eng')
    assert runtime is not None, 'Prepare the real OCR engine for managed import acceptance.'
    listing = subprocess.run(runtime.command([runtime.executable, '--list-langs']),
        check=True, capture_output=True, timeout=15, cwd=runtime.root,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.decode('utf-8')
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', listing, re.MULTILINE)
    assert match is not None, 'The prepared OCR engine must identify its model directory.'
    model_source = runtime.tessdata if runtime.tessdata is not None else Path(match[1])
    runtime.assert_unchanged()

    model_root = tmp_path / 'managed models Україна'
    monkeypatch.setenv('CARR_OCR_MODELS_DIR', str(model_root))
    store = OcrModelStore()
    # The production installer verifies exact upstream blob identities and
    # copies these files. A substituted local model is not test evidence.
    report = store.install('fast', model_source, ('eng', 'osd'))
    assert report.installed == ('eng', 'osd') and report.bytes > 0
    selected = store.select('fast', 'eng')

    source = tmp_path / 'Синтетичний managed scan.pdf'
    scan = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(scan).text((30, 70), GROUND_TRUTH,
                             font=ImageFont.load_default(size=72), fill='black')
    canvas = Canvas(str(source), pagesize=(612, 792))
    canvas.drawImage(ImageReader(scan), 50, 500, 500, 125)
    canvas.save()
    original = source.read_bytes()
    app = LibraryApp(tmp_path / 'private managed library')
    workspace = app.create_workspace('Managed OCR synthetic acceptance')
    config = Config(languages='eng', model_profile='fast', dpi=200, timeout=60)
    planned = app.plan_import_batch(workspace, [source], config=config)
    assert planned['config'] == asdict(config)
    return app, workspace, source, original, selected, config, planned


def checkpoint(app, job):
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        return db.execute('SELECT payload,digest FROM checkpoint WHERE job=? ORDER BY ordinal',
                          (job,)).fetchall()


def quarantine(managed_import):
    app, workspace, source, original, selected, config, planned = managed_import
    pending_batch = app.run_import_batch(workspace, planned['batch_id'], timeout=120)
    assert pending_batch['counts']['quarantined'] == 1, pending_batch['rows'][0]['result']
    assert pending_batch['counts']['complete'] == 0
    result = pending_batch['rows'][0]['result']
    assert result['isolated'] is True and not result['verified']
    assert app.browse(workspace) == ()
    request_id = planned['rows'][0]['request_id']
    request = load_request(app.root, request_id)
    assert request['config'] == asdict(config)
    assert request['state'] == 'quarantined'
    records = checkpoint(app, result['id'])
    assert len(records) == 1
    payload = json.loads(records[0][0])
    assert payload['status'] == 'review_required'
    assert payload['raw_text'].strip() == GROUND_TRUTH, payload
    assert payload['provenance']['config']['model_profile'] == 'fast'
    assert payload['provenance']['ocr_runtime']['managed_models'] == selected.fingerprint()
    assert payload['provenance']['ocr_runtime']['managed_models']['profile'] == 'fast'
    assert payload['provenance']['ocr_runtime']['managed_models']['revision'] == REVISIONS['fast']
    assert payload['physical_index'] == 1 and payload['printed_label'] is None
    assert payload['printed_label_verified'] is False
    assert payload['provenance']['asset_sha256'] == sha256(original).hexdigest()
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        settings = json.loads(db.execute('SELECT config FROM job WHERE id=?', (result['id'],)).fetchone()[0])
    assert settings['config'] == asdict(config)
    assert settings['runtime_identity']['ocr']['managed_models'] == selected.fingerprint()
    assert source.read_bytes() == original
    return request_id, result['id'], records


def test_real_managed_batch_quarantines_reopens_reviews_and_resumes_without_source_changes(managed_import):
    app, workspace, source, original, selected, config, planned = managed_import
    request_id, job, records = quarantine(managed_import)

    reopened = LibraryApp(app.root)
    restored = reopened.import_batch(workspace, planned['batch_id'])
    assert restored['config'] == asdict(config)
    assert restored['counts']['quarantined'] == 1
    assert restored['rows'][0]['request_id'] == request_id
    # Starting a saved batch is not permission to approve a quarantined page.
    assert reopened.run_import_batch(workspace, planned['batch_id'])['counts']['quarantined'] == 1
    assert reopened.browse(workspace) == ()
    pending = reopened.pending_review(workspace, request_id)
    # This independent expected text was used to create the synthetic scan;
    # model agreement alone does not grant review authority.
    assert pending['raw_text'].strip() == GROUND_TRUTH and pending['physical'] == 1
    with pytest.raises(ValueError, match='must be confirmed'):
        reopened.confirm_import_review(workspace, request_id, 'Synthetic fixture reviewer',
                                       expected_checksum=pending['checksum'])
    reopened.confirm_import_review(workspace, request_id, 'Synthetic fixture reviewer',
        confirmed=True, expected_checksum=pending['checksum'])
    completed = reopened.run_import_batch(workspace, planned['batch_id'], retry_failed=True, timeout=120)
    assert completed['counts']['complete'] == 1, completed['rows'][0]['result']
    assert completed['counts']['quarantined'] == completed['counts']['failed'] == 0
    assert completed['config'] == asdict(config)
    assert completed['rows'][0]['result']['id'] == job
    assert completed['rows'][0]['result']['isolated'] is True
    for key in ('row_id', 'work_id', 'edition_id', 'request_id'):
        assert completed['rows'][0][key] == planned['rows'][0][key]
    assert reopened.pending_review(workspace, request_id) is None
    hit, = reopened.search(workspace, 'Managed')
    assert hit.verified and reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'Managed'
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM source_page_review WHERE active=1').fetchone()[0] == 1
        assert [tuple(row) for row in db.execute('SELECT physical,printed,verified FROM page')] == [(1, None, 0)]
    assert checkpoint(reopened, job) == records
    assert source.read_bytes() == original
    assert reopened.assets.resolve(sha256(original).hexdigest()).read_bytes() == original


def test_tampered_managed_model_blocks_reviewed_resume_without_publication(managed_import):
    app, workspace, source, original, selected, config, planned = managed_import
    request_id, job, records = quarantine(managed_import)
    pending = app.pending_review(workspace, request_id)
    assert pending['raw_text'].strip() == GROUND_TRUTH
    app.confirm_import_review(workspace, request_id, 'Synthetic fixture reviewer',
        confirmed=True, expected_checksum=pending['checksum'])

    model = selected.directory / 'eng.traineddata'
    original_size = model.stat().st_size
    with model.open('r+b') as stream:
        stream.seek(-1, os.SEEK_END)
        changed = bytes((stream.read(1)[0] ^ 1,))
        stream.seek(-1, os.SEEK_END)
        stream.write(changed)
    assert model.stat().st_size == original_size

    reopened = LibraryApp(app.root)
    refused = reopened.run_import_batch(workspace, planned['batch_id'], retry_failed=True, timeout=120)
    assert refused['counts']['failed'] == 1, refused['rows'][0]['result']
    assert refused['counts']['complete'] == 0
    assert refused['rows'][0]['result']['isolated'] is True
    assert refused['config'] == asdict(config)
    assert 'fingerprint' in refused['rows'][0]['message'].lower()
    assert reopened.browse(workspace) == () and reopened.search(workspace, 'Managed') == ()
    with reopened.read(workspace) as db:
        for table in ('revision', 'active', 'page', 'catalog_edition', 'source_page_review'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    assert checkpoint(reopened, job) == records
    assert source.read_bytes() == original
    assert reopened.assets.resolve(sha256(original).hexdigest()).read_bytes() == original
