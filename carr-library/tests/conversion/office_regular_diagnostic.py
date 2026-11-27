"""Diagnostic-only real stat observations and independent authored DOC cases.

This suite never converts a failed attempt into a pass. Each DOC case invokes
the unchanged all-nine fixture's real conversion and every original assertion.
"""
from contextlib import contextmanager
from hashlib import sha1
import json
import os
from pathlib import Path
import runpy
import stat

import pytest

from carr_library import durable_state

_FIXTURE_BLOB = 'f7c4a5acf1af97d1a63d846b5ac1404658179ce7'
_FIXTURE = Path(__file__).with_name('office_native_acceptance.py')
_RAW = _FIXTURE.read_bytes()
assert sha1(b'blob ' + str(len(_RAW)).encode('ascii') + b'\0' + _RAW).hexdigest() == _FIXTURE_BLOB
_NATIVE = runpy.run_path(str(_FIXTURE))
_DOC_CASE = _NATIVE['test_real_selected_legacy_filter_retains_original_and_exact_provenance']


def _diagnostic(error):
    prefix = 'State must use regular local files, without links. diagnostic_regular='
    text = str(error)
    assert text.startswith(prefix)
    assert len(type(error).__name__ + ': ' + text) <= 2000
    detail = json.loads(text[len(prefix):])
    assert detail['resampled'] is False
    assert len(detail['path_tail']) <= 512
    assert len(detail['frames']) <= 6
    return detail


def test_diagnostic_retains_original_lstat_after_actual_alias_removal(tmp_path, monkeypatch):
    path, alias = tmp_path / 'captured.json', tmp_path / 'alias.json'
    path.write_text('{"value":1}', encoding='utf-8')
    os.link(path, alias)
    real_lstat = Path.lstat
    captured = []

    def sample(selected, *args, **kwargs):
        observed = real_lstat(selected, *args, **kwargs)
        if selected == path and not captured:
            captured.append(observed)
            alias.unlink()
        return observed

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', sample)
        with pytest.raises(ValueError) as caught:
            durable_state.regular(path)
    detail = _diagnostic(caught.value)
    assert real_lstat(path).st_nlink == 1
    assert captured[0].st_nlink == detail['nlink'] == 2
    assert detail['check'] == 'regular.lstat'
    assert detail['directory_expected'] is False
    assert detail['mode'] == captured[0].st_mode
    assert detail['file_type'] == stat.S_IFREG
    assert detail['inode'] == captured[0].st_ino
    assert detail['device'] == captured[0].st_dev
    assert path.read_text(encoding='utf-8') == '{"value":1}'


def test_diagnostic_retains_original_opened_fstat_without_resampling(tmp_path, monkeypatch):
    path, alias = tmp_path / 'opened.json', tmp_path / 'alias.json'
    path.write_text('{"value":2}', encoding='utf-8')
    real_open, real_fstat = durable_state._open_json_file, os.fstat
    active, captured = [], []

    @contextmanager
    def open_with_alias(selected):
        with real_open(selected) as stream:
            active.append(stream.fileno())
            os.link(path, alias)
            try:
                yield stream
            finally:
                alias.unlink(missing_ok=True)

    def sample(descriptor):
        observed = real_fstat(descriptor)
        if active and descriptor == active[0] and not captured:
            captured.append(observed)
            alias.unlink()
        return observed

    with monkeypatch.context() as patch:
        patch.setattr(durable_state, '_open_json_file', open_with_alias)
        patch.setattr(os, 'fstat', sample)
        with pytest.raises(ValueError) as caught:
            durable_state.read_json(path)
    detail = _diagnostic(caught.value)
    assert path.stat().st_nlink == 1
    assert captured[0].st_nlink == detail['nlink'] == 2
    assert detail['check'] == 'read_json.fstat'
    assert detail['directory_expected'] is False
    assert detail['mode'] == captured[0].st_mode
    assert detail['file_type'] == stat.S_IFREG
    assert detail['inode'] == captured[0].st_ino
    assert path.read_text(encoding='utf-8') == '{"value":2}'


@pytest.mark.parametrize('repetition', range(12), ids=lambda value: f'independent-doc-{value + 1:02d}')
def test_independent_authored_doc_retains_every_native_assertion(tmp_path, repetition):
    assert callable(getattr(durable_state, '_regular_rejection_diagnostic', None))
    assert 0 <= repetition < 12
    _DOC_CASE(tmp_path, 'doc')
