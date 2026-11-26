"""Actual SQLite/FFmpeg backup recovery using explicitly synthetic PCM fixtures.

These tests exercise storage integrity and resumed render dependencies. They do
not assert installed voices, perceptual speech quality, or physical playback.
"""
from array import array
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
import wave

import pytest

from carr_library.studio import Line, Project, ProjectStore, Role, Scene, narration_project
from carr_library.studio.backup import BackupCancelled, BackupError, backup_project, restore_project
from carr_library.studio.director import Director
from carr_library.studio.script import dialogue_project
from carr_library.studio.store import digest_file


def speech_fixture(calls, *, entered=None, release=None):
    def speak(request, target, cancelled=None):
        calls.append(request.text)
        if entered is not None and len(calls) == 2:
            entered.set()
            assert release.wait(15), 'Test render was not released.'
        if cancelled:
            cancelled()
        samples = array('h', [4000, -4000] * 1200)
        if os.sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(target), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'PCM fixture'))
            output.writeframes(samples.tobytes())
        return dict(path=str(target), sha256=digest_file(target), frames=2400,
                    sample_rate=24000, channels=1, duration_seconds=.1,
                    engine_fingerprint='explicit-test-fixture-v1')
    return speak


def director(store, calls, **kwargs):
    return Director(store, speech_renderer=speech_fixture(calls, **kwargs),
                    engine_identity=lambda voice: 'explicit-test-fixture-v1')


@pytest.fixture
def rendered(tmp_path):
    store = ProjectStore(tmp_path / 'проєкт ľšč')
    prior = narration_project('An earlier exact revision.\r\n', 'fixture:reader')
    store.save(prior)
    script = '## Перша сцена\r\nNarrator: Українська e\u0301.\r\n\r\nNarrator: Slovenský originál.\r\n'
    project = dialogue_project(script, {'Narrator': Role('reader', 'fixture:reader')})
    calls = []
    result = director(store, calls).render(project)
    return store, project, script, calls, result, prior


@pytest.fixture
def bundle(rendered, tmp_path):
    store, *_ = rendered
    path = tmp_path / 'запасна копія ľšč'
    digest = backup_project(store, path)
    return path, digest


def manifest(path):
    return json.loads((path / 'manifest.json').read_text(encoding='utf-8'))


