"""Real child-process protocol tests; synthetic engine, no ASR quality claim."""
import hashlib
import json
import importlib.util
import marshal
import os
from pathlib import Path
import shutil
import sys
import struct
import threading
import time

import pytest

from carr_library.audio import transcription as asr
from carr_library.audio import supplied_whisper
from carr_library.audio.models import AudioBudget, AudioCancelled, AudioError
from carr_library.durable_state import atomic_json, digest_file, read_json, state_lock
from carr_library.state_backup import snapshot_state, restore_state


# Authored synthetic protocol fixture. No private supplied source is reproduced.
# The AST shape exercises the exact preparation operations and launches through
# the real isolated subprocess, importing an explicitly inventoried dependency.
SYNTHETIC_WORKER = '''import argparse, json, sys, time
from pathlib import Path
from faster_whisper import marker

def srt_time(seconds):
    return 'bad unprepared timestamp'

def _v43_settings_attempts(base):
    return [('wrong implicit retry', dict(base, language='ru'))]

class Worker:
    def transcribe_one(self, item, settings):
        try:
            value = self.model.transcribe(item['path'], **settings)
        except TypeError:
            value = self.model.transcribe(item['path'], language='ru')
        return value

def main():
    assert sys.flags.utf8_mode == 1
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', required=True)
    job = Path(parser.parse_args().job_dir)
    config = json.loads((job/'job_config.json').read_text())
    settings = config['settings']
    source = next(Path(config['folders'][0]).iterdir())
    text = source.read_text()
    if text == 'sleep':
        time.sleep(60)
    if text == 'network':
        import socket
        socket.create_connection(('example.invalid', 443), timeout=.1)
    result = Path(config['results_root'])/'input'
    result.mkdir(parents=True)
    base = result/source.stem
    language = 'ru' if text == 'wrong-language' else settings['language']
    if language == 'auto':
        language = 'uk'
    segment = {'start': 59.9996, 'end': 61., 'text': text}
    if text == 'bad-timestamp':
        segment['start'] = -1
    if text == 'silence':
        text, segment = '', None
    attempt, actual = _v43_settings_attempts(settings)[0]
    meta = {'source': str(source), 'selected_language': settings['language'],
        'detected_language': language, 'duration':61., 'settings':actual,
        'segments': [segment] if segment else [], 'text':text,
        'v43_attempt':attempt, 'v43_attempt_no':1, 'dependency_marker':marker}
    base.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
    if settings['output_txt']:
        base.with_suffix('.txt').write_text(text + ('\\n' if text else ''), encoding='utf-8')
    if settings['output_srt'] and text != 'missing-srt':
        base.with_suffix('.srt').write_text('1\\n' + srt_time(segment['start']) +
            ' --> ' + srt_time(segment['end']) + '\\n' + text + '\\n' if segment else '', encoding='utf-8')
    if settings['output_vtt']:
        base.with_suffix('.vtt').write_text('WEBVTT\\n\\n' +
            (srt_time(segment['start']).replace(',', '.') + ' --> ' +
            srt_time(segment['end']).replace(',', '.') + '\\n' + text + '\\n' if segment else ''), encoding='utf-8')
    status = {'state':'завершено', 'done':1, 'total':1, 'failed':0, 'active_count':0}
    if text == 'fail':
        status.update(done=0, failed=1)
    (job/'job_status.json').write_text(json.dumps(status), encoding='utf-8')

if __name__ == '__main__':
    main()
'''


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / 'reviewed-installation'
    root.mkdir()
    worker = root / 'supplied-worker.py'
    worker.write_text(SYNTHETIC_WORKER, encoding='utf-8')
    monkeypatch.setattr(supplied_whisper, 'SOURCE_SHA256', digest_file(worker))
    monkeypatch.setattr(asr, 'SOURCE_SHA256', digest_file(worker))
    runtime = root / 'site-packages'
    runtime.mkdir()
    for package in asr._RUNTIME_PACKAGES:
        directory = runtime / package
        directory.mkdir()
        (directory / '__init__.py').write_text('marker = "reviewed dependency"\n')
    models = root / 'models'
    model = models / 'small'
    model.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'synthetic model, never loaded')
    python = Path(sys.executable).resolve()
    manifest = root / 'runtime-manifest.json'
    data = {'schema':1, 'engine':'supplied-whisper-v43', 'python':str(python),
            'python_sha256':digest_file(python), 'worker':str(worker),
            'runtime_directory':str(runtime),
            'runtime_files':{p.relative_to(runtime).as_posix():digest_file(p) for p in runtime.rglob('*.py')},
            'models_directory':str(models), 'model':'small',
            'model_files':{p.name:digest_file(p) for p in model.iterdir()},
            'rights_reviewed':True, 'runtime_reviewed':True,
            'rights_notice':'Synthetic protocol fixture; no real engine or model.'}
    atomic_json(manifest, data)
    return manifest


