from hashlib import sha256
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.production import LocalProduction
from carr_library.audio.engines import default_registry
from carr_library.audio.ffmpeg import wav_info


def test_actual_local_audio_is_literal_and_resumes_only_verified_artifacts(tmp_path, monkeypatch):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('A real FFmpeg runtime is required.')
    registry = default_registry(ffmpeg=ffmpeg)
    available = [v for v in registry.voices() if v.engine == 'flite']
    if not available:
        pytest.skip('This real speech test requires installed FFmpeg Flite.')
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    producer = LocalProduction(app.root, registry=registry, ffmpeg=ffmpeg)
    service = AssistantService(app, production=producer)
    source = 'Narrator: This label remains spoken.\n[pause 2]\nDo not change this text.'
    args = {'text': source, 'voice_ids': [available[0].id], 'stems': False}
    scope = Scope(output_root=output, max_bytes=64 * 1024 ** 2)
    plan = producer.plan(args, max_bytes=scope.max_bytes)
    assert plan.input_kind == 'narration' and plan.original_script == source
    identity = service.submit(Plan(scope, (Step('audio.produce', args),)))
    report = service.run(identity)
    assert report.status == 'complete', report
    result = report.results[0]
    assert result['source_sha256'] == sha256(source.encode('utf-8')).hexdigest()
    assert wav_info(result['output']).seconds > .1
    assert len(list(output.glob('audio-*'))) == 1
    monkeypatch.setattr(registry, 'synthesize', lambda *a, **k: pytest.fail('Completed speech was repeated.'))
    assert service.resume(identity).status == 'complete'
    master = Path(result['output'])
    with master.open('r+b') as stream:
        stream.seek(-2, 2)
        before = stream.read(2)
        stream.seek(-2, 2)
        stream.write(bytes((before[0] ^ 1, before[1])))
    failed = service.resume(identity)
    assert failed.status == 'failed' and failed.completed == 0


def test_script_interpretation_requires_explicit_json_mode_or_typed_project(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    producer = LocalProduction(app.root, registry=type('Registry', (), {'voices': lambda self: ()})())
    text = '[pause 2]\nThis is the first spoken line.'
    literal = producer.plan({'text': text, 'language': 'en'}, max_bytes=2 * 1024 ** 2)
    directed = producer.plan({'text': text, 'language': 'en', 'input_kind': 'script'}, max_bytes=2 * 1024 ** 2)
    assert literal.input_kind == 'narration' and directed.input_kind == 'script'
    assert directed.scenes[0].events[0].seconds == 2
    assert literal.scenes[0].events[0].text.startswith('[pause 2]')
    restored = producer.plan({'script': directed.to_dict()}, max_bytes=3 * 1024 ** 2)
    assert restored.disk_budget_bytes == directed.disk_budget_bytes
    service = AssistantService(app, production=producer)
    with pytest.raises(ValueError, match='Invalid arguments'):
        service.validate(Plan(Scope(output_root=tmp_path), (
            Step('audio.produce', {'text': 'No secret controls', '_artifact_budget_bytes': 2 ** 50}),)))


def test_full_backup_resume_checks_database_and_all_sidecar_bytes(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('One')
    app.add_text(workspace, 'Original', 'Original source remains unchanged.')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    scope = Scope(workspace, output_root=output, allow_full_backup=True, max_bytes=4 * 1024 ** 2)
    identity = service.submit(service.preview('Backup "complete"', scope))
    report = service.run(identity)
    assert report.status == 'complete', report
    receipt = report.results[0]['backup_receipt']
    assert receipt['files'] >= 2 and receipt['total_bytes'] > 1000
    assert service.resume(identity).status == 'complete'
    database = output / 'complete' / 'library.sqlite'
    with database.open('r+b') as stream:
        stream.seek(-1, 2)
        byte = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([byte[0] ^ 1]))
    rejected = service.resume(identity)
    assert rejected.status == 'failed' and rejected.completed == 0
    assert 'completed backup changed' in rejected.message


def test_backup_over_budget_does_not_publish_destination(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    scope = Scope(output_root=output, allow_full_backup=True, max_bytes=1024)
    identity = service.submit(service.preview('Backup "too-large"', scope))
    report = service.run(identity)
    assert report.status == 'failed' and not (output / 'too-large').exists()
    assert list(output.iterdir()) == []
