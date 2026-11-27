"""Encrypted transfer composes the canonical reader, Studio and job backup."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.ui.operation_progress import ProgressPreferences, load_preferences, save_preferences
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.source import read_source_range
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.sync.service import SyncService
from carr_library.studio.project import ProjectStore, parse_script


@pytest.mark.parametrize('encrypted', [False, True])
def test_portable_complete_state_retains_source_and_requires_job_reapproval(tmp_path, encrypted):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Exact local research')
    original = 'Exact original.\r\nНезмінне джерело — pôvodný text.'
    book = app.add_text(workspace, 'Original', original)
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    reading = read_source_range(ReadingDesk(app, workspace, book['edition']), 1, 1).reading_document()
    reading.replace(0, len(reading.text), 'An explicitly edited spoken derivative.')
    draft = ReaderDraftStore(app.root, slot='portable-source')
    draft.save(ReaderResume(reading, cursor=3))
    studio = ProjectStore(app.root / 'state' / 'studio' / 'projects')
    project = parse_script(reading.text, title='Explicit spoken derivative', voice_ids=('synthetic-local-voice',))
    studio.save(project)
    project_path = studio.directory(project.id) / 'project.json'
    project_relative = project_path.relative_to(app.root)

    inputs, output = tmp_path / 'approved-inputs', tmp_path / 'approved-output'
    inputs.mkdir()
    output.mkdir()
    assistant = AssistantService(app)
    identity = assistant.submit(Plan(Scope(workspace, (inputs,), output), (
        Step('library.search', {'query': 'Exact original'}),
    )))
    assert assistant.run(identity).status == 'complete'
    record = assistant.store.load(identity)
    record['status'] = 'running'
    assistant.store.save(record)
    assistant.store.control(identity, 'pause')
    preferences = ProgressPreferences(mode='beep', quiet=True, interval_seconds=12)
    save_preferences(app.root, preferences)
    retained = {path.relative_to(app.root): path.read_bytes()
                for path in assistant.store.directory.glob('*.json')}
    retained[project_relative] = project_path.read_bytes()
    retained[draft.path.relative_to(app.root)] = draft.path.read_bytes()
    preferences_path = Path('state/ui/progress.json')
    retained[preferences_path] = (app.root / preferences_path).read_bytes()

    service = SyncService(app)
    password = 'Synthetic transfer secret 12345' if encrypted else None
    folder = tmp_path / 'portable'
    receipt = service.publish(folder, 'Complete state', None, password)
    assert receipt['encrypted'] is encrypted
    assert service.inspect(folder, receipt['token'], password)['verified']
    target = tmp_path / 'new-device'
    service.restore(folder, receipt['token'], target, password)
    recovered = LibraryApp(target)
    assert {name: (target / name).read_bytes() for name in retained} == retained
    assert load_preferences(target) == preferences
    restored_studio = ProjectStore(target / 'state/studio/projects').load(project.id)
    assert restored_studio.to_dict() == project.to_dict()
    assert restored_studio.original_script == reading.text
    restored_reading = ReaderDraftStore(target, slot='portable-source').load()
    assert restored_reading.document.source_text == page.raw_text
    assert restored_reading.document.source_anchor == reading.source_anchor
    assert restored_reading.document.text == reading.text and restored_reading.cursor == 3
    assert restored_reading.document.undo()
    assert restored_reading.document.text == page.raw_text
    jobs = AssistantService(recovered)
    assert jobs.store.load(identity) == record
    with pytest.raises(PermissionError, match='review and submit'):
        jobs.get(identity)
    with pytest.raises(PermissionError, match='review and submit'):
        jobs.resume(identity)
    assert ReadingDesk(recovered, workspace, book['edition']).page(1).raw_text == page.raw_text
    assert {name: (app.root / name).read_bytes() for name in retained} == retained
