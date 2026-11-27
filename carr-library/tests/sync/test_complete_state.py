"""Real encrypted/plain transfers compose current reading drafts and job authority."""
from dataclasses import replace
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.keybindings import encode_preferences
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.sync.service import SyncService


@pytest.mark.parametrize('encrypted', [False, True], ids=['plain', 'encrypted'])
def test_portable_complete_state_retains_source_and_requires_job_reapproval(tmp_path, encrypted):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Exact local research')
    original = 'Exact original.\r\nНезмінне джерело — pôvodný text.'
    app.add_text(workspace, 'Original', original)
    original_hit, = app.search(workspace, 'Exact original')
    original_quote = app.copy(workspace, original_hit.ref, original_hit.revision, 'quote')
    reading = ReadingDocument(original)
    reading.replace(0, len(reading.text), 'An explicitly edited spoken derivative.')
    state = ReaderResume(reading, 'unavailable:retained-voice', 0, 77, 5, 1.25)
    ReaderDraftStore(app.root).save(state)
    preference_path = app.root / 'keybindings.json'
    preference_path.write_bytes(encode_preferences({}))

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
    retained_jobs = {path.relative_to(app.root): path.read_bytes()
                     for path in assistant.store.directory.glob('*.json')}
    preferences = preference_path.read_bytes()

    service = SyncService(app)
    password = 'Synthetic transfer secret 12345' if encrypted else None
    folder = tmp_path / 'portable'
    receipt = service.publish(folder, 'Complete state', None, password)
    assert receipt['encrypted'] is encrypted
    assert service.inspect(folder, receipt['token'], password)['verified']
    target = tmp_path / 'new-device'
    service.restore(folder, receipt['token'], target, password)
    recovered = LibraryApp(target)
    resumed = ReaderDraftStore(target).load()
    assert resumed.document.to_dict() == reading.to_dict()
    assert (resumed.voice_id, resumed.volume, resumed.cursor) == (state.voice_id, 77, 5)
    assert resumed.document.undo() and resumed.document.text == original
    assert (target / 'keybindings.json').read_bytes() == preferences
    recovered_hit, = recovered.search(workspace, 'Exact original')
    assert recovered_hit.ref != original_hit.ref
    assert replace(recovered_hit, ref=original_hit.ref) == original_hit
    assert recovered.copy(workspace, recovered_hit.ref, recovered_hit.revision, 'quote') == original_quote
    jobs = AssistantService(recovered)
    assert jobs.get(identity).status == 'attention'
    assert jobs.store.load(identity)['restore_requires_review'] is True
    with pytest.raises(PermissionError, match='review'):
        jobs.resume(identity)
    assert {name: (app.root / name).read_bytes() for name in retained_jobs} == retained_jobs
    assert ReaderDraftStore(app.root).load().document.to_dict() == reading.to_dict()
    assert preference_path.read_bytes() == preferences
