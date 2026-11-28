"""A saved Studio path describes data; local selection grants read authority.

All fixtures are synthetic local bytes. No render or external engine is needed:
these regressions cover the earlier project/queue-to-backup boundary directly.
"""
from pathlib import Path
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json
from carr_library.library_backup import create_backup
from carr_library.speech import VoiceRegistry
from carr_library.studio.backup_inputs import RECEIPT, input_review
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def _project(source):
    return Project('synthetic-authority', 'Imported synthetic project', (),
        (Scene('scene-1', 'Local cue', (),
               cues=(Cue('cue-1', str(source)),)),),
        original_script='Synthetic project; saved paths do not grant file access.')


def _service(app, *, input_roots=()):
    return ProductionService(app.root / 'state' / 'studio',
                             voices=VoiceRegistry([]), input_roots=input_roots)


def _audio(path, *, changed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes((b'\x01\x00' if changed else b'\x00\x01') * 800)
    return path


@pytest.mark.parametrize('admission', ['imported-project', 'queued-job'])
def test_unapproved_saved_cue_cannot_enter_backup_without_reading_input(tmp_path, monkeypatch, admission):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic input authority')
    # A valid typed Cue can name an unrelated, non-audio local file.
    unrelated = tmp_path / 'unrelated-local-file.bin'
    unrelated.write_bytes(b'SYNTHETIC unrelated bytes, never selected as Studio input.')
    project = Project.from_dict(_project(unrelated).to_dict())
    service = _service(app)
    if admission == 'imported-project':
        # This is the persistence operation used by Open project JSON.
        service.projects.save(project)
    else:
        output = tmp_path / 'output'
        output.mkdir()
        job = service.submit(project, output)
        assert service.get(job)['status'] == 'pending'
    assert str(unrelated) not in input_review(service.root)['approved']

    opened = []
    original_open = Path.open

    def observe_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if path == unrelated and ('r' in mode or '+' in mode):
            opened.append(mode)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observe_open)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match=r'(?i)(review|approv|permission|authori|input)'):
        create_backup(app, destination)
    assert opened == [], 'Unapproved Cue content must not be opened before rejection.'
    assert not destination.exists()


def test_explicit_root_queued_input_survives_restart_and_backup_before_render(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic queued inputs')
    source = _audio(tmp_path / 'selected' / 'cue.wav')
    original = source.read_bytes()
    service = _service(app, input_roots=(source.parent,))
    output = tmp_path / 'output'
    output.mkdir()
    project = _project(source)
    job = service.submit(project, output, operation_id='queued-before-render')
    project_path = service.projects.directory(project.id) / 'project.json'
    project_bytes = project_path.read_bytes()

    # Constructor roots are no longer present; the explicit selection must
    # already have bound the queued input to a durable local file identity.
    del service
    reopened = LibraryApp(app.root)
    service = _service(reopened)
    pending = service.get(job)
    assert pending['status'] == 'pending' and not pending['nodes'] and not pending['artifacts']
    assert not Path(pending['destination']).exists()
    destination = tmp_path / 'backup'
    digest = create_backup(reopened, destination)
    assert len(digest) == 64
    receipt = read_json(destination / 'state' / RECEIPT)
    entry = receipt['inputs'][str(source)]
    assert (destination / 'state' / entry['filename']).read_bytes() == original
    assert project_path.read_bytes() == project_bytes
    assert service.get(job)['status'] == 'pending'
    assert not Path(pending['destination']).exists()


def test_queued_input_changed_at_same_path_requires_new_approval(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic fixed input identity')
    source = _audio(tmp_path / 'selected' / 'cue.wav')
    before = source.read_bytes()
    service = _service(app, input_roots=(source.parent,))
    output = tmp_path / 'output'
    output.mkdir()
    job = service.submit(_project(source), output, operation_id='fixed-input-identity')
    _audio(source, changed=True)
    assert source.stat().st_size == len(before) and source.read_bytes() != before

    reopened = LibraryApp(app.root)
    service = _service(reopened)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match=r'(?i)(changed|differ|review|approv|identity|input)'):
        create_backup(reopened, destination)
    assert not destination.exists()
    assert service.get(job)['status'] == 'pending'
    assert not Path(service.get(job)['destination']).exists()


def test_reopening_imported_project_never_clears_pending_review(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    source = _audio(tmp_path / 'selected' / 'cue.wav')
    project = Project.from_dict(_project(source).to_dict())
    service = _service(app)
    original_open = Path.open

    def reject_input_read(path, *args, **kwargs):
        if path == source:
            raise AssertionError('Reopening a project must not read its pending cue.')
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, 'open', reject_input_read)
        service.review_project_inputs(project)
        service.projects.save(project)
        # An existing pending decision is cleared only by explicit per-file
        # review, never by opening a JSON file or submitting its path again.
        reopened = _service(app, input_roots=(source.parent,))
        reopened.review_project_inputs(project)
        outputs = tmp_path / 'output'
        outputs.mkdir()
        job = reopened.submit(project, outputs)
        assert reopened.restored_inputs_to_review(job) == (str(source),)
        assert input_review(reopened.root)['approved'] == {}

    reopened.approve_restored_inputs([source])
    assert reopened.restored_inputs_to_review(job) == ()
    create_backup(app, tmp_path / 'approved-backup')


def test_selected_root_traversal_never_admits_unrelated_input(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    selected = tmp_path / 'selected'
    selected.mkdir()
    unrelated = tmp_path / 'unrelated.bin'
    unrelated.write_bytes(b'Unrelated synthetic local file.')
    raw_path = selected / '..' / unrelated.name
    service = _service(app, input_roots=(selected,))
    original_open = Path.open

    def reject_input_read(path, *args, **kwargs):
        if path in (raw_path, unrelated):
            raise AssertionError('An escaping Cue path has no read authority.')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', reject_input_read)
    service.review_project_inputs(_project(raw_path))
    assert input_review(service.root)['pending'] == [str(raw_path)]
    assert input_review(service.root)['approved'] == {}
