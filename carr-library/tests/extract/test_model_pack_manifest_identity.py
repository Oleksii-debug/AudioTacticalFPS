"""Retained canonical OCR manifest-admission regressions on the current owner.

The current installer may refuse a changed source snapshot. It must never treat
a separately rehashed or malformed manifest as the previously approved pack.
Synthetic bytes test integrity only; actual OCR quality is covered separately.
"""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.extract import language_manager as packs


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


def prepared(tmp_path):
    source = tmp_path / 'Reviewed pack'
    source.mkdir()
    original = b'Original synthetic OCR model'
    (source / 'eng.traineddata').write_bytes(original)
    manifest = {'schema': 1, 'upstream': 'synthetic-reviewed-revision',
                'license': 'Synthetic fixture, not an OCR model.',
                'models': {'eng': {'size': len(original),
                                   'sha256': sha256(original).hexdigest()}}}
    raw = encoded(manifest)
    (source / 'models.json').write_bytes(raw)
    return source, manifest, raw, original


def test_manifest_replacement_cannot_install_an_unapproved_model(tmp_path, monkeypatch):
    source, approved, raw, original = prepared(tmp_path)
    replacement_bytes = b'X' * len(original)
    replacement = {**approved, 'upstream': 'unapproved-replacement',
        'models': {'eng': {'size': len(replacement_bytes),
                          'sha256': sha256(replacement_bytes).hexdigest()}}}
    actual_read = packs._read
    replaced = False

    def read_then_replace(path, *args, **options):
        nonlocal replaced
        result = actual_read(path, *args, **options)
        if Path(path) == source / 'models.json' and not replaced:
            assert result[1] == sha256(raw).hexdigest() and result[2] == raw
            replaced = True
            (source / 'eng.traineddata').write_bytes(replacement_bytes)
            pending = source / 'replacement.json'
            pending.write_bytes(encoded(replacement))
            os.replace(pending, source / 'models.json')
        return result

    monkeypatch.setattr(packs, '_read', read_then_replace)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError):
        packs.install_offline_pack(source, destination,
            expected_manifest_sha256=sha256(raw).hexdigest())
    assert replaced
    assert (source / 'models.json').read_bytes() == encoded(replacement)
    assert (source / 'eng.traineddata').read_bytes() == replacement_bytes
    assert not destination.exists()
    assert not list(tmp_path.glob('.ocr-install-*'))


@pytest.mark.parametrize('malformation', ['duplicate-key', 'non-finite'])
def test_approved_checksum_does_not_relax_strict_json(tmp_path, malformation):
    source, manifest, raw, original = prepared(tmp_path)
    if malformation == 'duplicate-key':
        raw = b'{"models":{},' + raw[1:]
    else:
        manifest['license'] = float('nan')
        raw = encoded(manifest)
    (source / 'models.json').write_bytes(raw)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError):
        packs.install_offline_pack(source, destination,
            expected_manifest_sha256=sha256(raw).hexdigest())
    assert not destination.exists()
    assert (source / 'models.json').read_bytes() == raw
    assert (source / 'eng.traineddata').read_bytes() == original
    assert not list(tmp_path.glob('.ocr-install-*'))


def test_oversized_manifest_cannot_substitute_the_empty_content_checksum(tmp_path):
    source, manifest, _, original = prepared(tmp_path)
    manifest['upstream'] = 'x' * (2 * 1024 * 1024)
    raw = encoded(manifest)
    (source / 'models.json').write_bytes(raw)
    destination = tmp_path / 'installed'
    with pytest.raises(ValueError):
        packs.install_offline_pack(source, destination,
            expected_manifest_sha256=sha256(b'').hexdigest())
    assert not destination.exists()
    assert (source / 'models.json').read_bytes() == raw
    assert (source / 'eng.traineddata').read_bytes() == original
    assert not list(tmp_path.glob('.ocr-install-*'))
