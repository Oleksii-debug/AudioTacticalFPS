"""Actual audio snapshots bind the generated-output choice to the reviewed task."""
from hashlib import sha256
import json
from pathlib import Path
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.planner import backup_notice
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.runtime import available_tool
from carr_library.library_backup import restore_backup
from carr_library.ui.audio_studio import StudioController


class NoSpeech:
    def synthesize(self, *args, **kwargs):
        raise AssertionError('This test uses actual PCM/FFmpeg, never synthetic speech acceptance.')


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed backup policy')
    app.add_text(workspace, 'Retained original', 'Original source remains exact.')
    output = tmp_path / 'output'
    output.mkdir()
    scope = Scope(workspace, output_root=output, allow_full_backup=True,
                  max_bytes=8 * 1024 ** 2)
    return app, AssistantService(app), scope, output


def rendered_project(app):
    executable = available_tool('ffmpeg')
    assert executable is not None, 'A real FFmpeg executable is required.'
    studio = StudioController(private_root=app.root, registry=NoSpeech(), ffmpeg=executable)
    script = '[output sample_rate=8000 stems=false]\n[sound "source.wav"]'
    studio.new('[output sample_rate=8000 stems=false]\n[pause 0.12]',
               title='Synthetic source audio', input_kind='script')
    studio.save_in_library()
    source = studio.output_dir / 'assets' / 'source.wav'
    source.parent.mkdir()
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', 2000 if i % 20 < 10 else -2000)
                                   for i in range(960)))
    studio.edit_script(script)
    studio.save()
    result = studio.render()
    assert result['status'] == 'complete' and wav_info(result['output']).frames == 960
    studio.edit_script(script + '\n[pause 0.01]')
    recovery = studio.save_recovery()
    return studio, source, recovery, Path(result['output'])


@pytest.mark.parametrize('include_generated', [False, True], ids=['default', 'explicit-audio'])
def test_actual_backup_and_restore_preserve_reviewed_generated_choice(prepared, tmp_path, include_generated):
    app, service, scope, output = prepared
    studio, source, recovery, master = rendered_project(app)
    source_bytes, project_bytes = source.read_bytes(), studio.project_path.read_bytes()
    recovery_bytes, master_bytes = recovery.read_bytes(), master.read_bytes()
    if include_generated:
        instruction = json.dumps({'schema': 'carr-assistant-plan/1', 'steps': [
            {'tool': 'library.backup', 'arguments': {'name': 'snapshot', 'include_generated': True}}]})
    else:
        instruction = 'Backup "snapshot"'
    plan = service.preview(instruction, scope)
    assert plan.steps[0].arguments['include_generated'] is include_generated
    assert ('Generated audio is included.' if include_generated else 'Generated audio is excluded.') in backup_notice(plan)
    identity = service.submit(plan, approved_plan_sha256=plan.digest)
    report = service.run(identity)
    assert report.status == 'complete', report
    result, = report.results
    assert result['generated_audio_included'] is include_generated
    folder = output / 'snapshot'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['generated_audio_included'] is include_generated
    paths = {row['filename'] for row in manifest['sidecars']}
    for path, raw in ((source, source_bytes), (studio.project_path, project_bytes), (recovery, recovery_bytes)):
        relative = path.relative_to(app.root).as_posix()
        assert relative in paths and (folder / relative).read_bytes() == raw
    master_relative = master.relative_to(app.root)
    assert (master_relative.as_posix() in paths) is include_generated
    assert any('/cache/' in path and path.endswith('.wav') for path in paths) is include_generated
    reopened = AssistantService(app)
    with pytest.raises(PermissionError, match='Review the exact saved plan'):
        reopened.resume(identity)
    reviewed = reopened.review(identity)
    assert reviewed.digest == plan.digest and reviewed.to_dict() == plan.to_dict()
    assert reopened.resume(identity, approved_plan_sha256=reviewed.digest).status == 'complete'
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', result['manifest_sha256']))
    recovered = AssistantService(restored)
    record = recovered.store.load(identity)
    assert record['requires_reapproval'] is True
    assert record['plan'] == plan.to_dict() and record['plan_sha256'] == plan.digest
    with pytest.raises(PermissionError, match='review and submit'):
        recovered.resume(identity, approved_plan_sha256=plan.digest)
    assert (restored.root / source.relative_to(app.root)).read_bytes() == source_bytes
    assert (restored.root / recovery.relative_to(app.root)).read_bytes() == recovery_bytes
    if include_generated:
        assert (restored.root / master_relative).read_bytes() == master_bytes
    else:
        assert not (restored.root / master_relative).exists()


@pytest.mark.parametrize('invalid', [0, 1, 'true', None, [], {}])
def test_generated_choice_requires_exact_boolean_before_any_job(prepared, invalid):
    _, service, scope, output = prepared
    instruction = json.dumps({'steps': [{'tool': 'library.backup',
        'arguments': {'name': 'snapshot', 'include_generated': invalid}}]})
    with pytest.raises(ValueError, match='include_generated.*true or false'):
        service.preview(instruction, scope)
    assert service.jobs() == () and not list(output.iterdir())


def test_legacy_schema_one_plan_is_not_rewritten_when_reviewed_or_resumed(prepared):
    _, service, scope, output = prepared
    plan = Plan(scope, (Step('library.backup', {'name': 'legacy'}),), 'Original reviewed summary')
    identity = service.submit(plan)
    stored_bytes = service.store.path(identity).read_bytes()
    reopened = AssistantService(service.app)
    reviewed = reopened.review(identity)
    assert reviewed.to_dict() == plan.to_dict() and reviewed.digest == plan.digest
    assert service.store.path(identity).read_bytes() == stored_bytes
    assert 'include_generated' not in reviewed.steps[0].arguments
    assert 'Generated audio is excluded.' in backup_notice(reviewed)
    report = reopened.resume(identity, approved_plan_sha256=reviewed.digest)
    assert report.status == 'complete', report
    stored = reopened.store.load(identity)
    assert stored['plan'] == plan.to_dict() and stored['plan_sha256'] == plan.digest
    assert json.loads((output / 'legacy' / 'manifest.json').read_bytes())['generated_audio_included'] is False


def test_changed_generated_choice_cannot_reuse_previous_review(prepared):
    _, service, scope, output = prepared
    plan = service.preview('Backup "snapshot"', scope)
    identity = service.submit(plan)
    changed = service.store.load(identity)
    changed['plan']['steps'][0]['arguments']['include_generated'] = True
    changed['plan_sha256'] = sha256(canonical(changed['plan']).encode('utf-8')).hexdigest()
    service.store.save(changed)
    with pytest.raises(PermissionError, match='Review the exact saved plan'):
        service.run(identity)
    reopened = AssistantService(service.app)
    with pytest.raises(PermissionError, match='Review the exact saved plan'):
        reopened.resume(identity, approved_plan_sha256=plan.digest)
    assert not list(output.iterdir())


def test_actual_producer_policy_mismatch_cannot_publish_or_relabel_snapshot(prepared, monkeypatch):
    app, service, scope, output = prepared
    plan = service.preview('Backup "snapshot"', scope)
    identity = service.submit(plan)
    original = app.backup
    def inconsistent_producer(destination, **kwargs):
        assert kwargs['include_generated'] is False and kwargs['max_bytes'] == scope.max_bytes
        assert callable(kwargs['cancelled'])
        return original(destination, **{**kwargs, 'include_generated': True})
    monkeypatch.setattr(app, 'backup', inconsistent_producer)
    report = service.run(identity)
    assert report.status == 'failed' and 'manifest differs' in report.message
    assert not list(output.iterdir())
