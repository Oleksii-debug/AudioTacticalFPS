import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.library_backup import restore_backup


def test_actual_complete_backup_includes_running_plan_and_checks_database_on_resume(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Original', 'Railway transport connected cities.')
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = AssistantService(app)
    scope = Scope(workspace, (), outputs, allow_full_backup=True,
                  max_bytes=8 * 1024 * 1024, max_files=1000)
    identity = service.submit(service.preview('Backup "complete"', scope))
    result = service.run(identity)
    assert result.status == 'complete', result.message
    backup = outputs / 'complete'
    saved = result.results[0]
    manifest = json.loads((backup / 'manifest.json').read_text())
    # The retained import journal is part of the complete library backup.
    assert manifest['version'] == 5
    assert manifest['layout'] == 'sidecars-pending-v1'
    assert any(row['filename'] == 'state/assistant/' + identity + '.json'
               for row in manifest['sidecars'])
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=saved['manifest_sha256'])
    reopened = LibraryApp(restored)
    assert reopened.search(workspace, 'Railway transport')[0].quote == 'Railway transport'
    assert (restored / 'state/assistant' / (identity + '.json')).is_file()
    # A restored plan never silently inherits permissions for another data root.
    with pytest.raises(PermissionError, match='another data directory'):
        AssistantService(reopened).run(identity)
    recovered_service = AssistantService(reopened)
    listed, = recovered_service.jobs()
    assert listed.status == 'attention'
    review = recovered_service.saved_plan_for_review(identity)
    assert 'scope' not in review
    assert [step['tool'] for step in review['steps']] == ['library.backup']
    assert not (restored / 'complete').exists()
    assert service.resume(identity).status == 'complete'
    database = backup / manifest['database']['filename']
    payload = bytearray(database.read_bytes())
    payload[-1] ^= 1
    database.write_bytes(payload)
    rejected = service.resume(identity)
    assert rejected.status == 'failed'
    assert 'backup changed' in rejected.message


def test_guided_import_reuses_extended_document_registry_and_preserves_original(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Sources')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = inputs / 'research.md'
    raw = '# Research\n\nRailway transport — unchanged source text.\n'.encode('utf-8')
    source.write_bytes(raw)
    service = AssistantService(app)
    scope = Scope(workspace, (inputs,))
    identity = service.submit(service.preview('Import "' + str(source) + '"\nSearch "Railway transport"', scope))
    result = service.run(identity)
    assert result.status == 'complete', result.message
    hit, = result.results[-1]['results']
    assert hit['quote'] == 'Railway transport'
    assert hit['verified'] is False
    assert source.read_bytes() == raw
