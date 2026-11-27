"""Actual shared backups of translation data; provider transport is controlled.

SQLite, source imports, translation checkpoints, budget receipts and common
backup/verify/restore all run. Socket access is forbidden. No Google API,
translation-quality or native UI acceptance is claimed by these tests.
"""
from hashlib import sha256
import json
from pathlib import Path
import socket
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_sidecars
from carr_library.assistant import translation as domain
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.providers import GOOGLE_TRANSLATE_ENDPOINT, ProfileStore, ProviderError, ProviderProfile
from carr_library.reader.narration import NarrationStore, decode_project, encode_project, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk


class Vault:
    def __init__(self):
        self.values = {}
    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret
    def get(self, reference, binding):
        return self.values[reference, binding]
    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.after = lambda: None
        self.fail = False
    def get(self, profile, route, parameters, headers, **options):
        self.calls.append(('GET', dict(parameters)))
        return b'{"data":{"languages":[{"language":"en","name":"English"},{"language":"uk","name":"Ukrainian"}]}}'
    def post(self, profile, route, payload, headers, **options):
        body = json.loads(payload)
        self.calls.append(('POST', body))
        if self.fail:
            raise OSError('Controlled provider disconnect after possible transmission.')
        self.after()
        return json.dumps({'data': {'translations': [
            {'translatedText': 'Derived ' + body['q'], 'detectedSourceLanguage': 'en'}
        ]}}, ensure_ascii=False).encode('utf-8')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('A translation backup test attempted network access.')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def profile_for(app, vault):
    return ProfileStore(app.root / 'api-connections.json', vault).save(ProviderProfile(
        'google', 'Controlled translation profile', 'google-translate',
        GOOGLE_TRANSLATE_ENDPOINT, 'nmt', ('translation', 'translation_languages')),
        secret='SYNTHETIC-KEY-NEVER-SENT-TO-NETWORK')