@pytest.fixture
def service(tmp_path, installation):
    return asr.TranscriptionService(tmp_path/'state', asr.SuppliedWhisperRuntime(installation),
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0, max_output_bytes=4*1024**2))


def audio(tmp_path, text='Синтетичний запис', name='record.wav'):
    source = tmp_path/name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding='utf-8')
    return source


def output(service, job_id, format='txt'):
    record = service.load(job_id)
    return service.root/job_id/'outputs'/record['request']['inputs'][0]['file']/('transcript.'+format)


def test_actual_isolated_worker_imports_reviewed_site_and_commits_all_formats(service, tmp_path):
    source = audio(tmp_path)
    original = source.read_bytes()
    job_id = service.create([source], settings=asr.TranscriptionSettings(language='uk'))
    events = []
    record = service.run(job_id, on_status=events.append)
    assert record['status'] == 'complete'
    assert output(service, job_id).read_text(encoding='utf-8') == 'Синтетичний запис\n'
    assert '00:01:00,000 --> 00:01:01,000' in output(service, job_id, 'srt').read_text(encoding='utf-8')
    result = read_json(output(service, job_id, 'json'))
    assert result['language'] == 'uk' and result['source_sha256'] == digest_file(source)
    assert result['diarization'] is False and result['word_timestamps'] is False
    assert source.read_bytes() == original
    assert events[-1].startswith('Transcription complete')


def test_completed_receipt_reuses_actual_outputs_without_another_process(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    def must_not_run(*args, **kwargs):
        raise AssertionError('A verified completed file must not invoke inference again.')
    monkeypatch.setattr(asr, 'run_process', must_not_run)
    assert service.run(job_id)['status'] == 'complete'


def test_same_size_mtime_audio_changes_do_not_reuse_cache(service, tmp_path):
    source = audio(tmp_path, 'alpha')
    before = source.stat()
    first = service.create([source])
    service.run(first)
    source.write_text('bravo')
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = service.create([source])
    assert first != second
    service.run(second)
    assert output(service, first).read_text() == 'alpha\n'
    assert output(service, second).read_text() == 'bravo\n'


def test_same_basename_separate_folders_keep_both_sources(service, tmp_path):
    first = audio(tmp_path, 'first', 'one/record.wav')
    second = audio(tmp_path, 'second', 'two/record.wav')
    job_id = service.create([first, second])
    record = service.run(job_id)
    texts = [(service.root/job_id/'outputs'/item['file']/'transcript.txt').read_text()
             for item in record['request']['inputs']]
    assert texts == ['first\n', 'second\n']
    assert len(record['completed']) == 2


def test_duplicate_identical_audio_deduplicates_but_settings_change_does_not(service, tmp_path):
    first = audio(tmp_path)
    duplicate = audio(tmp_path, name='renamed.wav')
    same = service.create([first, duplicate])
    assert len(service.load(same)['request']['inputs']) == 1
    assert same == service.create([duplicate])
    changed = service.create([first], settings=asr.TranscriptionSettings(vad_filter=False))
    assert changed != same


@pytest.mark.parametrize('content', ['fail', 'missing-srt', 'wrong-language', 'bad-timestamp'])
def test_failed_or_inconsistent_worker_cannot_publish_complete(service, tmp_path, content):
    job_id = service.create([audio(tmp_path, content)], settings=asr.TranscriptionSettings(language='uk'))
    with pytest.raises((ValueError, OSError, AudioError)):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'failed'
    assert not list((service.root/job_id).rglob('receipt.json'))


def test_unrequested_subtitle_failure_does_not_block_selected_text(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'missing-srt')], formats=('txt',))
    assert service.run(job_id)['status'] == 'complete'
    assert output(service, job_id).read_text() == 'missing-srt\n'
    assert not output(service, job_id, 'srt').exists()


