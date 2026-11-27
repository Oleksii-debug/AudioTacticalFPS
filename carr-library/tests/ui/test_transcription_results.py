"""Completed-result previews verify real bytes without editing outputs or sources."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path

import pytest

from carr_library.ui.transcription_results import (MAX_PREVIEW_BYTES, MAX_TRANSCRIPT_BYTES,
                                                 read_transcript_preview)


def complete_result(tmp_path, data):
    identity = sha256(b'synthetic-source-model-settings').hexdigest()
    directory = tmp_path / ('transcript-' + identity)
    directory.mkdir()
    path = directory / 'transcript.txt'
    path.write_bytes(data)
    receipt = {'schema_version': 1, 'status': 'complete', 'identity': identity,
        'files': {'txt': str(path)},
        'outputs': {'transcript.txt': {'bytes': len(data), 'sha256': sha256(data).hexdigest()}}}
    return path, receipt


def test_unicode_preview_preserves_exact_text_and_files(tmp_path):
    content = '  Синтетичний текст — Україна.\r\nДругий рядок 👩🏽\u200d💻 e\u0301.\n'
    path, receipt = complete_result(tmp_path, content.encode('utf-8'))
    before = deepcopy(receipt)
    result = read_transcript_preview(receipt)
    assert result.text == content
    assert result.output_folder == path.parent
    assert result.total_bytes == result.displayed_bytes == len(content.encode('utf-8'))
    assert not result.truncated
    assert 'Complete text' in result.summary
    assert path.read_bytes() == content.encode('utf-8')
    assert receipt == before


def test_long_result_hashes_all_bytes_and_does_not_split_unicode_preview(tmp_path):
    content = 'АБ' * (MAX_PREVIEW_BYTES + 1)
    path, receipt = complete_result(tmp_path, content.encode('utf-8'))
    result = read_transcript_preview(receipt, max_preview_bytes=7)
    assert result.text == 'АБА'
    assert result.displayed_bytes == 6
    assert result.truncated and 'Preview truncated' in result.summary
    assert str(len(content.encode('utf-8'))) in result.summary
    assert path.read_bytes() == content.encode('utf-8')


def test_empty_completed_transcript_has_clear_empty_status(tmp_path):
    _path, receipt = complete_result(tmp_path, b'')
    result = read_transcript_preview(receipt)
    assert result.text == '' and result.total_bytes == 0 and not result.truncated
    assert 'Empty transcript' in result.summary


def test_same_size_edit_with_original_mtime_is_not_silently_trusted(tmp_path):
    path, receipt = complete_result(tmp_path, b'Original transcript')
    previous = path.stat()
    changed = b'x' * previous.st_size
    path.write_bytes(changed)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with pytest.raises(ValueError, match='changed after completion'):
        read_transcript_preview(receipt)
    assert path.read_bytes() == changed


def test_tail_edit_beyond_preview_is_rejected(tmp_path):
    path, receipt = complete_result(tmp_path, b'a' * (MAX_PREVIEW_BYTES + 100))
    changed = path.read_bytes()[:-1] + b'x'
    path.write_bytes(changed)
    with pytest.raises(ValueError, match='changed after completion'):
        read_transcript_preview(receipt)
    assert path.read_bytes() == changed


@pytest.mark.parametrize('tail,message', [(b'\xff', 'not valid UTF-8'), (b'\x00', 'contains NUL')])
def test_invalid_text_beyond_preview_is_rejected_even_if_receipt_hash_matches(tmp_path, tail, message):
    path, receipt = complete_result(tmp_path, b'a' * (MAX_PREVIEW_BYTES + 100) + tail)
    with pytest.raises(ValueError, match=message):
        read_transcript_preview(receipt)
    assert path.read_bytes().endswith(tail)


def test_cancel_before_verification_performs_no_file_access(tmp_path, monkeypatch):
    _path, receipt = complete_result(tmp_path, b'text')
    def forbidden(*args, **kwargs):
        raise AssertionError('No file should be opened after cancellation')
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(InterruptedError, match='cancelled'):
        read_transcript_preview(receipt, cancelled=lambda: True)


def test_streaming_cancellation_retains_complete_output(tmp_path):
    data = b'a' * (3 * 1024 * 1024)
    path, receipt = complete_result(tmp_path, data)
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) >= 3
    with pytest.raises(InterruptedError, match='cancelled'):
        read_transcript_preview(receipt, cancelled=cancelled)
    assert len(checks) == 3
    assert path.read_bytes() == data


def test_changed_file_during_hashing_never_publishes_a_preview(tmp_path):
    path, receipt = complete_result(tmp_path, b'Original completed text')
    previous = path.stat()
    checks = []
    def change_after_read():
        checks.append(True)
        if len(checks) == 3:
            path.write_bytes(b'X' * previous.st_size)
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 10_000_000))
        return False
    with pytest.raises(ValueError, match='changed after completion'):
        read_transcript_preview(receipt, cancelled=change_after_read)
    assert path.read_bytes() == b'X' * previous.st_size


@pytest.mark.parametrize('change', [
    lambda r: r.update(status='running'),
    lambda r: r.update(identity='../unrelated'),
    lambda r: r['files'].pop('txt'),
    lambda r: r['outputs']['transcript.txt'].update(sha256='wrong'),
    lambda r: r['outputs']['transcript.txt'].update(bytes=True),
    lambda r: r['outputs']['transcript.txt'].update(bytes=MAX_TRANSCRIPT_BYTES + 1),
    lambda r: r['outputs']['transcript.txt'].update(unreviewed=True),
    lambda r: r['files'].update(txt='transcript.txt'),
])
def test_invalid_or_unbounded_receipts_fail_before_opening_text(tmp_path, monkeypatch, change):
    _path, receipt = complete_result(tmp_path, b'Safe text')
    change(receipt)
    def forbidden(*args, **kwargs):
        raise AssertionError('Malformed receipts must fail before opening any file')
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(ValueError):
        read_transcript_preview(receipt)


def test_receipt_cannot_repoint_to_another_existing_transcript(tmp_path):
    path, receipt = complete_result(tmp_path, b'Original text')
    wrong = tmp_path / 'another-transcript.txt'
    wrong.write_bytes(path.read_bytes())
    receipt['files']['txt'] = str(wrong)
    with pytest.raises(ValueError, match='does not match'):
        read_transcript_preview(receipt)
    assert wrong.read_bytes() == b'Original text'


@pytest.mark.parametrize('limit', [0, -1, MAX_PREVIEW_BYTES + 1, True])
def test_preview_budget_is_bounded(tmp_path, limit):
    _path, receipt = complete_result(tmp_path, b'Safe text')
    with pytest.raises(ValueError, match='bounded'):
        read_transcript_preview(receipt, max_preview_bytes=limit)