def seal(path, value=None, *, files=False):
    value = value or manifest(path)
    db = path / 'studio.sqlite'
    value['database'].update(size_bytes=db.stat().st_size, sha256=digest_file(db))
    if files:
        for entry in value['files']:
            selected = path / entry['path']
            entry.update(size_bytes=selected.stat().st_size, sha256=digest_file(selected))
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (path / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def tree_digests(root):
    return {p.relative_to(root).as_posix(): digest_file(p) for p in root.rglob('*') if p.is_file()}


def prohibit_writable_restore(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unverified backup reached writable restore preparation.')
    monkeypatch.setattr('carr_library.studio.backup._prepare_restore', forbidden)


def test_full_backup_preserves_exact_script_revisions_results_and_reuses_audio(rendered, bundle, tmp_path):
    source, project, script, _, result, prior = rendered
    path, digest = bundle
    untouched = tree_digests(path)
    restored = restore_project(path, tmp_path / 'відновлено ľšč', expected_manifest_sha256=digest)
    assert restored.load() == project
    assert restored.load().authored_script == script
    assert restored.load(prior.revision) == prior
    assert (restored.revisions / (project.revision + '.json')).read_bytes() == (
        source.revisions / (project.revision + '.json')).read_bytes()
    jobs = restored.jobs()
    assert jobs[0]['state'] == 'complete'
    assert Path(jobs[0]['result']['master']['path']).is_relative_to(restored.root)
    assert Path(jobs[0]['result']['manifest_path']).is_file()
    assert jobs[0]['result']['master']['sha256'] == result['master']['sha256']
    with restored.connect() as db:
        for key, relative in db.execute('SELECT cache_key,path FROM chunks'):
            assert relative.startswith('cache/') and not Path(relative).is_absolute()
            assert Path(restored.cached(key)['path']).is_relative_to(restored.cache)
    calls = []
    resumed = director(restored, calls).render(restored.load())
    assert calls == [] and resumed['completed_chunks'] == 0
    assert resumed['master']['sha256'] == result['master']['sha256']
    assert tree_digests(path) == untouched


def test_audio_free_restore_keeps_history_and_renders_again_without_publication_collision(rendered, tmp_path):
    source, project, script, _, result, _ = rendered
    backup = tmp_path / 'plans-only'
    digest = backup_project(source, backup, include_audio=False)
    assert not any(e['kind'] in ('cache', 'render-audio') for e in manifest(backup)['files'])
    restored = restore_project(backup, tmp_path / 'plans-restored', expected_manifest_sha256=digest)
    assert restored.load().authored_script == script
    assert restored.jobs()[0]['state'] == 'interrupted' and restored.jobs()[0]['result'] is None
    with restored.connect() as db:
        assert db.execute('SELECT count(*) FROM chunks').fetchone()[0] == 0
    archived = restored.root / 'restore-history' / digest / 'renders' / project.revision / 'production.json'
    assert archived.read_bytes() == (source.renders / project.revision / 'production.json').read_bytes()
    assert not (restored.renders / project.revision / 'production.json').exists()
    calls = []
    resumed = director(restored, calls).render(restored.load())
    assert len(calls) == 2 and resumed['master']['sha256'] == result['master']['sha256']
    second = tmp_path / 'backed-up-again'
    next_digest = backup_project(restored, second)
    again = restore_project(second, tmp_path / 'restored-again', expected_manifest_sha256=next_digest)
    assert (again.root / archived.relative_to(restored.root)).read_bytes() == archived.read_bytes()


def test_active_render_snapshot_excludes_uncommitted_partials_and_resumes_only_remaining_speech(tmp_path):
    store = ProjectStore(tmp_path / 'active')
    project = Project('Active job', (Role('reader', 'fixture:reader'),),
        (Scene('one', 'One', (Line('first', 'reader', 'Committed first words.'),
                             Line('second', 'reader', 'Not yet rendered words.'))),))
    entered, release = threading.Event(), threading.Event()
    calls, failures = [], []
    def run():
        try:
            director(store, calls, entered=entered, release=release).render(project)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(10), 'First chunk did not complete.'
        (store.cache / 'uncommitted-partial.wav').write_bytes(b'partial private render')
        (store.revisions / ('f' * 64 + '.json')).write_text('not a committed revision', encoding='utf-8')
        backup = tmp_path / 'during-render'
        digest = backup_project(store, backup)
        copied = {e['path'] for e in manifest(backup)['files']}
        assert not any('uncommitted' in p or 'f' * 64 in p for p in copied)
        assert worker.is_alive() and store.jobs()[0]['state'] == 'running'
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and not failures
    assert store.jobs()[0]['state'] == 'complete'
    restored = restore_project(backup, tmp_path / 'resuming-active', expected_manifest_sha256=digest)
    assert restored.jobs()[0]['state'] == 'interrupted'
    assert restored.jobs()[0]['progress']['completed_chunks'] >= 1
    resumed_calls = []
    director(restored, resumed_calls).render(restored.load())
    assert resumed_calls == ['Not yet rendered words.']


def test_sqlite_snapshot_ignores_an_uncommitted_database_transaction(rendered, tmp_path):
    store, project, *_ = rendered
    with closing(sqlite3.connect(store.database)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('DELETE FROM current_project')
        writer.execute('DELETE FROM chunks')
        backup = tmp_path / 'uncommitted-transaction'
        digest = backup_project(store, backup)
        writer.rollback()
    restored = restore_project(backup, tmp_path / 'committed-only', expected_manifest_sha256=digest)
    assert restored.load() == project
    with restored.connect() as db:
        assert db.execute('SELECT count(*) FROM chunks').fetchone()[0] > 0


def test_unknown_or_external_completed_result_is_cleared_and_verified_cache_remains(rendered, tmp_path):
    store, project, _, _, result, _ = rendered
    changed = {**result, 'master': {**result['master'], 'path': str(tmp_path / 'unrelated.wav')}}
    store.checkpoint(result['job_id'], state='complete', result=changed)
    backup = tmp_path / 'unknown-result'
    digest = backup_project(store, backup)
    assert not any(e['path'].startswith('renders/') for e in manifest(backup)['files'])
    restored = restore_project(backup, tmp_path / 'safe-result', expected_manifest_sha256=digest)
    assert restored.jobs()[0]['state'] == 'interrupted' and restored.jobs()[0]['result'] is None
    calls = []
    resumed = director(restored, calls).render(project)
    assert calls == [] and resumed['master']['sha256'] == result['master']['sha256']


@pytest.mark.parametrize('kind', ['cache', 'revision', 'render-metadata', 'render-audio', 'database'])
def test_corrupt_bytes_rejected_before_writable_open(bundle, tmp_path, monkeypatch, kind):
    backup, digest = bundle
    entry = manifest(backup)['database'] if kind == 'database' else next(e for e in manifest(backup)['files'] if e['kind'] == kind)
    file = backup / entry['path']
    data = bytearray(file.read_bytes())
    data[-1] ^= 1
    file.write_bytes(data)
    original = tree_digests(backup)
    prohibit_writable_restore(monkeypatch)
    destination = tmp_path / 'bad-checksum'
    with pytest.raises(BackupError, match='checksum'):
        restore_project(backup, destination, expected_manifest_sha256=digest)
    assert not destination.exists() and tree_digests(backup) == original


@pytest.mark.parametrize('sql', [
    'CREATE TABLE unknown_extension(value TEXT)',
    'CREATE INDEX unknown_index ON jobs(state)',
    'CREATE TRIGGER dangerous AFTER UPDATE ON jobs BEGIN DELETE FROM projects; END',
    'ALTER TABLE current_project RENAME TO other_current',
    'PRAGMA user_version=2',
])
def test_resealed_incompatible_schema_rejected_before_writable_open(bundle, tmp_path, monkeypatch, sql):
    backup, _ = bundle
    with closing(sqlite3.connect(backup / 'studio.sqlite')) as db:
        db.execute(sql)
        db.commit()
    digest = seal(backup)
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError, match='schema'):
        restore_project(backup, tmp_path / 'bad-schema', expected_manifest_sha256=digest)


@pytest.mark.parametrize('change', ['cache_traversal', 'wrong_frames', 'wrong_revision', 'invalid_current'])
def test_resealed_semantic_corruption_rejected_before_writable_open(bundle, tmp_path, monkeypatch, change):
    backup, _ = bundle
    with closing(sqlite3.connect(backup / 'studio.sqlite')) as db:
        if change == 'cache_traversal':
            db.execute("UPDATE chunks SET path='cache/../../external.wav'")
        elif change == 'wrong_frames':
            key, raw = db.execute('SELECT cache_key,report FROM chunks LIMIT 1').fetchone()
            report = json.loads(raw)
            report['frames'] += 1
            db.execute('UPDATE chunks SET report=? WHERE cache_key=?', (json.dumps(report), key))
        elif change == 'wrong_revision':
            db.execute("UPDATE projects SET document=replace(document,'An earlier','Changed original')")
        else:
            db.execute('UPDATE current_project SET revision=?', ('0' * 64,))
        db.commit()
    digest = seal(backup)
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError):
        restore_project(backup, tmp_path / 'bad-records', expected_manifest_sha256=digest)


@pytest.mark.parametrize('value', ['../outside', '/outside', 'cache/../outside', 'cache\\outside',
                                  'C:/outside', 'cache//outside', 'cache/CON.wav', 'cache/file.wav.'])
def test_manifest_traversal_and_nonportable_paths_are_rejected(bundle, tmp_path, monkeypatch, value):
    backup, _ = bundle
    data = manifest(backup)
    data['files'][0]['path'] = value
    digest = seal(backup, data)
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError):
        restore_project(backup, tmp_path / 'path-rejected', expected_manifest_sha256=digest)


