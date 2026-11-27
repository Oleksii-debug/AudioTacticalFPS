"""Actual reviewed artwork retention and passive typed-backup recovery."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioError, metadata
from carr_library.audio import metadata_review as saved
from carr_library.audio.metadata import MetadataChanges, review_metadata
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import create_backup, restore_backup


_helpers = runpy.run_path(str(Path(__file__).with_name('test_metadata.py')))
actual = _helpers['actual']
encoded = _helpers['encoded']


def reviewed(actual, tmp_path, mode='replace'):
    runtime, _pcm, output, cover = actual
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Reviewed audio metadata backup')
    source = encoded(actual, '.mp3')
    changes = MetadataChanges({'title': 'Рецензія 😀', 'artist': 'Explicit author'},
        mode, str(cover) if mode == 'replace' else None)
    plan = review_metadata(runtime, source, output / 'fresh-reviewed.mp3', changes)
    return app, runtime, source, cover, plan


@pytest.mark.parametrize('mode', ['keep', 'remove', 'replace'])
def test_saved_review_retains_typed_choices_without_publishing_audio(actual, tmp_path, mode):
    app, runtime, source, cover, plan = reviewed(actual, tmp_path, mode)
    original = source.read_bytes(), cover.read_bytes()
    value = saved.save_choices(app.root, runtime, plan)
    assert saved.load_choices(app.root) == value
    assert value['review_required'] is True and value['changes']['tags'] == dict(plan.changes.tags)
    assert value['source'] == {'name': source.name, 'size': len(original[0]),
                               'sha256': sha256(original[0]).hexdigest()}
    assert value['output_name'] == plan.output.name and not plan.output.exists()
    raw = saved.validate_choices(value)
    assert raw == (original[1] if mode == 'replace' else None)
    assert str(tmp_path).encode() not in encode(value)
    assert (source.read_bytes(), cover.read_bytes()) == original


def test_actual_backup_restore_retains_choices_and_cover_without_external_or_engine_authority(
        actual, tmp_path, monkeypatch):
    app, runtime, source, cover, plan = reviewed(actual, tmp_path)
    original_cover = cover.read_bytes()
    saved.save_choices(app.root, runtime, plan)
    original_record = (app.root / saved.SIDECAR).read_bytes()
    source.unlink(); cover.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('Passive metadata backup/restore inspected an external input or started a media engine.')
    monkeypatch.setattr(metadata, 'run_process', forbidden)
    monkeypatch.setattr(saved, '_file', forbidden)
    monkeypatch.setattr(saved, '_input_stream', forbidden)
    destination = tmp_path / 'backup'
    checksum = create_backup(app, destination)
    manifest = json.loads((destination / 'manifest.json').read_bytes())
    names = {row['filename'] for row in manifest['sidecars']}
    assert saved.SIDECAR in names and not any(name.endswith('.choices.lock') for name in names)
    restored = restore_backup(destination, tmp_path / 'restored', checksum)
    assert (restored / saved.SIDECAR).read_bytes() == original_record
    choices = saved.load_choices(restored)
    assert choices['review_required'] is True and saved.validate_choices(choices) == original_cover
    assert not source.exists() and not cover.exists() and not plan.output.exists()
    assert not tuple((restored / 'state/audio-metadata').glob('*.mp3'))

    exported = tmp_path / 'explicit-retained-cover.png'
    receipt = saved.export_saved_cover(restored, exported)
    assert receipt.path == exported and exported.read_bytes() == original_cover
    assert not plan.output.exists()
    with pytest.raises(FileExistsError):
        saved.export_saved_cover(restored, exported)
    assert exported.read_bytes() == original_cover
    with pytest.raises(ValueError):
        saved.export_saved_cover(restored, restored / 'unapproved-private-cover.png')
    assert not (restored / 'unapproved-private-cover.png').exists()


@pytest.mark.parametrize('change', ['authority', 'source-path', 'unknown', 'schema', 'cover-bytes', 'cover-size'])
def test_typed_backup_refuses_unknown_authority_and_changed_retained_art(
        actual, tmp_path, change):
    app, runtime, _source, _cover, plan = reviewed(actual, tmp_path)
    value = saved.save_choices(app.root, runtime, plan)
    changed = deepcopy(value)
    if change == 'authority':
        changed['review_required'] = False
    elif change == 'source-path':
        changed['source']['path'] = '/unapproved/original.mp3'
    elif change == 'unknown':
        changed['command'] = ['start', 'worker']
    elif change == 'schema':
        changed['schema'] = 'carr-audio-metadata-choices/2'
    elif change == 'cover-bytes':
        changed['cover']['base64'] = 'A' + changed['cover']['base64'][1:]
    else:
        changed['cover']['size'] += 1
    atomic_json(app.root / saved.SIDECAR, changed, replace=True)
    output = tmp_path / 'rejected-backup'
    with pytest.raises(Exception):
        create_backup(app, output)
    assert not output.exists() and not plan.output.exists()


def test_cover_change_after_actual_review_cannot_replace_saved_passive_choices(actual, tmp_path):
    app, runtime, _source, cover, plan = reviewed(actual, tmp_path)
    original = saved.save_choices(app.root, runtime, plan)
    before = (app.root / saved.SIDECAR).read_bytes()
    raw = cover.read_bytes()
    cover.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(AudioError, match='cover changed'):
        saved.save_choices(app.root, runtime, plan)
    assert (app.root / saved.SIDECAR).read_bytes() == before
    assert saved.load_choices(app.root) == original and not plan.output.exists()


def test_cover_export_requires_current_bytes_new_path_and_explicit_cancellation(actual, tmp_path):
    app, runtime, _source, _cover, plan = reviewed(actual, tmp_path)
    saved.save_choices(app.root, runtime, plan)
    with pytest.raises(ValueError, match='extension'):
        saved.export_saved_cover(app.root, tmp_path / 'wrong.jpg')
    with pytest.raises(AudioError, match='cancelled'):
        saved.export_saved_cover(app.root, tmp_path / 'cancelled.png', cancelled=lambda: True)
    assert not (tmp_path / 'wrong.jpg').exists() and not (tmp_path / 'cancelled.png').exists()


def test_passive_cover_byte_bound_is_checked_before_base64_decode(actual, tmp_path, monkeypatch):
    app, runtime, _source, _cover, plan = reviewed(actual, tmp_path)
    value = saved.save_choices(app.root, runtime, plan)
    value['cover']['size'] = saved.MAX_COVER_BYTES + 1
    monkeypatch.setattr(saved, 'b64decode', lambda *a, **k: pytest.fail('An oversized cover was decoded.'))
    with pytest.raises(ValueError, match='byte count'):
        saved.validate_choices(value)


def test_displayed_cover_identity_refuses_a_second_actual_review_before_destination_open(
        actual, tmp_path, monkeypatch):
    app, runtime, source, _cover, plan = reviewed(actual, tmp_path)
    first = saved.save_choices(app.root, runtime, plan)
    expected = {key: first['cover'][key] for key in saved.COVER_IDENTITY_FIELDS}
    source_before = source.read_bytes()
    second_cover = tmp_path / 'second-reviewed-cover.png'
    second_cover.write_bytes(_helpers['png'](width=31, height=23))
    second_plan = review_metadata(runtime, source, plan.output,
        MetadataChanges(dict(plan.changes.tags), 'replace', str(second_cover)))
    second = saved.save_choices(app.root, runtime, second_plan)
    assert first['cover']['codec'] == second['cover']['codec'] == 'png'
    assert first['cover']['sha256'] != second['cover']['sha256']
    destination = tmp_path / 'displayed-cover.png'
    with monkeypatch.context() as refused:
        refused.setattr(saved, 'write_binary_export',
            lambda *a, **k: pytest.fail('A changed saved cover reached destination publication.'))
        with pytest.raises(ValueError, match='changed since it was displayed'):
            saved.export_saved_cover(app.root, destination, expected_cover=expected)
    assert not destination.exists() and not plan.output.exists()
    assert source.read_bytes() == source_before and saved.load_choices(app.root) == second

    # A separate explicit API request without a displayed identity still means
    # the current record; it does not authorize an audio conversion or overwrite.
    receipt = saved.export_saved_cover(app.root, destination)
    assert receipt.path == destination and destination.read_bytes() == second_cover.read_bytes()
    assert source.read_bytes() == source_before and not plan.output.exists()