@pytest.fixture
def host(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Translation backup')
    vault, transport = Vault(), Transport()
    profile = profile_for(app, vault)
    service = domain.TranslationService(app, vault=vault, transport=transport)
    service.refresh_languages(profile.id, approved_profile_fingerprint=profile.fingerprint)
    transport.calls.clear()
    return SimpleNamespace(app=app, workspace=workspace, vault=vault, transport=transport,
                           profile=profile, service=service)


def prepare(host, value='Exact pasted text — Україна.\r\n', **options):
    return host.service.prepare(host.workspace, host.profile.id, value, 'auto', 'uk', **options)


def approve(service, identity):
    review = service.review(identity, budget_micros=10000, per_call_micros=10)
    return service.approve(review, approved_review_sha256=review.digest)


def backup(host, destination):
    return create_backup(host.app, destination)


def restored(bundle, target, checksum):
    verify_backup(bundle, checksum)
    return LibraryApp(restore_backup(bundle, target, checksum))


def source_job(host, *, reading_copy=False, topic=None):
    original = 'Immutable Unicode source — Україна.\r\nExact original words.'
    imported = host.app.add_text(host.workspace, 'Original source', original)
    page = ReadingDesk(host.app, host.workspace, imported['edition']).page(1)
    if reading_copy:
        project = revise(new_project(page.raw_text, source_location=page.location),
                         'Explicit edited reading copy; separate from the original.')
        checksum = NarrationStore(host.app.root).save(project)
        identity = host.service.prepare_reading_copy(host.workspace, host.profile.id,
            project.id, checksum, 'auto', 'uk', topic=topic)
    else:
        identity = prepare(host, page.raw_text, source_location=page.location, topic=topic)
    return identity, imported, page


def rehash_sidecar(bundle, identity, value):
    name = domain.DIRECTORY + '/' + identity + '.json'
    raw = (canonical(value) + '\n').encode('utf-8')
    (bundle / name).write_bytes(raw)
    manifest = json.loads((bundle / 'manifest.json').read_text('utf-8'))
    entry = next(item for item in manifest['sidecars'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = (canonical(manifest) + '\n').encode('utf-8')
    (bundle / 'manifest.json').write_bytes(encoded)
    return sha256(encoded).hexdigest()


@pytest.mark.parametrize('state', ['complete', 'paused', 'attention'])
def test_exact_checkpoint_roundtrip_has_no_reusable_grant_or_network(host, tmp_path, state):
    identity = prepare(host, 'Original Unicode 😀\r\n' * (600 if state == 'paused' else 1))
    grant = approve(host.service, identity)
    stop = Event()
    if state == 'attention':
        host.transport.fail = True
        with pytest.raises(ProviderError, match='uncertain'):
            host.service.run(identity, grant=grant)
    else:
        if state == 'paused':
            host.transport.after = stop.set
        assert host.service.run(identity, grant=grant, cancelled=stop.is_set)['status'] == state
    before = host.service.store.path(identity).read_bytes()
    language = host.service.store.directory / 'languages-google.json'
    language_before = language.read_bytes()
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    app = restored(bundle, tmp_path / 'restored', checksum)
    assert (app.root / domain.DIRECTORY / (identity + '.json')).read_bytes() == before
    assert (app.root / domain.DIRECTORY / language.name).read_bytes() == language_before
    assert b'SYNTHETIC-KEY' not in before and grant.token.encode() not in before
    assert not (app.root / 'api-connections.json').exists()
    assert not (app.root / 'api-operations.sqlite').exists()
    transport = Transport()
    service = domain.TranslationService(app, vault=Vault(), transport=transport)
    assert service.summary(identity)['status'] == state
    assert service.details(identity)['quotation_authority'] is False
    assert service.jobs(host.workspace)[0]['id'] == identity
    with pytest.raises(PermissionError, match='fresh'):
        service.run(identity, grant=grant)
    assert transport.calls == [] and service._reviews == {} and service._grants == {}
    if state == 'complete':
        assert service.result(identity)[1].startswith('Derived Original')
    if state == 'attention':
        with pytest.raises(ProviderError, match='charged'):
            service.review(identity, budget_micros=100, per_call_micros=10)
        assert transport.calls == []


def test_restored_pause_requires_reconnection_and_fresh_review_before_only_remaining_chunks(host, tmp_path):
    identity = prepare(host, 'Original Unicode 😀\r\n' * 600)
    stop = Event()
    host.transport.after = stop.set
    host.service.run(identity, grant=approve(host.service, identity), cancelled=stop.is_set)
    before = host.service.details(identity)
    assert len(before['receipts']) == 1 and before['status'] == 'paused'
    # This grant has not been consumed: even an otherwise valid live grant is
    # not authority for a restored service instance.
    still_live = approve(host.service, identity)
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    app = restored(bundle, tmp_path / 'restored', checksum)
    vault, transport = Vault(), Transport()
    service = domain.TranslationService(app, vault=vault, transport=transport)
    with pytest.raises(PermissionError, match='fresh'):
        service.run(identity, grant=still_live)
    with pytest.raises(ProviderError, match='existing'):
        service.review(identity, budget_micros=10000, per_call_micros=10)
    profile = profile_for(app, vault)
    assert service.languages(profile.id) and transport.calls == []
    fresh = approve(service, identity)
    assert transport.calls == []
    result = service.run(identity, grant=fresh)
    assert result['status'] == 'complete'
    assert [body['q'] for method, body in transport.calls if method == 'POST'] == before['plan']['chunks'][1:]
    assert service.details(identity)['receipts'][0] == before['receipts'][0]


def test_restored_sending_marker_cannot_be_retried_even_after_reconnection(host, tmp_path):
    identity = prepare(host)
    host.transport.fail = True
    with pytest.raises(ProviderError):
        host.service.run(identity, grant=approve(host.service, identity))
    value = host.service.details(identity)
    value['status'] = 'running'  # An exact crash boundary before error recovery.
    domain.validate_job(value)
    atomic_json(host.service.store.path(identity), value)
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    app = restored(bundle, tmp_path / 'restored', checksum)
    vault, transport = Vault(), Transport()
    profile_for(app, vault)
    service = domain.TranslationService(app, vault=vault, transport=transport)
    with pytest.raises(ProviderError, match='charged'):
        service.review(identity, budget_micros=10000, per_call_micros=10)
    assert service.details(identity)['pending'] == value['pending']
    assert transport.calls == []


def test_saved_derivative_and_archived_historical_origin_remain_exact(host, tmp_path):
    topic = host.app.create_topic(host.workspace, 'Archived translation intent')
    identity, imported, page = source_job(host, reading_copy=True, topic=topic)
    host.service.run(identity, grant=approve(host.service, identity))
    project_id, project_hash = host.service.save_reading_copy(identity)
    original_asset = host.app.assets.resolve(imported['asset']).read_bytes()
    before = host.service.store.path(identity).read_bytes()
    project_bytes = (host.app.root / 'narration-projects' / (project_id + '.json')).read_bytes()
    new_revision = host.app.update_metadata(host.workspace, imported['edition'], title='Later metadata')
    assert new_revision != page.location.revision
    with host.app.topic_transaction(host.workspace) as tree:
        tree.archive(topic)
    host.app.archive_workspace(host.workspace)
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    app = restored(bundle, tmp_path / 'restored', checksum)
    assert (app.root / domain.DIRECTORY / (identity + '.json')).read_bytes() == before
    assert (app.root / 'narration-projects' / (project_id + '.json')).read_bytes() == project_bytes
    project, actual = NarrationStore(app.root).load(project_id)
    assert actual == project_hash and project.original_text == page.raw_text
    assert project.source_location == page.location
    assert project.current_text.startswith('Derived Explicit edited reading copy')
    assert app.assets.resolve(imported['asset']).read_bytes() == original_asset
    with app.read() as db:
        assert db.execute('SELECT archived FROM workspace_meta WHERE workspace=?', (host.workspace,)).fetchone()[0] == 1
        assert db.execute('SELECT archived FROM research_topic WHERE workspace=? AND id=?', (host.workspace, topic)).fetchone()[0] == 1


@pytest.mark.parametrize('bad_scope', ['missing-workspace', 'missing-topic', 'foreign-topic'])
@pytest.mark.parametrize('phase', ['backup', 'restore'])
def test_pasted_text_still_requires_exact_database_scope(host, tmp_path, bad_scope, phase):
    identity = prepare(host)
    other = host.app.create_workspace('Other library')
    foreign_topic = host.app.create_topic(other, 'Foreign topic')
    record = host.service.details(identity)
    assert domain.source_locations(record) == ()
    if phase == 'restore':
        bundle = tmp_path / 'bundle'
        backup(host, bundle)
    if bad_scope == 'missing-workspace':
        record['plan']['workspace'] = 'missing-workspace'
    else:
        record['plan']['topic'] = foreign_topic if bad_scope == 'foreign-topic' else 'missing-topic'
    record['plan_sha256'] = domain._hash(record['plan'])
    domain.validate_job(record)
    if phase == 'backup':
        atomic_json(host.service.store.path(identity), record)
        with pytest.raises(ValueError, match='missing library|saved library'):
            backup(host, tmp_path / 'rejected')
    else:
        checksum = rehash_sidecar(bundle, identity, record)
        with pytest.raises(ValueError, match='missing library|saved library'):
            verify_backup(bundle, checksum)
        with pytest.raises(ValueError, match='missing library|saved library'):
            restore_backup(bundle, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert host.transport.calls == []


@pytest.mark.parametrize('change', ['internal-digest', 'rehashed-direct-source', 'rehashed-reading-origin'])
def test_rehashed_outer_manifest_cannot_retarget_source_provenance(host, tmp_path, change):
    identity, imported, page = source_job(host, reading_copy=change == 'rehashed-reading-origin')
    original = host.app.assets.resolve(imported['asset']).read_bytes()
    bundle = tmp_path / 'bundle'
    backup(host, bundle)
    value = host.service.details(identity)
    plan = value['plan']
    if change == 'internal-digest':
        plan['source_sha256'] = '0' * 64
    elif change == 'rehashed-direct-source':
        plan['source_text'] = 'Invented source words.'
        plan['source_sha256'] = domain._hash_text(plan['source_text'])
        plan['chunks'] = list(domain.split_text(plan['source_text']))
        plan['source_location']['text_sha256'] = plan['source_sha256']
    else:
        origin = plan['origin']['project']
        origin['original_text'] = 'Invented original for the retained reading revision.'
        origin['original_sha256'] = domain._hash_text(origin['original_text'])
        origin['source_location']['text_sha256'] = origin['original_sha256']
        origin['revisions'][0]['text'] = origin['original_text']
        parsed = decode_project((canonical(origin) + '\n').encode())
        plan['origin']['project_sha256'] = sha256(encode_project(parsed)).hexdigest()
    value['plan_sha256'] = domain._hash(plan)
    if change != 'internal-digest':
        domain.validate_job(value)  # Self-consistent hashes alone are insufficient.
    checksum = rehash_sidecar(bundle, identity, value)
    with pytest.raises(ValueError, match='translation text|exact saved source'):
        verify_backup(bundle, checksum)
    with pytest.raises(ValueError, match='translation text|exact saved source'):
        restore_backup(bundle, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert host.app.assets.resolve(imported['asset']).read_bytes() == original
    assert host.transport.calls == []


@pytest.mark.parametrize('budget', ['file', 'language-cache', 'aggregate', 'jobs'])
def test_translation_budgets_fail_before_oversized_decode_or_publication(host, tmp_path, budget):
    identity = prepare(host)
    path = host.service.store.path(identity)
    if budget == 'file':
        with path.open('r+b') as stream:
            stream.truncate(domain.MAX_BYTES + 1)
    elif budget == 'language-cache':
        with (path.parent / 'languages-google.json').open('r+b') as stream:
            stream.truncate(domain.MAX_LANGUAGE_BYTES + 1)
    elif budget == 'aggregate':
        # Sparse files exercise real stat admission without allocating a large
        # JSON fixture. The aggregate guard must reject before decoding them.
        for number in range(5):
            with (path.parent / (f'{number:032x}.json')).open('wb') as stream:
                stream.truncate(domain.MAX_SNAPSHOT_BYTES // 5 + 1)
    else:
        value = host.service.details(identity)
        for number in range(domain.MAX_JOBS):
            copy = json.loads(canonical(value))
            copy['id'] = f'{number:032x}'
            (path.parent / (copy['id'] + '.json')).write_text(canonical(copy) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='budget|snapshot byte'):
        backup(host, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert path.exists() and host.transport.calls == []


def test_language_inventory_does_not_consume_a_job_slot_at_exact_limit(host, tmp_path):
    identity = prepare(host)
    value = host.service.details(identity)
    for number in range(domain.MAX_JOBS - 1):
        copy = json.loads(canonical(value))
        copy['id'] = f'{number:032x}'
        host.service.store.path(copy['id']).write_text(canonical(copy) + '\n', encoding='utf-8')
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    manifest = verify_backup(bundle, checksum)
    names = [item['filename'] for item in manifest['sidecars'] if item['filename'].startswith(domain.DIRECTORY + '/')]
    assert len(names) == domain.MAX_JOBS + 1
    assert domain.DIRECTORY + '/languages-google.json' in names


def test_only_exact_producer_locks_and_atomic_temporaries_are_excluded(host, tmp_path):
    identity = prepare(host)
    directory = host.service.store.directory
    names = [identity + '.lock', '.' + identity + '.json.' + uuid4().hex + '.tmp',
             '.languages-google.json.' + uuid4().hex + '.tmp']
    for name in names:
        (directory / name).write_bytes(b'')
    bundle = tmp_path / 'bundle'
    checksum = backup(host, bundle)
    app = restored(bundle, tmp_path / 'restored', checksum)
    assert all((directory / name).exists() for name in names)
    assert all(not (app.root / domain.DIRECTORY / name).exists() for name in names)
    assert domain.validate_backup_file('languages-google.json',
        (app.root / domain.DIRECTORY / 'languages-google.json').read_bytes())['profile_id'] == 'google'


@pytest.mark.parametrize('name', ['credentials.json', '.unrecognized.tmp', 'sub/' + '1' * 32 + '.json'])
def test_unknown_translation_neighbors_are_not_silently_dropped(host, tmp_path, name):
    prepare(host)
    path = host.service.store.directory / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'{"synthetic":"unrecognized user data"}')
    with pytest.raises(ValueError, match='translation backup filename'):
        backup(host, tmp_path / 'rejected')
    assert path.read_bytes() == b'{"synthetic":"unrecognized user data"}'
    assert not (tmp_path / 'rejected').exists()


def test_invalid_cached_language_digest_is_not_backed_up(host, tmp_path):
    prepare(host)
    path = host.service.store.directory / 'languages-google.json'
    value = json.loads(path.read_text('utf-8'))
    value['languages_sha256'] = '0' * 64
    atomic_json(path, value)
    with pytest.raises(ValueError, match='language inventory changed'):
        backup(host, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_cancellation_after_real_translation_read_never_publishes_backup(host, tmp_path, monkeypatch):
    identity = prepare(host)
    source = host.service.store.path(identity)
    before = source.read_bytes()
    read = backup_sidecars.read_regular
    stop = Event()
    def observed(path, maximum):
        value = read(path, maximum)
        if Path(path) == source:
            stop.set()
        return value
    monkeypatch.setattr(backup_sidecars, 'read_regular', observed)
    with pytest.raises(InterruptedError):
        create_backup(host.app, tmp_path / 'cancelled', cancelled=stop.is_set)
    assert not (tmp_path / 'cancelled').exists()
    assert source.read_bytes() == before and host.transport.calls == []