@pytest.mark.parametrize('change', ['duplicate', 'boolean_size', 'oversized', 'wrong_kind', 'wrong_schema', 'nonboolean_audio'])
def test_manifest_shape_and_budgets_are_strict(bundle, tmp_path, monkeypatch, change):
    backup, _ = bundle
    data = manifest(backup)
    if change == 'duplicate':
        data['files'].append(dict(data['files'][0]))
    elif change == 'boolean_size':
        data['files'][0]['size_bytes'] = True
    elif change == 'oversized':
        data['files'][0]['size_bytes'] = 2**50
    elif change == 'wrong_kind':
        data['files'][0]['kind'] = 'execute'
    elif change == 'wrong_schema':
        data['schema_fingerprint'] = '0' * 64
    else:
        data['include_audio'] = 1
    digest = seal(backup, data)
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError):
        restore_project(backup, tmp_path / 'shape-rejected', expected_manifest_sha256=digest)


@pytest.mark.parametrize('extra', ['file', 'directory', 'missing'])
def test_incomplete_or_extra_inventory_is_rejected(bundle, tmp_path, monkeypatch, extra):
    backup, digest = bundle
    if extra == 'file':
        (backup / 'uncommitted-secret.txt').write_text('Unexpected data.', encoding='utf-8')
    elif extra == 'directory':
        (backup / 'unlisted').mkdir()
    else:
        (backup / manifest(backup)['files'][0]['path']).unlink()
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError, match='incomplete|unexpected'):
        restore_project(backup, tmp_path / 'inventory-rejected', expected_manifest_sha256=digest)


