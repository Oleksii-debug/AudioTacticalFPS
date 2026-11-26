"""Independent regression review of local jobs, cache evidence and tool scopes.

PCM fixtures are real files. Controlled renderers isolate durable orchestration
from the separately tested installed speech and FFmpeg backends.
"""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import wave

import pytest

from carr_library.agent.runtime import AgentError, LocalAgent, Plan, Step
from carr_library.application import LibraryApp
from carr_library.studio import Budget, Clip, Line, PlanError, Project, ProjectStore, Role, Scene
from carr_library.studio.director import Director, RenderCancelled, RenderPaused
from carr_library.studio.store import atomic_text, digest_file


KEY = 'a' * 64


def pcm(path, *, frames=2400, rate=24000, channels=1):
    path = Path(path)
    with wave.open(str(path), 'wb') as out:
        out.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * frames * channels)
    return dict(path=str(path), frames=frames, sample_rate=rate, channels=channels,
                duration_seconds=frames / rate, sha256=digest_file(path), peak=0.0)


class ControlledAudio:
    max_tracks = 128
    max_output_bytes = None

    def engine_fingerprint(self, *, cancelled=None):
        if cancelled:
            cancelled()
        return 'controlled-pcm-fixture-v1'

    def render_timeline(self, tracks, destination, *, duration, sample_rate=24000,
                        channels=2, cancelled=None, **options):
        if cancelled:
            cancelled()
        return pcm(destination, frames=round(duration * sample_rate),
                   rate=sample_rate, channels=channels)

    def concatenate_segments(self, paths, destination, *, sample_rate=24000,
                             channels=2, cancelled=None, **options):
        if cancelled:
            cancelled()
        frames = 0
        for path in paths:
            with wave.open(str(path), 'rb') as source:
                frames += round(source.getnframes() * sample_rate / source.getframerate())
        return pcm(destination, frames=frames, rate=sample_rate, channels=channels)


def renderer(store):
    calls = []

    def speak(request, path, *, cancelled=None):
        if cancelled:
            cancelled()
        calls.append(request.text)
        return {**pcm(path), 'engine_fingerprint': 'controlled-v1',
                'voice_id': request.voice_id, 'engine': 'controlled-test'}

    return Director(store, audio=ControlledAudio(), speech_renderer=speak,
                    engine_identity=lambda voice: 'controlled-v1'), calls


def narration():
    return Project('Reviewed source', (Role('reader', 'controlled-voice'),),
                   (Scene('first', 'First', (Line('a', 'reader', 'First sentence.'),
                                             Line('b', 'reader', 'Second sentence.'))),))


