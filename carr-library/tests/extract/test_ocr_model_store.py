"""Offline model activation, interruption recovery and exact-source boundaries.

Most cases use explicitly synthetic catalogue entries so failures are small and
deterministic. One case verifies and uses the actual installed official English
pack; no test downloads a model or treats a fixture as multilingual accuracy.
"""
from dataclasses import replace
import errno
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.extract import model_catalog as catalogue
from carr_library.extract import ocr_model_store as store_module


def _git_blob(raw):
    return sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


def _directory_link(link, target):
    if os.name == 'nt':
        # Exercise a real NTFS reparse point without relying on the optional
        # Windows developer-mode permission for symbolic links.
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    payloads = {
        'eng': b'synthetic English model\n' * 17,
        'ukr': 'synthetic Ukrainian model: Україна\n'.encode() * 19,
        'script/Latin': b'synthetic Latin script model\n' * 23,
        'osd': b'synthetic orientation model\n' * 13,
    }
    source = tmp_path / 'user-selected models'
    source.mkdir()
    rows = []
    for identifier, raw in payloads.items():
        relative = identifier + '.traineddata'
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        kind = 'script' if identifier.startswith('script/') else 'auxiliary' if identifier == 'osd' else 'language'
        rows.append(catalogue.Model(identifier, identifier, kind, 'fast',
                    catalogue.REVISIONS['fast'], len(raw), _git_blob(raw), relative))
    notice = b'Synthetic test model license; not a real upstream model.\n'
    monkeypatch.setattr(store_module, 'models',
                        lambda profile: tuple(replace(row, profile=profile,
                            revision=catalogue.REVISIONS[profile]) for row in rows))
    monkeypatch.setattr(store_module, 'catalogue_sha256', lambda: 'a' * 64)
    monkeypatch.setattr(store_module, 'license_bytes', lambda profile: notice)
    store = store_module.OcrModelStore(tmp_path / 'installed')
    profile = store.root / ('fast-' + catalogue.REVISIONS['fast'])
    return SimpleNamespace(store=store, source=source, profile=profile,
                           manifest=profile / 'manifest.json', payloads=payloads, notice=notice)


def test_official_catalogue_aliases_and_complete_licenses():
    for profile, count in (('fast', 163), ('best', 162)):
        rows = catalogue.models(profile)
        assert len(rows) == count
        table = {row.identifier: row for row in rows}
        assert 'eng' in table and 'script/Latin' in table and 'script/Cyrillic' in table
        assert table['osd'].kind == 'auxiliary'
        assert len(catalogue.license_bytes(profile)) > 10_000
        for row in rows:
            assert row.download_url.startswith(
                f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_{profile}/{catalogue.REVISIONS[profile]}/')
            if row.alias_of:
                target = table[row.alias_of]
                assert (row.size, row.blob_sha1) == (target.size, target.blob_sha1)


def test_actual_official_english_install_selection_and_inference(tmp_path, monkeypatch):
    from bootstrap.ocr_acceptance import prepared_ocr
    from carr_library.extract.ocr_process import run_ocr
    runtime, source = prepared_ocr(monkeypatch, ('eng',))
    model = next(row for row in catalogue.models('fast') if row.identifier == 'eng')
    original = (source / 'eng.traineddata').read_bytes()
    assert (len(original), _git_blob(original)) == (model.size, model.blob_sha1)
    store = store_module.OcrModelStore(tmp_path / 'official models')
    installed = store.install('fast', source, ('eng',))
    assert installed.installed == ('eng',) and installed.already_present == ()
    assert installed.bytes == model.size
    selected = store.select('fast', 'eng')
    assert dict(selected.model_sha256) == {'eng': sha256(original).hexdigest()}
    assert str(tmp_path) not in json.dumps(selected.fingerprint())
    selected.assert_unchanged()
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (1500, 350), 'white')
    ImageDraw.Draw(image).text((40, 90), 'Verified local model', fill='black',
                              font=ImageFont.load_default(size=70))
    picture = tmp_path / 'source.png'
    image.save(picture)
    recognized = run_ocr(runtime, [runtime.executable, str(picture), 'stdout',
        '--tessdata-dir', str(selected.directory), '-l', 'eng', '--psm', '6'],
        remaining=lambda: 20).stdout.decode('utf-8')
    assert recognized.strip() == 'Verified local model'
    repeated = store.install('fast', source, ('eng',))
    assert repeated.already_present == ('eng',) and repeated.bytes == 0
    assert (source / 'eng.traineddata').read_bytes() == original


