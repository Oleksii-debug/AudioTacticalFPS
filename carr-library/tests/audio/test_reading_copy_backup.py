"""Actual reading-to-Studio producers retain strict source provenance in snapshots."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import zipfile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.backup import backup_project, restore_project
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.narration_editor import NarrationController
from carr_library.ui.audio_studio import StudioController


def _producer(tmp_path, *, source_bound):
    app = LibraryApp(tmp_path / 'library')
    location = None
    original = 'Original railway text. Українська мова.'
    if source_bound:
        workspace = app.create_workspace('Reading evidence')
        imported = app.add_text(workspace, 'Original', original)
        page = ReadingDesk(app, workspace, imported['edition']).page(1)
        original, location = page.raw_text, page.location
    reading = new_project(original, title='Prepared reading', source_location=location)
    editor = NarrationController(NarrationStore(app.root), reading)
    editor.set_text(original.replace('railway', 'rail way') + '\nPrepared for reading.')
    editor.save()
    studio = editor.audio_copy()
    output = tmp_path / 'audio'
    output.mkdir()
    studio.configure(output_dir=output)
    return app, editor, studio


@pytest.mark.parametrize('source_bound', [False, True], ids=['independent', 'source-bound'])
def test_producer_snapshot_preserves_historical_reading_reference_after_studio_edits(tmp_path, source_bound):
    app, editor, studio = _producer(tmp_path, source_bound=source_bound)
    reference = deepcopy(studio.source_reference)
    original = editor.project.original_text
    reading_text = editor.project.current_text
    assert reference['reading_sha256'] == sha256(reading_text.encode()).hexdigest()
    studio.edit_script(studio.script + '\nLater saved Studio edit.')
    source = studio.save(tmp_path / 'studio.json')
    saved_script = studio.script
    studio.edit_script(studio.script + '\nRecoverable Studio edit.')
    recovery = studio.save_recovery()
    source_bytes, recovery_bytes = source.read_bytes(), recovery.read_bytes()
    assert reference['reading_sha256'] != studio.draft.to_dict()['reading_sha256']
    snapshot = tmp_path / 'reading-audio.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir)
    restored = restore_project(snapshot, tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private').open(restored['project_file'])
    assert reopened.script == saved_script and reopened.source_reference == reference
    assert reopened.draft.source_text == original
    assert reopened.draft.source_id == 'reading-copy:' + editor.project.id
    reopened.recover()
    assert reopened.script == studio.script and reopened.source_reference == reference
    assert reopened.draft.source_sha256 == reference['original_sha256']
    assert reopened.output_dir == Path(restored['project_dir'])
    assert source.read_bytes() == source_bytes and recovery.read_bytes() == recovery_bytes
    assert editor.store.load(editor.project.id)[0].current_text == reading_text
    assert editor.project.original_text == original
    if source_bound:
        page = ReadingDesk(app, reference['source_location']['workspace'], reference['source_location']['edition']).page(1)
        assert page.raw_text == original


def _damage(payload, damage):
    reference = payload['source_reference']
    if damage == 'wrong-project':
        reference['project_id'] = 'f' * 32
    elif damage == 'wrong-original':
        reference['original_sha256'] = '0' * 64
    elif damage == 'malformed-reading':
        reference['reading_sha256'] = 'not a digest'
    elif damage == 'historical-mismatch':
        reference['reading_sha256'] = 'f' * 64
    elif damage == 'location-extra':
        reference['source_location']['path'] = '/untrusted/source'
    elif damage == 'location-hash':
        reference['source_location']['text_sha256'] = '0' * 64
    elif damage == 'location-index':
        reference['source_location']['index'] = True
    elif damage == 'extra-field':
        reference['credential'] = 'not exported'
    else:
        del reference['reading_sha256']


@pytest.mark.parametrize('recovery_member', [False, True], ids=['saved-project', 'recovery'])
@pytest.mark.parametrize('damage', ['wrong-project', 'wrong-original', 'malformed-reading',
                                  'historical-mismatch',
                                  'location-extra', 'location-hash', 'location-index',
                                  'extra-field', 'missing-field'])
def test_malformed_and_resigned_reading_references_never_publish(tmp_path, recovery_member, damage):
    _, _, studio = _producer(tmp_path, source_bound=True)
    source = studio.save(tmp_path / 'studio.json')
    studio.edit_script(studio.script + '\nRecovery edit.')
    recovery = studio.save_recovery()
    snapshot = tmp_path / 'good.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir)
    selected = recovery if recovery_member else source
    payload = json.loads(selected.read_bytes())
    _damage(payload['project'] if recovery_member else payload, damage)
    damaged = json.dumps(payload).encode()
    selected.write_bytes(damaged)
    rejected = tmp_path / 'bad.zip'
    with pytest.raises(ValueError):
        backup_project(source, rejected, project_dir=studio.output_dir)
    assert not rejected.exists() and selected.read_bytes() == damaged

    with zipfile.ZipFile(snapshot) as incoming:
        contents = {name: incoming.read(name) for name in incoming.namelist()}
    member = 'studio/project.json' + ('.recovery.json' if recovery_member else '')
    contents[member] = damaged
    manifest = json.loads(contents['manifest.json'])
    record = next(row for row in manifest['files'] if row['path'] == member)
    record.update(size=len(damaged), sha256=sha256(damaged).hexdigest())
    manifest['total_bytes'] = sum(row['size'] for row in manifest['files'])
    contents['manifest.json'] = json.dumps(manifest).encode()
    resigned = tmp_path / 'resigned.zip'
    with zipfile.ZipFile(resigned, 'w') as outgoing:
        for name, raw in contents.items():
            outgoing.writestr(name, raw)
    with pytest.raises(ValueError):
        restore_project(resigned, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert not list(tmp_path.glob('.audio-restore-*'))
