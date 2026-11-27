"""Encrypted transfer composes the canonical reader, Studio and job backup."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore, FILENAME
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.sync.service import SyncService
from carr_library.ui.audio_studio import StudioController


@pytest.mark.parametrize('encrypted', [False, True])
def test_portable_complete_state_retains_source_and_requires_job_reapproval(tmp_path, encrypted):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Exact local research')
    original = 'Exact original.\r\nНезмінне джерело — pôvodný text.'
    book = app.add_text(workspace, 'Original', original)
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    reading = revise(new_project(page.raw_text, source_location=page.location),
                     'An explicitly edited spoken derivative.')
    saved = NarrationStore(app.root).save(reading)
    studio = StudioController(private_root=app.root)
    studio.from_reading_project(reading, saved_digest=saved)
    project = studio.save_in_library()
    project_relative = project.relative_to(app.root)

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
    retained[project_relative] = project.read_bytes()
    retained[Path('progress-preferences.json')] = (app.root / 'progress-preferences.json').read_bytes()

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
    restored_studio = StudioController(private_root=target).open(target / project_relative)
    assert restored_studio.draft.source_text == page.raw_text
    assert restored_studio.script == reading.current_text
    assert restored_studio.output_dir.is_relative_to(target)
    jobs = AssistantService(recovered)
    assert jobs.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='review and submit'):
        jobs.resume(identity)
    assert ReadingDesk(recovered, workspace, book['edition']).page(1).raw_text == page.raw_text
    assert {name: (app.root / name).read_bytes() for name in retained} == retained