def test_silence_is_valid_completed_empty_transcript(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'silence')])
    assert service.run(job_id)['status'] == 'complete'
    assert output(service, job_id).read_bytes() == b''
    assert output(service, job_id, 'vtt').read_bytes() == b'WEBVTT\n\n'


def test_runtime_or_model_mutation_is_rejected_before_process(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    model = Path(service.runtime.data['models_directory'])/'small'/'model.bin'
    model.write_bytes(b'changed')
    monkeypatch.setattr(asr, 'run_process', lambda *a, **kw: pytest.fail('No changed runtime may run.'))
    with pytest.raises(ValueError, match='SHA256'):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'failed'


def test_runtime_change_during_process_cannot_bind_output_to_old_identity(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    model = Path(service.runtime.data['models_directory'])/'small'/'model.bin'
    real = asr.run_process
    def changed_after_execution(*args, **kwargs):
        result = real(*args, **kwargs)
        model.write_bytes(b'changed during inference')
        return result
    monkeypatch.setattr(asr, 'run_process', changed_after_execution)
    with pytest.raises(ValueError, match='SHA256'):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'failed'
    assert not list((service.root/job_id).rglob('receipt.json'))


def test_unlisted_runtime_code_is_rejected(service, tmp_path):
    (Path(service.runtime.data['runtime_directory'])/'unreviewed.py').write_text('raise RuntimeError')
    with pytest.raises(ValueError, match='complete inventory'):
        service.create([audio(tmp_path)])


def test_original_worker_replacement_is_rejected(service, tmp_path):
    Path(service.runtime.data['worker']).write_text('raise RuntimeError')
    with pytest.raises(ValueError, match='reviewed source'):
        service.create([audio(tmp_path)])


def test_model_manifest_requires_exact_required_dependencies(service, tmp_path):
    root = Path(service.runtime.data['runtime_directory'])
    name = 'tokenizers/__init__.py'
    (root/name).unlink()
    service.runtime.data['runtime_files'].pop(name)
    with pytest.raises(ValueError, match='missing an ASR dependency'):
        service.create([audio(tmp_path)])


def test_original_audio_can_disappear_after_immutable_retention(service, tmp_path):
    source = audio(tmp_path)
    job_id = service.create([source])
    source.unlink()
    assert service.run(job_id)['status'] == 'complete'


def test_modified_retained_audio_and_edited_transcripts_are_preserved(service, tmp_path):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    target = output(service, job_id)
    target.write_text('User correction')
    with pytest.raises(ValueError, match='preserved'):
        service.run(job_id)
    assert target.read_text() == 'User correction'


def test_real_child_process_cancel_is_bounded_and_pause_is_distinct(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'sleep')])
    timer = threading.Timer(.35, lambda: service.control(job_id, 'pause'))
    start = time.monotonic()
    timer.start()
    try:
        with pytest.raises(AudioCancelled):
            service.run(job_id)
    finally:
        timer.join()
    assert time.monotonic() - start < 5
    assert service.load(job_id)['status'] == 'paused'
    assert not list((service.root/job_id).rglob('receipt.json'))


def test_cancel_before_start_does_not_launch_worker(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    service.control(job_id, 'cancel')
    monkeypatch.setattr(asr, 'run_process', lambda *a, **kw: pytest.fail('Cancelled before launch.'))
    with pytest.raises(AudioCancelled):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'cancelled'


def test_completed_file_survives_later_failure_and_restart(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path, 'first', 'first.wav'), audio(tmp_path, 'fail', 'later.wav')])
    with pytest.raises(AudioError):
        service.run(job_id)
    assert len(service.load(job_id)['completed']) == 1
    real = asr.run_process
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(asr, 'run_process', counted)
    with pytest.raises(AudioError):
        service.run(job_id)
    assert len(calls) == 1
    assert output(service, job_id).read_text() == 'first\n'