def test_symlink_in_backup_never_reads_its_target(bundle, tmp_path, monkeypatch):
    backup, digest = bundle
    target = tmp_path / 'outside-private.wav'
    target.write_bytes(b'Private unrelated data.')
    chosen = backup / manifest(backup)['files'][0]['path']
    chosen.unlink()
    try:
        chosen.symlink_to(target)
    except OSError as exc:
        # Creating a symlink requires a Windows privilege on some CI hosts.
        # This branch proves the host restriction and an independent local-
        # path boundary; it does not claim an actual symlink was exercised.
        assert os.name == 'nt' and exc.winerror == 1314
        with pytest.raises(BackupError, match='local'):
            restore_project('\\\\server\\private', tmp_path / 'network-refused',
                            expected_manifest_sha256=digest)
        return
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError, match='links|reparse'):
        restore_project(backup, tmp_path / 'symlink-rejected', expected_manifest_sha256=digest)
    assert target.read_bytes() == b'Private unrelated data.'


def test_production_cannot_disagree_with_resealed_completed_job(bundle, tmp_path, monkeypatch):
    backup, _ = bundle
    entry = next(e for e in manifest(backup)['files'] if e['path'].endswith('/production.json'))
    path = backup / entry['path']
    data = json.loads(path.read_text(encoding='utf-8'))
    data['title'] = 'Changed production title.'
    path.write_text(json.dumps(data), encoding='utf-8')
    digest = seal(backup, files=True)
    prohibit_writable_restore(monkeypatch)
    with pytest.raises(BackupError, match='Production manifest'):
        restore_project(backup, tmp_path / 'production-rejected', expected_manifest_sha256=digest)


def test_windows_origin_paths_are_portable_and_reusable_on_this_host(bundle, tmp_path):
    backup, _ = bundle
    data = manifest(backup)
    old_root = Path(data['original_root'])
    new_root = 'C:\\Users\\Oleksii\\Studio project'
    data['original_root'] = new_root
    def remap(path):
        return new_root + '\\' + Path(path).relative_to(old_root).as_posix().replace('/', '\\')
    with closing(sqlite3.connect(backup / 'studio.sqlite')) as db:
        for key, path, raw in db.execute('SELECT cache_key,path,report FROM chunks').fetchall():
            report = json.loads(raw)
            report['path'] = remap(report['path'])
            db.execute('UPDATE chunks SET path=?,report=? WHERE cache_key=?', (path.replace('/', '\\'), json.dumps(report), key))
        for identity, raw in db.execute('SELECT id,result FROM jobs WHERE result IS NOT NULL').fetchall():
            result = json.loads(raw)
            result['master']['path'] = remap(result['master']['path'])
            for field in ('manifest_path', 'captions_path'):
                result[field] = remap(result[field])
            for chapter in result['chapters']:
                chapter['path'] = chapter['path'].replace('/', '\\')
            db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(result), identity))
        db.commit()
    for entry in data['files']:
        if entry['path'].endswith('/production.json'):
            path = backup / entry['path']
            production = json.loads(path.read_text(encoding='utf-8'))
            production['master']['path'] = production['master']['path'].replace('/', '\\')
            for chapter in production['chapters']:
                chapter['path'] = chapter['path'].replace('/', '\\')
            path.write_text(json.dumps(production), encoding='utf-8')
    digest = seal(backup, data, files=True)
    restored = restore_project(backup, tmp_path / 'portable', expected_manifest_sha256=digest)
    assert restored.jobs()[0]['state'] == 'complete'
    calls = []
    result = director(restored, calls).render(restored.load())
    assert not calls and result['completed_chunks'] == 0