def test_selection_is_exact_and_inventory_does_not_hash_model_bytes(synthetic, monkeypatch):
    s = synthetic
    s.store.install('fast', s.source, ('eng', 'script/Latin', 'osd'))
    selection = s.store.select('fast', 'script/Latin+eng', orientation=True)
    assert selection.languages == ('eng', 'osd', 'script/Latin')
    assert selection.fingerprint()['model_sha256'] == {
        name: sha256(s.payloads[name]).hexdigest() for name in selection.languages}
    with monkeypatch.context() as patch:
        patch.setattr(store_module, '_model_digest',
                      lambda *args, **kwargs: pytest.fail('Inventory must not hash model packs'))
        assert s.store.available('fast') == ('eng', 'osd', 'script/Latin')


@pytest.mark.parametrize('identifiers', ['eng', (), ('eng', 'eng'), ('ENG',), ('../eng',)])
def test_invalid_selection_cannot_create_or_rewrite_a_store(synthetic, identifiers):
    with pytest.raises(ValueError):
        synthetic.store.install('fast', synthetic.source, identifiers)
    assert not synthetic.store.root.exists()


def test_bad_later_model_keeps_prior_manifest_and_retry_adopts_verified_copy(synthetic):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    before = s.manifest.read_bytes()
    broken = s.source / 'script/Latin.traineddata'
    broken.write_bytes(b'X' * len(s.payloads['script/Latin']))
    with pytest.raises(store_module.ModelStoreError):
        s.store.install('fast', s.source, ('ukr', 'script/Latin'))
    assert s.manifest.read_bytes() == before and s.store.available('fast') == ('eng',)
    assert (s.profile / 'tessdata/ukr.traineddata').read_bytes() == s.payloads['ukr']
    broken.write_bytes(s.payloads['script/Latin'])
    report = s.store.install('fast', s.source, ('ukr', 'script/Latin'))
    assert report.bytes == len(s.payloads['script/Latin'])
    assert s.store.available('fast') == ('eng', 'script/Latin', 'ukr')


def test_cancel_between_publications_keeps_old_selection_and_retry_completes(synthetic):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    before = s.manifest.read_bytes()
    first = s.profile / 'tessdata/ukr.traineddata'
    with pytest.raises(InterruptedError):
        s.store.install('fast', s.source, ('ukr', 'script/Latin'), cancelled=first.exists)
    assert first.read_bytes() == s.payloads['ukr']
    assert s.manifest.read_bytes() == before and s.store.available('fast') == ('eng',)
    s.store.install('fast', s.source, ('ukr', 'script/Latin'))
    assert s.store.select('fast', 'eng+ukr+script/Latin').languages == ('eng', 'script/Latin', 'ukr')


def test_cancel_before_start_creates_no_store(synthetic):
    with pytest.raises(InterruptedError):
        synthetic.store.install('fast', synthetic.source, ('eng',), cancelled=lambda: True)
    assert not synthetic.store.root.exists()


