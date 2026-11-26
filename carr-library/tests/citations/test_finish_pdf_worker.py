"""Independent process-boundary checks for the existing PDF review worker.

Crash, deadline and malformed-reply fixtures launch real controlled child
processes; they do not simulate PDFium faults or claim Windows execution.
"""
from hashlib import sha256
from io import BytesIO
import json
import subprocess
import sys
from time import monotonic

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.citations import pdf_review_attestation
from carr_library.citations.anchors import Identity, Locator, digest
from carr_library.citations.pdf_review_attestation import ReviewedPage, pdf_review_attestor
from carr_library.ingest import worker


@pytest.fixture
def pdf_bytes():
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(200, 200), invariant=1)
    canvas.drawString(20, 150, 'Known review source')
    canvas.save()
    return stream.getvalue()


def controlled_child(monkeypatch, code, *, completed=False):
    """Keep the real supervisor and process lifecycle, inject a known child."""
    original = subprocess.Popen
    processes = []

    def launch(command, *args, **kwargs):
        assert '-I' in command  # Real worker must ignore user-site and PYTHONPATH.
        child = original([sys.executable, '-I', '-c', code], *args, **kwargs)
        processes.append(child)
        if completed:
            child.wait(timeout=10)
        return child

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return processes


def test_real_pdf_fingerprint_matches_existing_primitive_without_parsing_in_parent(pdf_bytes, monkeypatch):
    expected = pdf_review_attestation.render_fingerprint(pdf_bytes, 1)

    def no_parent_render(*args, **kwargs):
        pytest.fail('The host must not invoke PDFium for review.')

    monkeypatch.setattr(pdf_review_attestation, 'render_fingerprint', no_parent_render)
    assert worker.isolated_render_fingerprint(pdf_bytes, 1) == expected


@pytest.mark.parametrize('physical', [None, '1', 1.0, True, 0, -1, 1001])
def test_invalid_physical_page_rejected_before_worker_spawn(pdf_bytes, monkeypatch, physical):
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Worker spawned for invalid page'))
    with pytest.raises(ValueError, match='Invalid PDF review page'):
        worker.isolated_render_fingerprint(pdf_bytes, physical)


@pytest.mark.parametrize('data', [None, '', b'', bytearray(b'%PDF-1.7')])
def test_invalid_bytes_rejected_before_worker_spawn(data, monkeypatch):
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Worker spawned for invalid data'))
    with pytest.raises(ValueError, match='Invalid PDF review bytes'):
        worker.isolated_render_fingerprint(data, 1)


def test_oversized_bytes_rejected_before_worker_spawn(monkeypatch):
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Worker spawned for oversized data'))
    with pytest.raises(ValueError, match='Invalid PDF review bytes'):
        worker.isolated_render_fingerprint(b'x' * (64 * 1024 * 1024 + 1), 1)


def test_real_malformed_pdf_and_absent_page_fail_closed(pdf_bytes):
    for data, physical in ((b'%PDF-1.7\nmalformed', 1), (pdf_bytes, 2)):
        with pytest.raises(ValueError, match='could not verify'):
            worker.isolated_render_fingerprint(data, physical)


def test_real_controlled_child_crash_cannot_verify_page(pdf_bytes, monkeypatch):
    processes = controlled_child(monkeypatch, 'import os; os._exit(37)')
    with pytest.raises(ValueError, match='could not verify'):
        worker.isolated_render_fingerprint(pdf_bytes, 1)
    assert len(processes) == 1 and processes[0].returncode == 37


def test_real_controlled_child_timeout_is_stopped_and_cannot_verify(pdf_bytes, monkeypatch):
    processes = controlled_child(monkeypatch, 'import time; time.sleep(30)')
    original = worker._invoke

    def short_deadline(message, *, timeout, **kwargs):
        return original(message, timeout=0.1, **kwargs)

    monkeypatch.setattr(worker, '_invoke', short_deadline)
    started = monotonic()
    with pytest.raises(ValueError, match='could not verify'):
        worker.isolated_render_fingerprint(pdf_bytes, 1)
    assert monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None


def test_cancelled_pdf_verification_stops_the_native_process(pdf_bytes, monkeypatch):
    processes = controlled_child(monkeypatch, 'import time; time.sleep(30)')
    started = monotonic()
    with pytest.raises(InterruptedError, match='cancelled'):
        worker.isolated_render_fingerprint(pdf_bytes, 1, cancelled=lambda: bool(processes))
    assert monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize('reply', [
    {},
    {'result': {}},
    {'result': {'renderer': '', 'fingerprint': 'a' * 64}},
    {'result': {'renderer': 'fixture', 'fingerprint': 'not-a-sha256'}},
    {'result': {'renderer': [], 'fingerprint': 'a' * 64}},
    {'result': {'renderer': 'fixture', 'fingerprint': 'a' * 64, 'extra': True}},
])
def test_real_controlled_child_malformed_reply_cannot_verify(pdf_bytes, monkeypatch, reply):
    code = 'import sys; sys.stdout.write(' + repr(json.dumps(reply)) + ')'
    processes = controlled_child(monkeypatch, code)
    with pytest.raises(ValueError, match='could not verify'):
        worker.isolated_render_fingerprint(pdf_bytes, 1)
    assert len(processes) == 1 and processes[0].returncode == 0


def test_attestor_does_not_accept_review_when_worker_crashes(pdf_bytes, monkeypatch):
    renderer, fingerprint = pdf_review_attestation.render_fingerprint(pdf_bytes, 1)
    identity = Identity('workspace', 'edition', sha256(pdf_bytes).hexdigest(), 'revision')
    raw_hash = digest('Known review source')
    review = ReviewedPage(identity, 1, raw_hash, fingerprint, renderer, 'fixture reviewer')
    processes = controlled_child(monkeypatch, 'import os; os._exit(37)')
    attest = pdf_review_attestor(identity.workspace, lambda _: pdf_bytes,
                                lambda *_: review, renderer=worker.isolated_render_fingerprint)
    assert attest(identity, Locator('physical_page', 1), raw_hash) is False
    assert len(processes) == 1 and processes[0].returncode == 37


def test_real_worker_rejects_changed_source_digest(pdf_bytes, tmp_path):
    source = tmp_path / 'source.pdf'
    source.write_bytes(pdf_bytes)
    result = worker._invoke(dict(action='render', source=str(source), sha256='0' * 64,
                                 physical=1, timeout=5), timeout=5)
    assert result['state'] == 'failed'
    assert 'digest changed' in result['message']


def test_exited_worker_cannot_bypass_stderr_limit(pdf_bytes, monkeypatch):
    reply = json.dumps({'result': {'renderer': 'fixture', 'fingerprint': 'a' * 64}})
    code = ("import sys; sys.stderr.buffer.write(b'x' * " + str(worker.MAX_LOG + 1)
            + '); sys.stderr.flush(); sys.stdout.write(' + repr(reply) + ')')
    # Exercise the legitimate race where a fast worker exits before the
    # supervisor's first poll. Limits must also be checked after completion.
    processes = controlled_child(monkeypatch, code, completed=True)
    with pytest.raises(ValueError):
        worker.isolated_render_fingerprint(pdf_bytes, 1)
    assert len(processes) == 1 and processes[0].returncode == 0
