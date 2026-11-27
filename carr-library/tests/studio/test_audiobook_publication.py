"""Actual Studio M4B, measured chapters and existing durable completion reuse."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from carr_library.audio.audiobook import inspect_chapters, line_lrc
from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.studio.project import parse_script
from carr_library.studio.render import ProductionService


def test_actual_studio_m4b_embeds_measured_chapters_and_lrc(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    provider = NativeSapiProvider() if sys.platform == 'win32' else FliteProvider(shutil.which('ffmpeg'))
    voices = VoiceRegistry([provider])
    installed = voices.voices()
    assert installed, 'Actual native SAPI or Flite speech is required: ' + '; '.join(voices.notices)
    selected = (next((voice for voice in installed if voice.language.casefold().startswith('en')), installed[0]).id
                if sys.platform == 'win32' else 'flite:awb')
    assert selected in {voice.id for voice in installed}, 'The actual test voice is required.'
    output = tmp_path / 'output'; output.mkdir()
    service = ProductionService(tmp_path / 'state', voices=voices)
    original = '[Scene: First]\nReader: Preserve this first line.\n[Scene: Second]\nReader: Preserve this final line.'
    project = parse_script(original, title='Measured chapters', voice_ids=(selected,), mode='authored')
    try:
        job = service.submit(project, output, format='m4b', stems=False)
        result = service.run(job)
        target = Path(result['destination'])
        runtime = service._runtime(reviewed_roots=(target,))
        actual = inspect_chapters(runtime, target / 'audiobook.m4b')
        chapters = result['result']['chapters']
        assert result['status'] == 'complete' and len(chapters) == 2
        assert [item['tags']['title'] for item in actual['chapters']] == ['First', 'Second']
        for embedded, measured in zip(actual['chapters'], chapters):
            assert abs(float(embedded['start_time']) - measured['start']) <= .001
            assert abs(float(embedded['end_time']) - measured['end']) <= .001
        assert (target / 'script.lrc').read_text() == line_lrc(result['result']['lines'])
        assert project.original_script == original
        receipts = {item['path']: item['sha256'] for item in result['artifacts']}
        assert str(target / 'script.lrc') in receipts
        assert all(sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in receipts.items())
        service.runtime_factory = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Completed job relaunched'))
        assert service.run(job)['artifacts'] == result['artifacts']
    finally:
        service.close(); voices.close()


def silent_project():
    # Explicit measured silence avoids an unrelated voice/model dependency in
    # the recovery tests. The retained test above exercises actual local speech.
    return parse_script('[Scene: First]\n[Duration: 0.6]\n[Scene: Second]\n[Duration: 0.9]',
                        title='Silent recovery fixture', mode='authored')


def test_m4b_writer_warning_is_saved_without_changing_completed_job(tmp_path, monkeypatch):
    import carr_library.audio.audiobook as module
    from carr_library.durable_state import read_json
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    output = tmp_path / 'output'; output.mkdir()
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state' / 'studio')
    job = service.submit(silent_project(), output, format='m4b', stems=False)
    publish = module.publish_file

    def interrupted(staged, target, **options):
        publish(staged, target, **options)
        raise OSError('Actual M4B committed before injected writer interruption')

    monkeypatch.setattr(module, 'publish_file', interrupted)
    try:
        result = service.run(job)
        assert result['status'] == 'complete'
        assert any('atomically published' in value for value in result['warnings'])
        saved = read_json(service.jobs_root / job / 'job.json')
        assert saved['status'] == 'complete' and saved['warnings'] == result['warnings']
        assert service.broker.snapshot()['leases'] == []
        target = Path(result['destination'])
        assert (target / 'script.lrc').read_bytes() == b''
        expected = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                    for row in result['artifacts']}
        assert all(expected[row['path']] == row['sha256'] for row in result['artifacts'])
        fresh = ProductionService(service.root,
            runtime_factory=lambda *args, **kwargs: pytest.fail('Completed M4B job must not re-render'))
        try:
            assert fresh.run(job) == saved
            assert expected == {path: sha256(Path(path).read_bytes()).hexdigest() for path in expected}
        finally:
            fresh.close()
        original_job = (service.jobs_root / job / 'job.json').read_bytes()
        backup = tmp_path / 'warning-backup'
        checksum = create_backup(app, backup)
        manifest = read_json(backup / 'manifest.json')
        relative = 'state/studio/jobs/' + job + '/job.json'
        assert manifest['version'] == 4
        assert relative in {row['filename'] for row in manifest['sidecars']}
        assert (backup / relative).read_bytes() == original_job
        restored_root = restore_backup(backup, tmp_path / 'warning-restored', checksum)
        restored = ProductionService(restored_root / 'state' / 'studio')
        try:
            pending = restored.get(job)
            assert pending['status'] == 'waiting-input' and pending['destination'] == ''
            assert pending['warnings'] == result['warnings']
            assert pending['artifact_version'] == 2
            assert not list(restored_root.rglob('*.m4b'))
        finally:
            restored.close()
        assert (service.jobs_root / job / 'job.json').read_bytes() == original_job
        assert expected == {path: sha256(Path(path).read_bytes()).hexdigest() for path in expected}
    finally:
        service.close()


def test_equal_pcm_with_changed_titles_gets_distinct_verified_m4b_cache(tmp_path, monkeypatch):
    from carr_library.audio import AudioRuntime
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    output = tmp_path / 'output'; output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio')
    original = AudioRuntime.audiobook
    observed = []

    def actual_publication(runtime, source, target, **options):
        observed.append({'source': sha256(Path(source).read_bytes()).hexdigest(),
                         'target': str(target), 'title': options['title'],
                         'chapters': options['chapters']})
        return original(runtime, source, target, **options)

    monkeypatch.setattr(AudioRuntime, 'audiobook', actual_publication)
    first = silent_project()
    second = replace(first, id='new-project', revision=2, title='A changed audiobook title',
                     scenes=(replace(first.scenes[0], title='Changed first chapter'), first.scenes[1]))
    try:
        complete = [service.run(service.submit(project, output, format='m4b', stems=False))
                    for project in (first, second)]
        assert len(observed) == 2 and observed[0]['source'] == observed[1]['source']
        assert observed[0]['target'] != observed[1]['target']
        for project, result in zip((first, second), complete):
            destination = Path(result['destination'])
            runtime = service._runtime(reviewed_roots=(destination,))
            actual = inspect_chapters(runtime, destination / 'audiobook.m4b')
            assert actual['format']['tags']['title'] == project.title
            assert [row['tags']['title'] for row in actual['chapters']] == [
                scene.title for scene in project.scenes]
    finally:
        service.close()


@pytest.mark.parametrize('boundary', ('before-link', 'after-link'))
def test_actual_studio_exit_resumes_without_adopting_unreceipted_m4b_cache(
        tmp_path, monkeypatch, boundary):
    from carr_library.audio import AudioRuntime
    import carr_library.audio.audiobook as module
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    output = tmp_path / 'output'; output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio')
    job = service.submit(silent_project(), output, format='m4b', stems=False)
    marker = tmp_path / 'studio-audiobook-child.json'
    child = r'''
import json, os, sys
from pathlib import Path
from carr_library.audio import audiobook
from carr_library.studio.render import ProductionService
root, identity, marker, boundary, expected_origin = sys.argv[1:]
assert Path(audiobook.__file__).resolve() == Path(expected_origin).resolve()
publish = audiobook.publish_file
def exit_at_link(staged, target, **options):
    if boundary == 'after-link':
        publish(staged, target, **options)
        assert os.path.samefile(staged, target)
    Path(marker).write_text(json.dumps({'staged': str(staged), 'encoded': str(target)}),
                            encoding='utf-8')
    os._exit(73)
audiobook.publish_file = exit_at_link
ProductionService(root).run(identity)
raise AssertionError('The actual measured M4B publication boundary was not reached.')
'''
    process = subprocess.run([sys.executable, '-B', '-c', child, str(service.root), job,
        str(marker), boundary, str(Path(module.__file__).resolve())],
        cwd=Path(module.__file__).resolve().parents[2], capture_output=True,
        text=True, encoding='utf-8', timeout=120, check=False)
    assert process.returncode == 73, (process.returncode, process.stdout, process.stderr)
    record = json.loads(marker.read_text(encoding='utf-8'))
    encoded, staged = Path(record['encoded']), Path(record['staged'])
    receipt = encoded.with_name(encoded.stem + '-encoded.json')
    assert staged.is_file() and not receipt.exists()
    interrupted = service.get(job)
    assert interrupted['status'] != 'complete'
    assert not (Path(interrupted['destination']) / 'audiobook.m4b').exists()
    completed_before = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                        for row in interrupted['artifacts']}
    old_stage = {path.name: sha256(path.read_bytes()).hexdigest()
                 for path in staged.parent.iterdir() if path.is_file()}
    if boundary == 'after-link':
        assert encoded.stat().st_nlink == 2 and os.path.samefile(encoded, staged)
        abandoned = encoded.read_bytes()
    else:
        assert not encoded.exists()

    # Only the actual AAC conversion boundary is observed. Native processes,
    # result objects, package search paths and existing cache receipts are real.
    converted, convert = [], AudioRuntime.convert
    def observe_conversion(runtime, source, target, **options):
        if Path(target).name == 'encoded.m4b' and Path(target).parent.name.startswith('.audiobook-'):
            converted.append(Path(target))
        return convert(runtime, source, target, **options)
    monkeypatch.setattr(AudioRuntime, 'convert', observe_conversion)
    fresh = ProductionService(service.root)
    try:
        result = fresh.run(job)
        assert result['status'] == 'complete' and len(converted) == 1
        assert fresh.broker.snapshot()['leases'] == []
        assert completed_before == {
            path: sha256(Path(path).read_bytes()).hexdigest() for path in completed_before}
        assert old_stage == {path.name: sha256(path.read_bytes()).hexdigest()
                             for path in staged.parent.iterdir() if path.is_file()}
        from carr_library.durable_state import read_json
        cache_receipt = read_json(receipt)
        selected = fresh.cache / cache_receipt['file']
        assert cache_receipt['sha256'] == sha256(selected.read_bytes()).hexdigest()
        assert selected.stat().st_nlink == 1
        if boundary == 'after-link':
            assert selected != encoded and encoded.read_bytes() == abandoned
            assert os.path.samefile(encoded, staged) and encoded.stat().st_nlink == 2
        else:
            assert selected == encoded
        destination = Path(result['destination'])
        assert cache_receipt['sha256'] == sha256((destination / 'audiobook.m4b').read_bytes()).hexdigest()
        assert (destination / 'script.lrc').read_bytes() == b''
        for row in result['artifacts']:
            assert sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256']
    finally:
        fresh.close()
        service.close()


@pytest.mark.parametrize('legacy_version', (None, 1))
def test_completed_legacy_job_without_lrc_is_not_upgraded_on_read(tmp_path, legacy_version):
    from carr_library.durable_state import atomic_json, read_json
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    output = tmp_path / 'output'; output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio')
    job = service.submit(silent_project(), output, stems=False)
    try:
        result = service.run(job)
        destination = Path(result['destination'])
        lrc = destination / 'script.lrc'
        # Exact historical schema1 completed-job shape: LRC did not exist in its
        # artifact graph. Reading it must not manufacture or amend an export.
        if legacy_version is None:
            del result['artifact_version']
        else:
            result['artifact_version'] = legacy_version
        result['artifacts'] = [row for row in result['artifacts'] if row['path'] != str(lrc)]
        lrc.unlink()
        path = service.jobs_root / job / 'job.json'
        atomic_json(path, result, replace=True)
        original_job = path.read_bytes()
        original_outputs = {row['path']: Path(row['path']).read_bytes() for row in result['artifacts']}
        fresh = ProductionService(service.root,
            runtime_factory=lambda *args, **kwargs: pytest.fail('A legacy completed job was re-rendered'))
        try:
            assert fresh.get(job) == read_json(path)
            assert fresh.run(job) == read_json(path)
            assert not lrc.exists() and path.read_bytes() == original_job
            assert original_outputs == {name: Path(name).read_bytes() for name in original_outputs}
        finally:
            fresh.close()
    finally:
        service.close()


def test_production_runtime_identity_binds_measured_audiobook_implementation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.speech import _io
    from carr_library.studio.render import production_runtime_identity
    runtime = SimpleNamespace(ffmpeg=tmp_path / 'synthetic-ffmpeg', ffprobe=tmp_path / 'synthetic-ffprobe')
    runtime.ffmpeg.write_bytes(b'Never executed runtime identity fixture')
    runtime.ffprobe.write_bytes(b'Never executed runtime identity fixture')
    actual, calls = _io.digest, []
    changed = False

    def digest(path, **options):
        value = actual(path, **options)
        if Path(path).name == 'audiobook.py':
            calls.append(Path(path))
            return 'f' * 64 if changed else value
        return value

    monkeypatch.setattr(_io, 'digest', digest)
    first = production_runtime_identity(runtime, {'version': 'same inert fixture'})
    changed = True
    second = production_runtime_identity(runtime, {'version': 'same inert fixture'})
    assert len(calls) == 2 and first != second


def test_failed_encoded_receipt_resume_keeps_warning_history_bounded_and_restorable(tmp_path, monkeypatch):
    import carr_library.audio.audiobook as audiobook
    import carr_library.studio.render as render
    from carr_library.application import LibraryApp
    from carr_library.durable_state import atomic_json, read_json
    from carr_library.library_backup import create_backup, restore_backup

    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    service = ProductionService(app.root / 'state' / 'studio')
    identity = service.submit(silent_project(), output, format='m4b', stems=False)
    checkpoint = service.jobs_root / identity / 'job.json'
    state = service.get(identity)
    original_history = [f'Historical warning {number}' for number in range(999)]
    state['warnings'] = original_history.copy()
    atomic_json(checkpoint, state, replace=True)
    real_publish, real_write = audiobook.publish_file, render.atomic_json
    current = ['writer-warning-A']
    emitted, receipt_failures, snapshots = [], [], []

    def published_then_warning(staged, target, **options):
        real_publish(staged, target, **options)
        raise OSError(current[0])

    def fail_encoded_receipt(path, value, **options):
        if Path(path).name.endswith('-encoded.json'):
            receipt_failures.append(str(path))
            raise OSError('Injected encoded-cache receipt failure')
        return real_write(path, value, **options)

    monkeypatch.setattr(audiobook, 'publish_file', published_then_warning)
    monkeypatch.setattr(render, 'atomic_json', fail_encoded_receipt)
    try:
        for message in ('writer-warning-A', 'writer-warning-A', 'writer-warning-B', 'writer-warning-B'):
            current[0] = message
            with pytest.raises(OSError, match='encoded-cache receipt failure'):
                service.run(identity, progress=emitted.append)
            saved = read_json(checkpoint)
            assert saved['status'] == 'failed'
            assert len(saved['warnings']) == 1000
            assert saved['warnings'][:999] == original_history
            assert all(isinstance(row, str) and 0 < len(row) <= 1800 for row in saved['warnings'])
            snapshots.append(saved['warnings'].copy())
            assert service.broker.snapshot()['leases'] == []
        assert len(receipt_failures) == 4
        assert snapshots[0] == snapshots[1]
        assert snapshots[2] == snapshots[3]
        assert 'writer-warning-A' in snapshots[0][-1]
        assert 'omitted' in snapshots[2][-1] and 'writer-warning-B' in snapshots[2][-1]
        assert sum('writer-warning-A' in row for row in emitted) == 2
        assert sum('omitted' in row and 'writer-warning-B' in row for row in emitted) == 2

        saved_bytes = checkpoint.read_bytes()
        backup = tmp_path / 'bounded-warning-backup'
        checksum = create_backup(app, backup)
        relative = 'state/studio/jobs/' + identity + '/job.json'
        assert (backup / relative).read_bytes() == saved_bytes
        restored_root = restore_backup(backup, tmp_path / 'restored-warning-library', checksum)
        restored = ProductionService(restored_root / 'state' / 'studio')
        try:
            recovered = restored.get(identity)
            assert recovered['status'] == 'waiting-input' and recovered['destination'] == ''
            assert recovered['warnings'] == snapshots[-1] and recovered['artifact_version'] == 2
        finally:
            restored.close()
        assert checkpoint.read_bytes() == saved_bytes
    finally:
        service.close()