def test_receipt_recovers_crash_before_job_checkpoint(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    record = service.load(job_id)
    record.update(status='running', completed=[])
    atomic_json(service.root/job_id/'job.json', record, replace=True)
    monkeypatch.setattr(asr, 'run_process', lambda *a, **kw: pytest.fail('Recover committed receipt.'))
    assert service.run(job_id)['status'] == 'complete'


def test_backup_preserves_queued_audio_completed_outputs_and_control(service, tmp_path):
    source = audio(tmp_path)
    job_id = service.create([source])
    service.control(job_id, 'pause')
    snapshot = tmp_path/'backup-state'
    snapshot.mkdir()
    entries = snapshot_state(service.root.parent, snapshot)
    restored = tmp_path/'restored-state'
    restored.mkdir()
    restore_state(snapshot, restored, entries)
    recovered = asr.TranscriptionService(restored, service.runtime, budget=service.budget)
    source.unlink()
    assert recovered.load(job_id)['status'] == 'queued'
    recovered.control(job_id, 'run')
    assert recovered.run(job_id)['status'] == 'complete'


def test_runtime_lock_blocks_overlapping_processes(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    identity = service.load(job_id)['request']['runtime']
    monkeypatch.setattr(asr, 'run_process', lambda *a, **kw: pytest.fail('Runtime is reserved.'))
    with state_lock(service.root, 'runtime-'+identity):
        with pytest.raises(RuntimeError, match='another operation'):
            service.run(job_id)


def test_worker_network_denied_by_actual_python_audit(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'network')])
    with pytest.raises(AudioError, match='denies network'):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'failed'


def test_completed_cleanup_failure_keeps_success_and_notice(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    real = shutil.rmtree
    def cleanup(path, *args, **kwargs):
        if Path(path).parent == service.root/job_id and Path(path).name.startswith('.pending-asr-'):
            raise PermissionError('Synthetic cleanup denial')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(asr.shutil, 'rmtree', cleanup)
    record = service.run(job_id)
    assert record['status'] == 'complete' and record['cleanup_pending']
    assert output(service, job_id).exists()


def test_unverified_matching_timestamp_bytecode_is_never_loaded(service, tmp_path):
    source = Path(service.runtime.data['runtime_directory'])/'faster_whisper'/'__init__.py'
    info = source.stat()
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    # This bytecode has exactly the timestamp/size Python would accept for the
    # reviewed .py file. -B alone still reads it; a fresh pycache prefix prevents it.
    poison = compile("raise RuntimeError('UNVERIFIED_BYTECODE_EXECUTED')", str(source), 'exec')
    cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack('<III', 0,
        int(info.st_mtime) & 0xffffffff, info.st_size) + marshal.dumps(poison))
    job_id = service.create([audio(tmp_path)])
    assert service.run(job_id)['status'] == 'complete'


def test_standalone_unreviewed_bytecode_is_rejected(service, tmp_path):
    (Path(service.runtime.data['runtime_directory'])/'unexpected.pyc').write_bytes(b'not reviewed')
    with pytest.raises(ValueError, match='standalone Python bytecode'):
        service.create([audio(tmp_path)])


def test_supplied_writes_use_deterministic_newlines_on_windows(service, tmp_path, monkeypatch):
    source = Path(service.runtime.data['worker'])
    original = source.read_text(encoding='utf-8')
    # Emulate Windows text-mode LF translation in the actual subprocess. The
    # source preparation must explicitly request a newline mode for all writes.
    windows_writes = '''
_original_write_text = Path.write_text
def _windows_write_text(path, data, encoding=None, errors=None, newline=None):
    if newline is None:
        data = data.replace('\\n', '\\r\\n')
    return _original_write_text(path, data, encoding=encoding, errors=errors, newline='')
Path.write_text = _windows_write_text
'''
    source.write_text(original.replace('from faster_whisper import marker',
        windows_writes + '\nfrom faster_whisper import marker'), encoding='utf-8')
    checksum = digest_file(source)
    monkeypatch.setattr(supplied_whisper, 'SOURCE_SHA256', checksum)
    monkeypatch.setattr(asr, 'SOURCE_SHA256', checksum)
    job_id = service.create([audio(tmp_path)])
    assert service.run(job_id)['status'] == 'complete'
    assert b'\r' not in output(service, job_id, 'srt').read_bytes()


def test_observer_failure_cannot_rewrite_successful_completion(service, tmp_path):
    job_id = service.create([audio(tmp_path)])
    def broken_observer(text):
        raise RuntimeError('Synthetic progress observer failure')
    record = service.run(job_id, on_status=broken_observer)
    assert record['status'] == 'complete' and record['notices']
    assert service.load(job_id)['status'] == 'complete'


