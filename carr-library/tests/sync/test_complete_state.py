"""Portable transfer retains actual reader, Studio and approved-job state."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.durable_state import read_json
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore, FILENAME
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.source import read_source_range
from carr_library.sync.service import SyncService
from carr_library.studio.project import Project
from carr_library.ui.studio_dialog import StudioController


@pytest.mark.parametrize('encrypted', [False, True])
def test_portable_complete_state_retains_source_and_requires_job_reapproval(tmp_path, encrypted):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Exact local research')
    original = 'Exact original.\r\nНезмінне джерело — pôvodný text.'
    book = app.add_text(workspace, 'Original', original)
    desk = ReadingDesk(app, workspace, book['edition'])
    page = desk.page(1)
    reading = read_source_range(desk, 1, 1).reading_document()
    reading.set_text('An explicitly edited spoken derivative.')
    slot = 'portable-complete-state'
    reader_store = ReaderDraftStore(app.root, slot=slot)
    reader_path = reader_store.save(ReaderResume(
        reading, 'fixture:voice', rate=2, volume=67, cursor=5, audio_seconds=1.25))

    # Save through the actual Studio controller. The reading copy and Studio
    # project are independent durable producers; this does not invent a handoff API.
    studio = StudioController(app)
    first = studio.save(page.raw_text, 'Original performance', ())
    project = studio.save(reading.text, 'Edited performance', ())
    project_directory = studio.service.projects.directory(project.id)
    assert project.id == first.id and project.revision == 2
    history = tuple((project_directory / 'revisions').glob('*.json'))
    assert len(history) == 1 and Project.from_dict(read_json(history[0])).to_dict() == first.to_dict()
    project_relative = (project_directory / 'project.json').relative_to(app.root)
    draft_relative = studio.draft.relative_to(app.root)
    studio.service.close()

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
    preferences = ProgressPreferences(mode='beep', muted=True, interval_seconds=12)
    ProgressPreferenceStore(app.root / FILENAME).save(preferences)
    retained = {path.relative_to(app.root): path.read_bytes()
                for path in assistant.store.directory.glob('*.json')}
    retained.update({path.relative_to(app.root): path.read_bytes()
                     for path in (app.root / 'state' / 'studio').rglob('*.json')})
    retained[reader_path.relative_to(app.root)] = reader_path.read_bytes()
    retained[Path(FILENAME)] = (app.root / FILENAME).read_bytes()

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
    assert ProgressPreferenceStore(target / FILENAME).load() == preferences

    resumed = ReaderDraftStore(target, slot=slot).load()
    assert resumed.document.to_dict() == reading.to_dict()
    assert (resumed.voice_id, resumed.rate, resumed.volume, resumed.cursor,
            resumed.audio_seconds) == ('fixture:voice', 2, 67, 5, 1.25)
    source = read_source_range(ReadingDesk(recovered, workspace, book['edition']), 1, 1)
    assert resumed.document.source_anchor == source.reading_document().source_anchor
    assert resumed.document.source_anchor['quotation_authority'] is False
    assert resumed.document.undo() and resumed.document.text == page.raw_text
    assert resumed.document.redo() and resumed.document.text == reading.text

    restored_studio = StudioController(recovered)
    assert restored_studio.service.projects.load(project.id).to_dict() == project.to_dict()
    assert read_json(target / project_relative)['original_script'] == reading.text
    assert read_json(target / draft_relative)['text'] == reading.text
    assert read_json(target / draft_relative)['project_id'] == project.id
    assert restored_studio.service.root.is_relative_to(target)
    assert restored_studio.draft.is_relative_to(target)
    restored_studio.service.close()

    # The current assistant deliberately rejects old data-directory authority.
    # Reading the report and resuming both require a newly reviewed submission;
    # restoration itself does not rewrite the approval or its pause control.
    jobs = AssistantService(recovered)
    assert jobs.store.load(identity)['status'] == 'running'
    for operation in (jobs.get, jobs.resume):
        with pytest.raises(PermissionError, match='review and submit'):
            operation(identity)
    assert not tuple(output.iterdir())
    assert ReadingDesk(recovered, workspace, book['edition']).page(1).raw_text == page.raw_text
    assert recovered.assets.resolve(book['asset']).read_bytes() == original.encode('utf-8')
    assert {name: (target / name).read_bytes() for name in retained} == retained
    assert {name: (app.root / name).read_bytes() for name in retained} == retained