def test_low_disk_space_keeps_prior_manifest_and_is_retryable(synthetic, monkeypatch):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    before = s.manifest.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(store_module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
        with pytest.raises(store_module.ModelStoreError, match='free space'):
            s.store.install('fast', s.source, ('ukr',))
    assert s.manifest.read_bytes() == before
    assert not (s.profile / 'tessdata/ukr.traineddata').exists()
    s.store.install('fast', s.source, ('ukr',))
    assert s.store.available('fast') == ('eng', 'ukr')


def test_partial_license_write_leaves_no_unrecoverable_install(synthetic, monkeypatch):
    s = synthetic
    original_open = Path.open

    class DiskFullWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, raw):
            if raw == s.notice:
                self.stream.write(raw[:len(raw) // 2])
                raise OSError(errno.ENOSPC, 'injected license write exhaustion')
            return self.stream.write(raw)

    def failing_open(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return DiskFullWriter(stream) if mode == 'xb' else stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', failing_open)
        with pytest.raises(OSError, match='license write exhaustion'):
            s.store.install('fast', s.source, ('eng',))
    assert s.store.available('fast') == ()
    s.store.install('fast', s.source, ('eng',))
    assert (s.profile / 'LICENSE').read_bytes() == s.notice
    assert s.store.select('fast', 'eng').languages == ('eng',)


@pytest.mark.parametrize('target_kind', ['model', 'license'])
def test_interrupted_link_publication_recovers_only_known_verified_twin(synthetic, target_kind):
    s = synthetic
    if target_kind == 'model':
        target = s.profile / 'tessdata/script/Latin.traineddata'
        raw, selection = s.payloads['script/Latin'], ('script/Latin',)
    else:
        target = s.profile / 'LICENSE'
        raw, selection = s.notice, ('eng',)
    target.parent.mkdir(parents=True)
    temporary = target.with_name('.model-' + 'b' * 32 + '.tmp')
    temporary.write_bytes(raw)
    os.link(temporary, target)
    assert target.stat().st_nlink == 2
    s.store.install('fast', s.source, selection)
    assert not temporary.exists() and target.stat().st_nlink == 1
    assert s.store.select('fast', '+'.join(selection)).languages == selection


@pytest.mark.parametrize('extra_twin', [False, True])
def test_external_hardlinks_are_never_removed_or_accepted(synthetic, tmp_path, extra_twin):
    s = synthetic
    target = s.profile / 'tessdata/eng.traineddata'
    target.parent.mkdir(parents=True)
    target.write_bytes(s.payloads['eng'])
    outside = tmp_path / 'user-owned-copy'
    os.link(target, outside)
    if extra_twin:
        os.link(target, target.with_name('.model-' + 'c' * 32 + '.tmp'))
    with pytest.raises(store_module.ModelStoreError, match='link'):
        s.store.install('fast', s.source, ('eng',))
    assert outside.read_bytes() == s.payloads['eng']
    assert s.store.available('fast') == ()


@pytest.mark.parametrize('kind', ['source-file', 'source-directory', 'store-directory'])
def test_linked_paths_do_not_escape_selected_roots(synthetic, tmp_path, kind):
    s = synthetic
    if kind == 'source-file':
        source_file = s.source / 'eng.traineddata'
        original = tmp_path / 'outside-model'
        source_file.rename(original)
        if os.name == 'nt':
            os.link(original, source_file)
        else:
            source_file.symlink_to(original)
    elif kind == 'source-directory':
        original = tmp_path / 'outside-source'
        s.source.rename(original)
        _directory_link(s.source, original)
    else:
        original = tmp_path / 'outside-store'
        original.mkdir()
        _directory_link(s.store.root, original)
    with pytest.raises(store_module.ModelStoreError):
        s.store.install('fast', s.source, ('eng',))
    assert not s.manifest.exists()


def test_source_changed_while_streaming_is_not_activated(synthetic):
    s = synthetic
    source = s.source / 'eng.traineddata'
    replaced = False

    def progress(*args):
        nonlocal replaced
        if not replaced:
            replaced = True
            if os.name == 'nt':
                # Windows can disallow renaming an open source handle. A
                # same-size write still exercises concurrent input mutation.
                raw = bytearray(s.payloads['eng'])
                raw[-1] ^= 1
                source.write_bytes(raw)
            else:
                source.rename(source.with_suffix('.original'))
                source.write_bytes(s.payloads['eng'])

    with pytest.raises(store_module.ModelStoreError, match='changed'):
        s.store.install('fast', s.source, ('eng',), progress=progress)
    assert s.store.available('fast') == ()
    assert not (s.profile / 'tessdata/eng.traineddata').exists()


@pytest.mark.parametrize('kind', ['model', 'license', 'manifest'])
def test_selected_identity_detects_tampering(synthetic, kind):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    selected = s.store.select('fast', 'eng')
    if kind == 'manifest':
        data = json.loads(s.manifest.read_bytes())
        data['models']['eng']['sha256'] = '0' * 64
        s.manifest.write_text(json.dumps(data))
    else:
        target = s.profile / ('tessdata/eng.traineddata' if kind == 'model' else 'LICENSE')
        raw = bytearray(target.read_bytes())
        raw[-1] ^= 1
        target.write_bytes(raw)
    with pytest.raises(store_module.ModelStoreError):
        selected.assert_unchanged()


@pytest.mark.parametrize('malformed', ['duplicate-key', 'wrong-profile', 'wrong-size', 'unknown-model'])
def test_malformed_manifest_is_rejected_without_repairing_unknown_state(synthetic, malformed):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    data = json.loads(s.manifest.read_bytes())
    if malformed == 'duplicate-key':
        raw = json.dumps(data).replace('"schema": 1', '"schema": 1, "schema": 1').encode()
    else:
        if malformed == 'wrong-profile':
            data['profile'] = 'best'
        elif malformed == 'wrong-size':
            data['models']['eng']['size'] = True
        else:
            data['models']['../eng'] = data['models'].pop('eng')
        raw = json.dumps(data).encode()
    s.manifest.write_bytes(raw)
    with pytest.raises(ValueError):
        s.store.available('fast')
    with pytest.raises(ValueError):
        s.store.install('fast', s.source, ('eng',))
    assert s.manifest.read_bytes() == raw


def test_manifest_publication_failure_preserves_old_selection_and_retry(synthetic, monkeypatch):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    before = s.manifest.read_bytes()
    original_replace = os.replace

    def failed_manifest(source, target, *args, **kwargs):
        if Path(target) == s.manifest:
            raise OSError(errno.ENOSPC, 'injected manifest activation failure')
        return original_replace(source, target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, 'replace', failed_manifest)
        with pytest.raises(OSError, match='manifest activation failure'):
            s.store.install('fast', s.source, ('ukr',))
    assert s.manifest.read_bytes() == before and s.store.available('fast') == ('eng',)
    assert not tuple(s.profile.glob('.manifest-*.tmp'))
    s.store.install('fast', s.source, ('ukr',))
    assert s.store.available('fast') == ('eng', 'ukr')


def test_existing_target_appearing_at_publication_is_never_overwritten(synthetic, monkeypatch):
    s = synthetic
    target = s.profile / 'tessdata/eng.traineddata'
    original_link = os.link
    other_bytes = b'preexisting user file; must not overwrite'

    def raced_link(source, destination, *args, **kwargs):
        if Path(destination) == target:
            target.write_bytes(other_bytes)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(store_module.os, 'link', raced_link)
    with pytest.raises(FileExistsError):
        s.store.install('fast', s.source, ('eng',))
    assert target.read_bytes() == other_bytes and s.store.available('fast') == ()


def test_manifest_change_during_selection_is_not_returned_as_current(synthetic):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    replaced = False

    def replace_manifest():
        nonlocal replaced
        if not replaced:
            replaced = True
            data = json.loads(s.manifest.read_bytes())
            data['models'] = {}
            s.manifest.write_text(json.dumps(data))

    with pytest.raises(store_module.ModelStoreError):
        s.store.select('fast', 'eng', remaining=replace_manifest)


def test_manifest_change_during_install_is_not_silently_overwritten(synthetic):
    s = synthetic
    s.store.install('fast', s.source, ('eng',))
    replacement = b'{"unexpected": "concurrent manifest replacement"}'

    def replace_manifest(*args):
        s.manifest.write_bytes(replacement)

    with pytest.raises(ValueError):
        s.store.install('fast', s.source, ('ukr',), progress=replace_manifest)
    assert s.manifest.read_bytes() == replacement
