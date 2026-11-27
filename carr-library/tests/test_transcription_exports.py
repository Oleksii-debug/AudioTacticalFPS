"""Real retained-data export/commit boundaries, without Whisper inference."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from carr_library.audio import jobs
from carr_library.transcription_exports import export_results, PublishedExportError
from tests.test_transcription_service import prepared_environment, real_protocol_child


def complete(prepared_environment):
    service, manifest, source, settings = prepared_environment(settings={'output_srt': True, 'output_json': True})
    identity = service.prepare([source], manifest, settings)
    result = service.run(identity, manifest, timeout=10)
    return service, manifest, source, identity, result


def test_exact_saved_outputs_export_offline_as_independent_files(
        prepared_environment, real_protocol_child, tmp_path):
    service, manifest, source, identity, summary = complete(prepared_environment)
    source.unlink()
    shutil.rmtree(manifest.parent)
    target = tmp_path / 'Транскрипти ü'
    result = export_results(service, identity, target)
    assert result['status'] == 'complete' and result['historical']
    receipt = jobs.read_json(target / 'transcription-export.json', limit=16 * 1024**2)
    assert receipt['completion_sha256'] == service.completed(identity)['completion_sha256']
    assert receipt['runtime_current_verified'] is False
    for row in receipt['outputs']:
        output = target / 'files' / row['path']
        original = Path(summary['results_root']) / row['path']
        assert output.read_bytes() == original.read_bytes()
        assert hashlib.sha256(output.read_bytes()).hexdigest() == row['sha256']
        assert output.stat().st_nlink == original.stat().st_nlink == 1
        output.write_bytes(b'Edited export, never the accepted transcript')
        assert hashlib.sha256(original.read_bytes()).hexdigest() == row['sha256']


def test_single_format_selection_and_missing_format_refusal(
        prepared_environment, real_protocol_child, tmp_path):
    service, _, _, identity, _ = complete(prepared_environment)
    result = export_results(service, identity, tmp_path / 'only-srt', formats=['srt'])
    assert result['files'] == 1
    assert {row['format'] for row in jobs.read_json(
        tmp_path / 'only-srt/transcription-export.json')['outputs']} == {'srt'}
    with pytest.raises(ValueError, match='absent'):
        export_results(service, identity, tmp_path / 'missing-vtt', formats=['vtt'])
    assert not (tmp_path / 'missing-vtt').exists()


def test_existing_destination_is_refused_before_verifying_or_reading_transcripts(
        prepared_environment, tmp_path, monkeypatch):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    target = tmp_path / 'owned-by-user'
    target.mkdir()
    sentinel = target / 'keep.txt'
    sentinel.write_bytes(b'Keep exactly')
    monkeypatch.setattr(service, 'completed', lambda *a, **k: pytest.fail('No output admission'))
    with pytest.raises(FileExistsError):
        export_results(service, identity, target)
    assert sentinel.read_bytes() == b'Keep exactly'


def test_changed_transcript_cannot_export_as_accepted(prepared_environment, real_protocol_child, tmp_path):
    service, _, _, identity, summary = complete(prepared_environment)
    path = Path(summary['results_root']) / summary['outputs'][0]['path']
    path.write_bytes(b'Unaccepted replacement')
    with pytest.raises((ValueError, RuntimeError)):
        export_results(service, identity, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_copy_cancel_removes_only_owned_stage(prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    target = tmp_path / 'cancelled'
    original = jobs.publish_new
    stopped = [False]
    def copy_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped[0] = True
        return result
    monkeypatch.setattr(jobs, 'publish_new', copy_then_stop)
    with pytest.raises(InterruptedError):
        export_results(service, identity, target, cancelled=lambda: stopped[0])
    assert not target.exists() and not list(tmp_path.glob('.transcription-export-*'))
    assert service.completed(identity)['outputs']


def test_cancel_during_final_directory_flush_is_before_commit(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    target = tmp_path / 'before-commit'
    original = jobs._sync_directory
    publish = jobs.publish_noreplace
    stopped, final_move = [False], [False]
    def flush_then_stop(path):
        result = original(path)
        if final_move[0] and Path(path).name.startswith('.transcription-export-'):
            stopped[0] = True
        return result
    def moving(source, destination, **kwargs):
        final_move[0] = Path(destination) == target
        try:
            return publish(source, destination, **kwargs)
        finally:
            final_move[0] = False
    monkeypatch.setattr(jobs, '_sync_directory', flush_then_stop)
    monkeypatch.setattr(jobs, 'publish_noreplace', moving)
    with pytest.raises(InterruptedError):
        export_results(service, identity, target, cancelled=lambda: stopped[0])
    assert not target.exists() and not list(tmp_path.glob('.transcription-export-*'))


def test_post_move_sync_failure_is_explicit_and_retains_published_directory(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    target = tmp_path / 'published-unconfirmed'
    original = jobs._sync_directory
    def fail_after_move(path):
        if Path(path) == target.parent and target.exists():
            raise OSError('Synthetic post-move directory sync failure')
        return original(path)
    monkeypatch.setattr(jobs, '_sync_directory', fail_after_move)
    with pytest.raises(PublishedExportError) as error:
        export_results(service, identity, target)
    assert error.value.destination == target
    receipt = jobs.read_json(target / 'transcription-export.json')
    for row in receipt['outputs']:
        assert hashlib.sha256((target / 'files' / row['path']).read_bytes()).hexdigest() == row['sha256']
    with pytest.raises(FileExistsError):
        export_results(service, identity, target)


def test_stop_after_commit_does_not_relabel_completed_export(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    target = tmp_path / 'accepted'
    stopped = [False]
    original = jobs.publish_noreplace
    def commit_then_stop(source, destination, **kwargs):
        result = original(source, destination, **kwargs)
        if Path(destination) == target:
            stopped[0] = True
        return result
    monkeypatch.setattr(jobs, 'publish_noreplace', commit_then_stop)
    result = export_results(service, identity, target, cancelled=lambda: stopped[0])
    assert result['status'] == 'complete' and stopped[0]


def test_private_library_destination_is_refused(prepared_environment):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    with pytest.raises(ValueError, match='private library'):
        export_results(service, identity, service.library_root / 'export')


def test_resource_cleanup_failure_does_not_hide_committed_export(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    from carr_library.operations import ResourceLease
    original = ResourceLease.release
    def release_then_fail(lease):
        original(lease)
        raise OSError('Synthetic cleanup failure after native release')
    monkeypatch.setattr(ResourceLease, 'release', release_then_fail)
    target = tmp_path / 'completed-despite-cleanup'
    result = export_results(service, identity, target)
    assert result['status'] == 'complete' and result['cleanup_warning']
    receipt = jobs.read_json(target / 'transcription-export.json')
    for row in receipt['outputs']:
        assert hashlib.sha256((target / 'files' / row['path']).read_bytes()).hexdigest() == row['sha256']


def test_parent_substitution_during_final_flush_cannot_redirect_publication(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    parent = tmp_path / 'chosen-parent'
    parent.mkdir()
    moved = tmp_path / 'moved-owned-parent'
    target = parent / 'requested'
    original, publish = jobs._sync_directory, jobs.publish_noreplace
    final_move = [False]
    def replace_parent_after_flush(path):
        result = original(path)
        if final_move[0] and Path(path).name.startswith('.transcription-export-'):
            parent.rename(moved)
            parent.mkdir()
            (parent / 'foreign.txt').write_bytes(b'Unrelated new parent')
        return result
    def moving(source, destination, **kwargs):
        final_move[0] = Path(destination) == target
        try:
            return publish(source, destination, **kwargs)
        finally:
            final_move[0] = False
    monkeypatch.setattr(jobs, '_sync_directory', replace_parent_after_flush)
    monkeypatch.setattr(jobs, 'publish_noreplace', moving)
    with pytest.raises(ValueError, match='parent changed'):
        export_results(service, identity, target)
    assert not target.exists()
    assert (parent / 'foreign.txt').read_bytes() == b'Unrelated new parent'
    retained, = moved.glob('.transcription-export-*')
    assert (retained / 'transcription-export.json').exists()


def test_stage_substitution_during_final_flush_never_publishes_foreign_content(
        prepared_environment, real_protocol_child, tmp_path, monkeypatch):
    service, _, _, identity, _ = complete(prepared_environment)
    target = tmp_path / 'requested'
    original, publish = jobs._sync_directory, jobs.publish_noreplace
    final_move, retained = [False], []
    def replace_stage_after_flush(path):
        result = original(path)
        path = Path(path)
        if final_move[0] and path.name.startswith('.transcription-export-'):
            saved = path.with_name('retained-original-stage')
            path.rename(saved)
            retained.append(saved)
            path.mkdir()
            (path / 'foreign.txt').write_bytes(b'Never publish or remove this')
        return result
    def moving(source, destination, **kwargs):
        final_move[0] = Path(destination) == target
        try:
            return publish(source, destination, **kwargs)
        finally:
            final_move[0] = False
    monkeypatch.setattr(jobs, '_sync_directory', replace_stage_after_flush)
    monkeypatch.setattr(jobs, 'publish_noreplace', moving)
    with pytest.raises(ValueError, match='stage'):
        export_results(service, identity, target)
    assert not target.exists()
    other, = tmp_path.glob('.transcription-export-*')
    assert (other / 'foreign.txt').read_bytes() == b'Never publish or remove this'
    assert (retained[0] / 'transcription-export.json').exists()
