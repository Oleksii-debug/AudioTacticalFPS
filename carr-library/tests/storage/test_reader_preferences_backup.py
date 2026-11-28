"""Typed reader settings survive real restore without activating their contents."""
from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import state_lock
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.presentation import PresentationStore, ReadingPresentation
from carr_library.speech.reading import PronunciationRule
from carr_library.ui.pronunciation_dialog import DictionaryStore


APPEARANCE = 'state/speech/reading-presentation.json'
PRONUNCIATIONS = 'state/speech/pronunciations.json'


def prepared(root):
    app = LibraryApp(root)
    workspace = app.create_workspace('Reader settings recovery')
    app.add_text(workspace, 'Retained original', 'Незмінний оригінал. 😀', edition_id='original')
    PresentationStore(root).save(ReadingPresentation(zoom=125), expected_token=None)
    # Invalid regex remains passive rule data until the user explicitly previews.
    DictionaryStore(root).save((PronunciationRule('(', 'review first', regex=True),), expected=None)
    return app, workspace


def test_actual_backup_and_relocated_restore_keep_both_settings_without_execution(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    app, workspace = prepared(root)
    original = {name: (root / name).read_bytes() for name in (APPEARANCE, PRONUNCIATIONS)}
    monkeypatch.setattr(subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('Passive backup executed a process.'))
    folder = tmp_path / 'backup'
    with state_lock(root / 'state/speech', 'reading-presentation'):
        with state_lock(root / 'state/speech', 'pronunciations'):
            assert (root / 'state/speech/.reading-presentation.lock').is_file()
            assert (root / 'state/speech/.pronunciations.lock').is_file()
            digest = create_backup(app, folder)
    assert not list((folder / 'state/speech').glob('*.lock'))
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'relocated', digest)
    assert {name: (restored / name).read_bytes() for name in original} == original
    assert not list((restored / 'state/speech').glob('*.lock'))
    assert PresentationStore(restored).load()[0].zoom == 125
    assert DictionaryStore(restored).load()[0] == (PronunciationRule('(', 'review first', regex=True),)
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Retained original'


@pytest.mark.parametrize(('name', 'change'), [
    (APPEARANCE, 'unknown'), (APPEARANCE, 'schema'), (APPEARANCE, 'zoom'),
    (APPEARANCE, 'duplicate'), (APPEARANCE, 'large'), (APPEARANCE, 'color'),
    (PRONUNCIATIONS, 'unknown'), (PRONUNCIATIONS, 'schema'),
    (PRONUNCIATIONS, 'rules'), (PRONUNCIATIONS, 'extra_rule_field'),
    (PRONUNCIATIONS, 'rule_type'), (PRONUNCIATIONS, 'duplicate'),
])
def test_resigned_invalid_reader_state_is_rejected_before_destination_publication(tmp_path, name, change):
    app, _ = prepared(tmp_path / 'library')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = json.loads((folder / name).read_bytes())
    if change == 'unknown': value['command'] = 'must remain unrecognized'
    elif change == 'schema': value['schema'] = 'future-or-incorrect'
    elif change == 'zoom': value['zoom'] = True
    elif change == 'large': value['zoom'] = 301
    elif change == 'color': value['foreground'] = [0, 0, 256]
    elif change == 'rules': value['rules'] = {'unexpected': 'mapping'}
    elif change == 'extra_rule_field': value['rules'][0]['command'] = 'unrecognized'
    elif change == 'rule_type': value['rules'][0]['regex'] = 'yes'
    raw = json.dumps(value).encode('utf-8')
    if change == 'duplicate': raw = raw[:-1] + b',"schema":"duplicate"}'
    (folder / name).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(manifest_raw)
    destination = tmp_path / 'must-not-be-published'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, sha256(manifest_raw).hexdigest())
    assert not destination.exists()
    assert (folder / name).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == manifest_raw


def test_unknown_speech_sidecar_is_not_accepted_as_a_reader_preference(tmp_path):
    app, _ = prepared(tmp_path / 'library')
    unknown = app.root / 'state/speech/unreviewed-command.json'
    unknown.write_text('{"command":"never execute"}', encoding='utf-8')
    folder = tmp_path / 'must-not-be-published'
    with pytest.raises(ValueError, match='No validated backup adapter'):
        create_backup(app, folder)
    assert not folder.exists()
    assert unknown.read_text(encoding='utf-8') == '{"command":"never execute"}'
