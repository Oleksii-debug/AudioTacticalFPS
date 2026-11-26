from copy import deepcopy
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.assistant.validation import validate_job, validate_state_document


def checkpoint(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    return app, service, identity, service.store.load(identity)


def test_backup_validation_does_not_access_saved_paths(tmp_path, monkeypatch):
    app, service, identity, saved = checkpoint(tmp_path)
    saved['data_dir'] = 'Z:/unavailable-original-device/library'
    saved['requires_reapproval'] = True
    monkeypatch.setattr('carr_library.assistant.models.local_path', lambda *a, **k: pytest.fail('Pure backup validation opened a path.'))
    raw = canonical(saved).encode('utf-8')
    assert validate_state_document(identity + '.json', raw) == saved
    assert validate_state_document(identity + '.control.json', b'{"command":"pause"}') == {'command': 'pause'}


@pytest.mark.parametrize('change', ['id', 'hash', 'scope', 'status', 'permission', 'step', 'marker'])
def test_structural_corruption_is_rejected(tmp_path, change):
    app, service, identity, saved = checkpoint(tmp_path)
    if change == 'id':
        saved['id'] = 'not-an-identity'
    elif change == 'hash':
        saved['plan_sha256'] = '0' * 64
    elif change == 'scope':
        saved['plan']['scope']['output_root'] = 'https://remote.example/upload'
    elif change == 'status':
        saved['status'] = 'claimed-success'
    elif change == 'permission':
        saved['plan']['scope']['allow_full_backup'] = 'yes'
    elif change == 'step':
        saved['steps'][0]['status'] = 'complete'
    else:
        saved['requires_reapproval'] = 'false'
    with pytest.raises(ValueError):
        validate_state_document(identity + '.json', canonical(saved))


def test_restored_plan_is_visible_but_cannot_retarget_or_resume(tmp_path):
    app, service, identity, saved = checkpoint(tmp_path)
    original_plan = deepcopy(saved['plan'])
    saved['data_dir'] = 'Z:/unavailable-original-device/library'
    saved['requires_reapproval'] = True
    atomic_json(service.store.path(identity), saved)
    report = service.get(identity)
    assert report.status == 'attention' and report.completed == 0
    assert service.jobs()[0] == report
    with pytest.raises(PermissionError, match='review and submit'):
        service.resume(identity)
    assert service.store.load(identity)['plan'] == original_plan


@pytest.mark.parametrize('name,payload', [
    ('x.lock', '{}'), ('0' * 32 + '.tmp', '{}'),
    ('0' * 32 + '.control.json', '{"command":"run","command":"cancel"}'),
    ('0' * 32 + '.control.json', '{"command":"shell"}'),
    ('0' * 32 + '.control.json', '{"command":"run","extra":true}')])
def test_backup_sidecars_exclude_locks_temps_and_ambiguous_control(name, payload):
    with pytest.raises(ValueError):
        validate_state_document(name, payload)