def test_existing_or_nested_destinations_are_never_overwritten(rendered, bundle, tmp_path):
    store, *_ = rendered
    backup, digest = bundle
    existing = tmp_path / 'existing'
    existing.mkdir()
    marker = existing / 'keep.txt'
    marker.write_text('Keep this original.', encoding='utf-8')
    with pytest.raises(FileExistsError):
        backup_project(store, existing)
    with pytest.raises(FileExistsError):
        restore_project(backup, existing, expected_manifest_sha256=digest)
    assert marker.read_text(encoding='utf-8') == 'Keep this original.'
    with pytest.raises(BackupError, match='outside'):
        backup_project(store, store.root / 'nested-backup')
    with pytest.raises(BackupError, match='outside'):
        restore_project(backup, backup / 'nested-restore', expected_manifest_sha256=digest)


def test_cancelled_backup_and_restore_do_not_publish(rendered, bundle, tmp_path):
    store, *_ = rendered
    backup, digest = bundle
    stop = threading.Event()
    stop.set()
    with pytest.raises(BackupCancelled):
        backup_project(store, tmp_path / 'cancelled-backup', cancelled=stop)
    with pytest.raises(BackupCancelled):
        restore_project(backup, tmp_path / 'cancelled-restore', expected_manifest_sha256=digest, cancelled=stop)
    assert not (tmp_path / 'cancelled-backup').exists() and not (tmp_path / 'cancelled-restore').exists()
    calls = 0
    def after_start():
        nonlocal calls
        calls += 1
        return calls > 12
    with pytest.raises(BackupCancelled):
        restore_project(backup, tmp_path / 'cancelled-copy', expected_manifest_sha256=digest, cancelled=after_start)
    assert not (tmp_path / 'cancelled-copy').exists()


def test_interrupted_backup_is_not_a_complete_bundle(rendered, tmp_path, monkeypatch):
    source, *_ = rendered
    target = tmp_path / 'partial-backup'
    link = os.link
    def fail_completion(source, destination, *args, **kwargs):
        if Path(destination) == target / 'manifest.json':
            raise OSError('Injected publication failure')
        return link(source, destination, *args, **kwargs)
    monkeypatch.setattr('carr_library.studio.backup.os.link', fail_completion)
    with pytest.raises(OSError, match='Injected'):
        backup_project(source, target)
    assert target.is_dir() and not (target / 'manifest.json').exists()
    with pytest.raises((OSError, BackupError)):
        restore_project(target, tmp_path / 'partial-refused', expected_manifest_sha256='0' * 64)


def test_interrupted_restore_keeps_guard_and_can_be_retried_to_a_fresh_path(bundle, tmp_path, monkeypatch):
    backup, digest = bundle
    target = tmp_path / 'partial-restore'
    link = os.link
    def fail_database(source, destination, *args, **kwargs):
        if Path(destination) == target / 'studio.sqlite':
            raise OSError('Injected restore publication failure')
        return link(source, destination, *args, **kwargs)
    monkeypatch.setattr('carr_library.studio.backup.os.link', fail_database)
    with pytest.raises(OSError, match='Injected'):
        restore_project(backup, target, expected_manifest_sha256=digest)
    assert (target / '.restore-incomplete').is_file()
    with pytest.raises(ValueError, match='incomplete'):
        ProjectStore(target)
    with pytest.raises(FileExistsError):
        restore_project(backup, target, expected_manifest_sha256=digest)
    recovered = restore_project(backup, tmp_path / 'fresh-retry', expected_manifest_sha256=digest)
    assert recovered.load().scenes


def test_manifest_digest_is_required_and_bound_to_exact_bytes(bundle, tmp_path):
    backup, digest = bundle
    for wrong in (None, True, 'not-a-hash', '0' * 64):
        with pytest.raises(BackupError):
            restore_project(backup, tmp_path / 'bad-trust', expected_manifest_sha256=wrong)
    raw = (backup / 'manifest.json').read_bytes()
    (backup / 'manifest.json').write_bytes(raw + b' ')
    with pytest.raises(BackupError, match='checksum'):
        restore_project(backup, tmp_path / 'different-manifest', expected_manifest_sha256=digest)