def test_cancel_between_format_writes_publishes_no_partial_final_source(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    real = asr.atomic_bytes
    def interrupt(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if Path(path).name == 'transcript.txt':
            service.control(job_id, 'cancel')
        return result
    monkeypatch.setattr(asr, 'atomic_bytes', interrupt)
    with pytest.raises(AudioCancelled):
        service.run(job_id)
    assert list((service.root/job_id/'outputs').iterdir()) == []
    monkeypatch.setattr(asr, 'atomic_bytes', real)
    service.control(job_id, 'run')
    assert service.run(job_id)['status'] == 'complete'


def test_crash_between_format_writes_allows_clean_nondestructive_retry(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    real = asr.atomic_bytes
    def crash(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if Path(path).name == 'transcript.srt':
            raise SystemExit('Synthetic process crash before whole-result publication')
        return result
    monkeypatch.setattr(asr, 'atomic_bytes', crash)
    with pytest.raises(SystemExit):
        service.run(job_id)
    assert service.load(job_id)['status'] == 'running'
    assert list((service.root/job_id/'outputs').iterdir()) == []
    monkeypatch.setattr(asr, 'atomic_bytes', real)
    assert service.run(job_id)['status'] == 'complete'


@pytest.mark.parametrize('damage', ['receipt', 'transcript'])
def test_status_revalidates_completed_artifacts(service, tmp_path, damage):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    target = output(service, job_id)
    if damage == 'receipt':
        (target.parent/'receipt.json').unlink()
    else:
        target.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        service.load(job_id)


def test_cli_status_and_jobs_remain_available_without_runtime(service, tmp_path, capsys):
    from carr_library.transcription_cli import main
    from carr_library.__main__ import main as application_main
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    assert main(['--data-dir', str(tmp_path), 'status', job_id]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert application_main(['transcribe', '--data-dir', str(tmp_path), 'jobs']) == 0
    assert json.loads(capsys.readouterr().out)[0]['id'] == job_id
    output(service, job_id).unlink()
    assert main(['--data-dir', str(tmp_path), 'status', job_id]) == 1
    assert 'Transcription:' in capsys.readouterr().err


@pytest.mark.parametrize('options', [{'language':'../uk'}, {'beam_size':True}, {'device':'auto'},
    {'compute_type':'auto'}, {'task':'unknown'}, {'vad_filter':'true'}])
def test_settings_fail_closed(options):
    with pytest.raises(ValueError):
        asr.TranscriptionSettings(**options)


def test_prepare_refuses_unreviewed_source():
    with pytest.raises(ValueError, match='reviewed source'):
        supplied_whisper.prepare(b'print("unreviewed")')



def test_saved_job_view_keeps_basename_outside_identity_and_verifies_results(service, tmp_path):
    source = audio(tmp_path, name='first name.wav')
    job_id = service.create([source])
    assert service.load(job_id)['source_labels'] == {
        service.load(job_id)['request']['inputs'][0]['file']: 'first name.wav'}
    renamed = audio(tmp_path, name='renamed.wav')
    assert service.create([renamed]) == job_id
    assert service.jobs()[0]['status'] == 'queued'
    service.run(job_id)
    detail = service.details(job_id)
    assert detail['record']['status'] == 'complete'
    assert detail['outputs'][0]['verified']
    assert detail['outputs'][0]['label'] == 'first name.wav'
    assert service.transcript_text(job_id, detail['outputs'][0]['file']) == (
        'Синтетичний запис\n', 'first name.wav')


def test_damaged_results_are_visible_for_review_but_never_verified(service, tmp_path):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    target = output(service, job_id)
    target.write_text('User-edited generated transcript', encoding='utf-8')
    before = target.read_bytes()
    assert service.jobs()[0]['status'] == 'invalid'
    detail = service.details(job_id)
    assert detail['record']['status'] == 'invalid'
    assert not detail['outputs'][0]['verified']
    with pytest.raises(ValueError):
        service.transcript_text(job_id, detail['outputs'][0]['file'])
    assert target.read_bytes() == before
    with pytest.raises(ValueError):
        service.load(job_id)


def test_existing_reader_draft_reopens_after_generated_transcript_was_deleted(service, tmp_path):
    from carr_library.ui.transcription_dialog import prepare_reading_copy
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    target = output(service, job_id)
    source_file = service.load(job_id)['request']['inputs'][0]['file']
    prepared = prepare_reading_copy(tmp_path, service, job_id, source_file)
    store = ReaderDraftStore(tmp_path, slot=prepared['slot'])
    saved = store.load()
    saved.document.set_text('User correction retained independently.')
    store.save(ReaderResume(saved.document))
    target.unlink()
    recovered = prepare_reading_copy(tmp_path, service, job_id, source_file)
    assert recovered['recovered'] is True
    assert store.load().document.text == 'User correction retained independently.'
    assert service.details(job_id)['record']['status'] == 'invalid'
    with pytest.raises((ValueError, FileNotFoundError)):
        service.load(job_id)


def test_old_jobs_without_display_names_remain_readable(service, tmp_path):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    record = service.load(job_id)
    record.pop('source_labels')
    atomic_json(service.root/job_id/'job.json', record, replace=True)
    assert service.details(job_id)['outputs'][0]['label'] == 'Audio 1'


def test_partial_failed_job_exposes_only_committed_verified_transcript(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'first', 'one.wav'),
                             audio(tmp_path, 'fail', 'two.wav')])
    with pytest.raises(AudioError):
        service.run(job_id)
    detail = service.details(job_id)
    assert detail['record']['status'] == 'failed'
    assert len(detail['outputs']) == 1 and detail['outputs'][0]['verified']
    assert service.transcript_text(job_id, detail['outputs'][0]['file'])[0] == 'first\n'


def test_reading_copy_requires_selected_txt_format(service, tmp_path):
    job_id = service.create([audio(tmp_path)], formats=('srt',))
    service.run(job_id)
    detail = service.details(job_id)
    with pytest.raises(ValueError, match='completed TXT'):
        service.transcript_text(job_id, detail['outputs'][0]['file'])


def test_worker_callback_flushes_pause_before_command_read(service, tmp_path):
    job_id = service.create([audio(tmp_path, 'sleep')])
    flushed = []
    def requested():
        if not flushed:
            service.control(job_id, 'pause')
            flushed.append(True)
        return False
    with pytest.raises(AudioCancelled):
        service.run(job_id, cancelled=requested)
    assert service.load(job_id)['status'] == 'paused'


def test_saved_job_inspection_can_cancel_during_receipt_hashing(service, tmp_path):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    with pytest.raises(AudioCancelled):
        service.load(job_id, cancelled=lambda: True)
    with pytest.raises(AudioCancelled):
        service.jobs(cancelled=lambda: True)


def test_folder_selection_is_direct_bounded_and_preserves_existing_choice(service, tmp_path):
    folder = tmp_path/'selected folder'
    first = audio(folder, 'one', 'ONE.WAV')
    second = audio(folder, 'two', 'two.mp3')
    audio(folder/'subfolder', 'nested', 'nested.wav')
    (folder/'ignored.txt').write_text('not audio', encoding='utf-8')
    existing = audio(tmp_path, 'separate', 'selected.wav')
    selected = service.folder_inputs(folder, existing=[str(existing)])
    assert set(selected) == {str(first), str(second), str(existing)}
    with pytest.raises(ValueError, match='file-count'):
        service.folder_inputs(folder, max_files=1)
    with pytest.raises(AudioCancelled):
        service.folder_inputs(folder, cancelled=lambda: True)


def test_folder_selection_reuses_input_byte_budget(service, tmp_path):
    folder = tmp_path/'bounded folder'
    audio(folder, 'x' * 8192, 'too-large.wav')
    service.budget = AudioBudget(timeout=10, reserve_disk_bytes=0, max_output_bytes=4096)
    with pytest.raises(AudioError, match='disk allowance'):
        service.folder_inputs(folder)



def test_resume_completed_job_polls_stop_while_rechecking_receipts(service, tmp_path, monkeypatch):
    job_id = service.create([audio(tmp_path)])
    service.run(job_id)
    target = output(service, job_id)
    before = target.read_bytes()
    real = asr._file_digest
    def paused_digest(path, cancelled=lambda: False):
        if Path(path).name == 'transcript.txt':
            service.control(job_id, 'pause')
            assert cancelled(), 'Completed-job validation did not receive the stop predicate.'
            raise AudioCancelled('Receipt recheck paused.')
        return real(path, cancelled)
    monkeypatch.setattr(asr, '_file_digest', paused_digest)
    with pytest.raises(AudioCancelled):
        service.run(job_id)
    record = read_json(service.root/job_id/'job.json')
    assert record['status'] == 'complete'
    interrupted = service.interrupted_details(job_id)
    assert interrupted['record']['status'] == 'unverified'
    assert interrupted['outputs'] == [] and interrupted['validation_cancelled']
    assert target.read_bytes() == before