def test_revision_roundtrip_and_stale_editor_never_replace_current(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    first = narration()
    revision = store.save(first)
    second = replace(first, title='A deliberate edited title')
    store.save(second, expected_revision=revision)
    with pytest.raises(PlanError, match='changed'):
        store.save(replace(first, title='Stale editor'), expected_revision=revision)
    assert store.load() == second
    assert store.load(revision) == first


@pytest.mark.parametrize('field,value', [
    ('sha256', 'b' * 64), ('frames', 2401), ('frames', True),
    ('sample_rate', 48000), ('channels', 2), ('duration_seconds', .2),
    ('duration_seconds', float('nan')), ('duration_seconds', '0.1'),
])
def test_cache_rejects_report_corruption_even_if_pcm_bytes_are_unchanged(tmp_path, field, value):
    store = ProjectStore(tmp_path / 'project')
    path = store.new_chunk_path(KEY)
    report = pcm(path)
    store.accept_chunk(KEY, path, report)
    before = path.read_bytes()
    changed = {**report, field: value}
    with store.connect() as db:
        db.execute('UPDATE chunks SET report=? WHERE cache_key=?', (json.dumps(changed), KEY))
    assert store.cached(KEY) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('report', ['null', '[]', '{', '{"sha256":true}'])
def test_malformed_cache_record_is_a_cache_miss(tmp_path, report):
    store = ProjectStore(tmp_path / 'project')
    path = store.new_chunk_path(KEY)
    store.accept_chunk(KEY, path, pcm(path))
    with store.connect() as db:
        db.execute('UPDATE chunks SET report=? WHERE cache_key=?', (report, KEY))
    assert store.cached(KEY) is None


def test_cache_accept_rejects_false_audio_metadata_before_checkpoint(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    path = store.new_chunk_path(KEY)
    report = pcm(path)
    with pytest.raises(PlanError, match='metadata|PCM|report'):
        store.accept_chunk(KEY, path, {**report, 'frames': 2401})
    assert store.cached(KEY) is None


def test_snapshot_cache_and_pcm_corruption_are_checked_without_overwriting(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    path = store.cache / 'selected-input.mp3'
    path.write_bytes(b'opaque prepared input bytes')
    report = dict(path=str(path), sha256=digest_file(path), size_bytes=path.stat().st_size)
    store.accept_chunk(KEY, path, report)
    assert store.cached(KEY) == report
    path.write_bytes(b'a different input of equal length')
    assert store.cached(KEY) is None
    assert path.read_bytes() == b'a different input of equal length'


def test_cached_row_cannot_escape_its_project_even_with_matching_digest(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    outside = tmp_path / 'outside.wav'
    report = pcm(outside)
    with store.connect() as db:
        db.execute('INSERT INTO chunks VALUES(?,?,?,?)',
                   (KEY, str(outside), report['sha256'], json.dumps(report)))
    assert store.cached(KEY) is None
    with pytest.raises(PlanError, match='inside'):
        store.accept_chunk(KEY, outside, report)


@pytest.mark.parametrize('key', ['../escape', 'x/../../escape', '/tmp/escape', '', 'a' * 63])
def test_chunk_keys_never_form_paths_outside_the_cache(tmp_path, key):
    store = ProjectStore(tmp_path / 'project')
    with pytest.raises(PlanError, match='key'):
        store.new_chunk_path(key)


def test_atomic_new_output_preserves_existing_user_bytes(tmp_path):
    path = tmp_path / 'original.txt'
    path.write_text('original reviewed source', encoding='utf-8')
    with pytest.raises(FileExistsError):
        atomic_text(path, 'unapproved replacement')
    assert path.read_text(encoding='utf-8') == 'original reviewed source'
    assert not list(tmp_path.glob('.studio-*'))


@pytest.mark.parametrize('exception,flag', [(RenderPaused, 'paused'), (RenderCancelled, 'cancelled')])
def test_stopped_job_resumes_verified_chunks_without_repeating_speech(tmp_path, exception, flag):
    store = ProjectStore(tmp_path / 'project')
    director, calls = renderer(store)
    with pytest.raises(exception):
        director.render(narration(), **{flag: lambda: len(calls) == 1})
    assert calls == ['First sentence.']
    assert store.jobs()[0]['state'] == ('paused' if flag == 'paused' else 'cancelled')
    assert not list(store.renders.rglob('master.wav'))
    result = director.render(narration())
    assert calls == ['First sentence.', 'Second sentence.']
    assert result['completed_chunks'] == 1
    assert result['reused_chunks'] >= 1
    assert store.jobs()[0]['state'] == 'complete'
    assert digest_file(result['master']['path']) == result['master']['sha256']


def test_conflicting_completed_master_is_retained_instead_of_overwritten(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    director, calls = renderer(store)
    result = director.render(narration())
    master = Path(result['master']['path'])
    master.write_bytes(b'preserve a subsequently changed output')
    with pytest.raises(PlanError, match='Existing master'):
        director.render(narration())
    assert master.read_bytes() == b'preserve a subsequently changed output'
    assert len(calls) == 2


def test_external_prepared_audio_is_rejected_before_copy_or_decode(tmp_path):
    store = ProjectStore(tmp_path / 'project')
    director, calls = renderer(store)
    allowed = tmp_path / 'approved-inputs'
    allowed.mkdir()
    outside = tmp_path / 'private.wav'
    pcm(outside)
    before = outside.read_bytes()
    project = Project('Outside scope', (), (Scene('one', 'One', clips=(Clip('bed', str(outside)),)),))
    with pytest.raises(PlanError, match='outside'):
        director.render(project, asset_root=allowed, allowed_audio_roots=(allowed,))
    assert outside.read_bytes() == before
    assert not tuple(store.cache.iterdir())
    assert calls == []


def test_approved_plan_is_immutable_during_callback_reentry(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    arguments = {'name': 'Reviewed library name'}
    plan = Plan('Create the reviewed library', (Step('create', 'library.create', arguments),))
    approval = plan.digest
    agent = LocalAgent(app, tmp_path / 'journal', allow_create=True)

    def changed_after_approval():
        arguments['name'] = 'Name never included in the approved digest'
        return False

    result = agent.run(plan, approval_sha256=approval, cancelled=changed_after_approval)
    assert [w.name for w in app.workspaces()] == ['Reviewed library name']
    assert result['plan_sha256'] == approval
    with sqlite3.connect(agent.database) as db:
        assert {row[0] for row in db.execute('SELECT plan FROM receipts')} == {approval}


def test_changed_plan_before_run_requires_fresh_approval(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    args = {'name': 'Reviewed'}
    plan = Plan('Create', (Step('create', 'library.create', args),))
    approval = plan.digest
    args['name'] = 'Changed before execution'
    with pytest.raises(AgentError, match='exact plan'):
        LocalAgent(app, tmp_path / 'journal', allow_create=True).run(plan, approval_sha256=approval)
    assert app.workspaces() == ()


@pytest.mark.parametrize('archived', [False, True])
def test_backup_does_not_include_a_library_outside_plan_scope(tmp_path, archived):
    app = LibraryApp(tmp_path / 'library')
    selected = app.create_workspace('Selected')
    secret = app.create_workspace('Outside the selected scope')
    if archived:
        app.archive_workspace(secret)
    destination = tmp_path / 'backup'
    plan = Plan('Backup the selected scope', (Step('backup', 'library.backup', {'destination': str(destination)}),))
    agent = LocalAgent(app, tmp_path / 'journal', output_roots=(tmp_path,), workspaces=(selected,))
    with pytest.raises(AgentError, match='all|scope'):
        agent.run(plan, approval_sha256=plan.digest)
    assert not destination.exists()


def test_new_library_between_preflight_and_snapshot_cannot_enter_backup(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    selected = app.create_workspace('Initially all libraries')
    destination = tmp_path / 'backup'
    plan = Plan('Backup approved libraries', (Step('backup', 'library.backup', {'destination': str(destination)}),))
    agent = LocalAgent(app, tmp_path / 'journal', output_roots=(tmp_path,), workspaces=(selected,))
    preflight = agent._preflight

    def concurrent_library(tool, args):
        result = preflight(tool, args)
        app.create_workspace('New private library created during execution')
        return result

    monkeypatch.setattr(agent, '_preflight', concurrent_library)
    with pytest.raises(ValueError, match='scope|approved'):
        agent.run(plan, approval_sha256=plan.digest)
    assert not destination.exists()


def test_full_scope_backup_includes_explicitly_approved_archived_libraries(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = app.create_workspace('Selected')
    archived = app.create_workspace('Approved archive')
    app.archive_workspace(archived)
    destination = tmp_path / 'backup'
    plan = Plan('Backup all approved libraries', (Step('backup', 'library.backup', {'destination': str(destination)}),))
    agent = LocalAgent(app, tmp_path / 'journal', output_roots=(tmp_path,), workspaces=(selected, archived))
    result = agent.run(plan, approval_sha256=plan.digest)
    assert result['state'] == 'complete'
    assert digest_file(destination / 'manifest.json') == result['results']['backup']['manifest_sha256']
    with sqlite3.connect(destination / 'library.sqlite') as db:
        assert {row[0] for row in db.execute('SELECT id FROM workspace')} == {selected, archived}


def test_receipt_reuse_cannot_reveal_a_now_unselected_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    first = app.create_workspace('First selected library')
    second = app.create_workspace('Previously selected private library')
    plan = Plan('List selected libraries', (Step('list', 'library.list', {}),))
    agent = LocalAgent(app, tmp_path / 'journal', workspaces=(first, second))
    result = agent.run(plan, approval_sha256=plan.digest)
    assert len(result['results']['list']['libraries']) == 2
    narrower = LocalAgent(app, tmp_path / 'journal', workspaces=(first,))
    result = narrower.run(plan, approval_sha256=plan.digest)
    assert [w['id'] for w in result['results']['list']['libraries']] == [first]


def test_search_receipts_refresh_source_revision_and_live_selection_after_restart(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = app.create_workspace('Selected library')
    source = app.add_text(selected, 'Original title', 'An exact source quotation.')
    plan = Plan('Find source evidence', (Step('find', 'library.search',
                {'workspace': selected, 'query': 'exact source quotation'}),))
    agent = LocalAgent(app, tmp_path / 'journal', workspaces=(selected,))
    original = agent.run(plan, approval_sha256=plan.digest)['results']['find']['results'][0]
    app.update_metadata(selected, source['edition'], title='Corrected source title')
    reopened = LibraryApp(app.root)
    resumed = LocalAgent(reopened, tmp_path / 'journal', workspaces=(selected,))
    found = resumed.run(plan, approval_sha256=plan.digest)['results']['find']['results'][0]
    assert found['title'] == 'Corrected source title'
    assert found['revision'] != original['revision']
    assert reopened.copy(selected, found['ref'], found['revision'])


def test_output_parent_component_cannot_escape_the_approved_root(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    approved = tmp_path / 'approved'
    approved.mkdir()
    agent = LocalAgent(app, tmp_path / 'journal', output_roots=(approved,))
    with pytest.raises(AgentError, match='outside'):
        agent._path(str(approved / '..'), output=True)


def test_agent_rejects_parent_symlink_escape_for_output(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    approved = tmp_path / 'approved'
    approved.mkdir()
    outside = tmp_path / 'private'
    outside.mkdir()
    link = approved / 'redirect'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        assert sys.platform == 'win32' and exc.winerror == 1314
        return
    agent = LocalAgent(app, tmp_path / 'journal', output_roots=(approved,))
    with pytest.raises(AgentError, match='outside'):
        agent._path(str(link / 'unapproved.wav'), output=True)
    assert not tuple(outside.iterdir())
