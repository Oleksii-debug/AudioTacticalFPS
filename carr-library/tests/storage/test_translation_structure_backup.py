"""Current complete backup preserves structured derivatives without remote authority."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.state import atomic_json
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_state import validate_translation_job
from carr_library.assistant.translation_structure import capture_library, source_text
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from tests.assistant.test_translation import ready, text_calls, SECRET
from tests.assistant.test_translation_structure import submit


def selection(service):
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Structured backup source')
    book = app.add_text(workspace, 'Original source', '# Exact title\n\nFirst paragraph.\fSecond page.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}], markdown=True)
    return app, workspace, book, document


def test_complete_structured_backup_restores_exact_sources_and_offline_exports_only(ready, tmp_path):
    service, selected, vault, transport = ready
    app, workspace, book, document = selection(service)
    complete = submit(service, selected, document)
    assert service.resume(complete)['status'] == 'complete'
    queued = submit(service, selected, document)
    before = {identity: service.jobs.path(identity).read_bytes() for identity in (complete, queued)}
    calls = len(text_calls(transport))
    # Old sealed source lineage remains recoverable after a newer source-metadata
    # revision and library archival. It never becomes current run authority.
    app.update_metadata(workspace, book['edition'], title='Later title')
    app.archive_workspace(workspace)
    backup = tmp_path / 'complete-backup'
    checksum = create_backup(app, backup)
    verify_backup(backup, checksum)
    target = tmp_path / 'restored'
    restore_backup(backup, target, checksum)
    for identity, raw in before.items():
        assert (target / 'state/translation' / (identity + '.json')).read_bytes() == raw
    assert not (target / 'api-connections.json').exists()
    assert not (target / 'api-operations.sqlite').exists()
    restored = TranslationService(target)
    output = tmp_path / 'offline-structured-export'
    result = restored.export(complete, output)
    assert result['status'] == 'complete'
    assert (output / 'original.txt').read_bytes() == source_text(document).encode('utf-8')
    structured = json.loads((output / 'structured-translation.json').read_text(encoding='utf-8'))
    assert structured['documents'][0]['context'] == document['documents'][0]['context']
    manifest = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert manifest['schema'] == 'carr-translation-export/3' and not manifest['quotation_authority']
    for name, digest in manifest['files'].items():
        assert sha256((output / name).read_bytes()).hexdigest() == digest
    with pytest.raises(PermissionError, match='moved/restored'):
        restored.resume(queued)
    assert len(text_calls(transport)) == calls
    rebackup = tmp_path / 'rebackup'
    receipt = create_backup(LibraryApp(target), rebackup)
    verify_backup(rebackup, receipt)
    assert all(SECRET.encode() not in path.read_bytes() for path in backup.rglob('*') if path.is_file())


@pytest.mark.parametrize('change', ['metadata', 'workspace'])
def test_rehashed_structured_history_must_match_the_copied_database(ready, tmp_path, change):
    service, selected, vault, transport = ready
    app, workspace, book, document = selection(service)
    identity = submit(service, selected, document)
    job = deepcopy(service.jobs.load(identity))
    context = job['structure']['documents'][0]['context']
    if change == 'metadata':
        context['source_metadata']['author'] = 'Invented source author'
    else:
        context['source_anchor']['workspace'] = 'missing-workspace'
        for section in job['structure']['documents'][0]['sections']:
            if section['origin'] is not None:
                section['origin']['location']['workspace'] = 'missing-workspace'
    # Request bytes/hashes can remain self-consistent; the database binding is
    # a separate requirement and cannot be replaced by rehashing the envelope.
    validate_translation_job(job)
    atomic_json(service.jobs.path(identity), job)
    target = tmp_path / 'refused'
    with pytest.raises(ValueError, match='source|metadata'):
        create_backup(app, target)
    assert not target.exists() and not text_calls(transport)
