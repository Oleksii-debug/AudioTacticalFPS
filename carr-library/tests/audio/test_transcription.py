"""Real filesystem/integrity tests; the injected engine is not ASR quality evidence."""
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.audio import transcription as module
from carr_library.audio.transcription import TranscriptionSession, TranscriptionSettings


def model_manifest(root, **updates):
    root.mkdir()
    files = {}
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        path = root / name
        path.write_bytes(b'synthetic fixture, never loaded as a real model: ' + name.encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    value = {'schema_version': 1, 'engine': 'faster-whisper', 'model_id': 'synthetic-asr-only',
             'revision': 'a' * 40, 'license': 'synthetic-test-fixture',
             'license_accepted': True, 'trusted_weights': True, 'files': files}
    value.update(updates)
    path = root / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def runtime(version='test-runtime-1'):
    return {'python': '3.11.test', 'distributions': {'faster-whisper': {
        'version': version, 'files_sha256': hashlib.sha256(version.encode()).hexdigest()}}}


class FakeModel:
    supported_languages = ['en', 'uk', 'sk']

    def __init__(self):
        self.calls = []
        self.fail = False
        self.closed = 0

    def transcribe(self, source, **options):
        self.calls.append((Path(source).read_bytes(), options))
        if self.fail:
            raise RuntimeError('Synthetic engine failure')

        def segments():
            try:
                yield SimpleNamespace(start=0., end=1., text=' Вітаю, друзі! ', words=(
                    [SimpleNamespace(start=0., end=0.5, word='Вітаю,'),
                     SimpleNamespace(start=0.5, end=1., word='друзі!')]
                    if options['word_timestamps'] else None))
                yield SimpleNamespace(start=59.9996, end=61., text='Другий фрагмент.', words=None)
            finally:
                self.closed += 1
        return segments(), SimpleNamespace(language='uk', duration=61.)


@pytest.fixture
def setup(tmp_path):
    model = FakeModel()
    loaded = []
    version = ['test-runtime-1']

    def factory(path, **options):
        loaded.append((path, options))
        return model
    manifest = model_manifest(tmp_path / 'model')
    session = TranscriptionSession(manifest, _model_factory=factory,
        _runtime_identity=lambda: runtime(version[0]))
    source = tmp_path / 'audio Україна.wav'
    source.write_bytes(b'synthetic media bytes')
    return SimpleNamespace(session=session, model=model, loaded=loaded, source=source,
        root=tmp_path / 'outputs', manifest=manifest, version=version)


def test_all_requested_formats_and_receipt_follow_verified_publication(setup):
    result = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(word_timestamps=True))
    assert result['status'] == 'complete'
    assert 'text' not in result  # Batch receipts must not retain every transcript body.
    assert result['cached'] is False and result['language'] == 'uk'
    assert result['segments'] == 2 and result['duration'] == 61
    assert set(result['files']) == set(module.FORMATS)
    for name, path in result['files'].items():
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == result['outputs']['transcript.' + name]['sha256']
    assert '00:01:00,000 --> 00:01:01,000' in Path(result['files']['srt']).read_text('utf-8')
    assert Path(result['files']['vtt']).read_text('utf-8').startswith('WEBVTT\n')
    details = json.loads(Path(result['files']['json']).read_text('utf-8'))
    assert details['segments'][0]['words'][0]['word'] == 'Вітаю,'
    assert details['settings']['word_timestamps'] is True
    assert details['translation_target'] is None and details['diarization'] is False
    assert str(setup.source) not in json.dumps(details)
    assert setup.model.calls[0][1]['language'] is None
    assert len(setup.loaded) == 1 and setup.model.closed == 1
    assert list(Path(result['files']['txt']).parent.glob('complete-*.json'))
    assert not list(setup.root.glob('.transcription-*/*source*'))


def test_exact_cache_reuses_engine_result_but_always_verifies_outputs(setup):
    first = setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    second = setup.session.transcribe(setup.source, setup.root, formats=('json', 'txt'))
    assert first['identity'] == second['identity'] and second['cached'] is True
    assert len(setup.model.calls) == 1
    Path(second['files']['txt']).write_text('user edited transcript', encoding='utf-8')
    with pytest.raises(ValueError, match='different content'):
        setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    assert Path(second['files']['txt']).read_text('utf-8') == 'user edited transcript'


def test_same_basename_unverified_text_is_never_adopted(setup, tmp_path):
    setup.root.mkdir()
    (setup.root / (setup.source.stem + '.txt')).write_text('wrong preexisting transcript', encoding='utf-8')
    first = setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    other = tmp_path / 'other'
    other.mkdir()
    other_audio = other / setup.source.name
    other_audio.write_bytes(b'different same-name audio')
    second = setup.session.transcribe(other_audio, setup.root, formats=('txt',))
    assert first['identity'] != second['identity']
    assert len(setup.model.calls) == 2
    assert 'wrong' not in Path(first['files']['txt']).read_text('utf-8')


def test_exact_content_changes_invalidate_cache_even_same_size_and_mtime(setup):
    import os
    first = setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    old = setup.source.stat()
    setup.source.write_bytes(b'X' * old.st_size)
    os.utime(setup.source, ns=(old.st_atime_ns, old.st_mtime_ns))
    second = setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    assert first['identity'] != second['identity'] and len(setup.model.calls) == 2


def test_settings_and_actual_runtime_identity_invalidate_cache(setup):
    first = setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    second = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(language='uk', beam_size=1), formats=('txt',))
    setup.version[0] = 'changed-runtime'
    third = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(language='uk', beam_size=1), formats=('txt',))
    assert len({first['identity'], second['identity'], third['identity']}) == 3
    assert len(setup.model.calls) == 3
    assert len(setup.loaded) == 2


def test_model_bytes_are_reverified_before_a_cached_result(setup):
    setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    (setup.manifest.parent / 'model.bin').write_bytes(b'mutated checkpoint')
    with pytest.raises(module.ModelManifestError, match='Checksum mismatch'):
        setup.session.transcribe(setup.source, setup.root, formats=('txt',))


def test_cache_result_hash_tamper_cannot_be_accepted(setup):
    setup.session.transcribe(setup.source, setup.root, formats=('txt',))
    path = next((setup.root / '.transcription-cache').glob('*.json'))
    saved = json.loads(path.read_text('utf-8'))
    saved['result']['text'] = 'substituted'
    path.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ValueError, match='does not match'):
        setup.session.transcribe(setup.source, setup.root, formats=('txt',))


def test_failed_export_does_not_publish_completion_and_can_retry_from_cache(setup, monkeypatch):
    original = module.publish_new

    def fail_srt(source, target):
        if target.suffix == '.srt':
            raise OSError('Synthetic full disk during SRT publication')
        return original(source, target)
    monkeypatch.setattr(module, 'publish_new', fail_srt)
    with pytest.raises(OSError, match='Synthetic full disk'):
        setup.session.transcribe(setup.source, setup.root, formats=('txt', 'srt'))
    assert not list(setup.root.glob('transcript-*/complete-*.json'))
    assert len(setup.model.calls) == 1
    monkeypatch.setattr(module, 'publish_new', original)
    result = setup.session.transcribe(setup.source, setup.root, formats=('txt', 'srt'))
    assert result['cached'] is True and len(setup.model.calls) == 1
    assert set(result['files']) == {'txt', 'srt'}


def test_post_commit_cleanup_failure_remains_complete_with_truthful_warning(setup, monkeypatch):
    def cleanup(directory):
        raise PermissionError('Synthetic locked snapshot')
    monkeypatch.setattr(module.tempfile.TemporaryDirectory, 'cleanup', cleanup)
    result = setup.session.transcribe(setup.source, setup.root, formats=('txt', 'json'))
    assert result['status'] == 'complete'
    assert result['warnings'] and result['cleanup_pending']
    assert Path(result['files']['txt']).is_file() and Path(result['files']['json']).is_file()
    assert list(Path(result['files']['txt']).parent.glob('complete-*.json'))


def test_cleanup_failure_preserves_original_inference_error(setup, monkeypatch):
    setup.model.fail = True
    def cleanup(directory):
        raise PermissionError('Synthetic locked snapshot')
    monkeypatch.setattr(module.tempfile.TemporaryDirectory, 'cleanup', cleanup)
    with pytest.raises(RuntimeError, match='Synthetic engine failure') as error:
        setup.session.transcribe(setup.source, setup.root)
    assert 'also needs cleanup' in error.value.__notes__[0]
    assert not list(setup.root.glob('transcript-*/complete-*.json'))


def test_engine_error_does_not_force_another_language_or_claim_success(setup):
    setup.model.fail = True
    with pytest.raises(RuntimeError, match='Synthetic engine failure'):
        setup.session.transcribe(setup.source, setup.root)
    assert len(setup.model.calls) == 1
    assert setup.model.calls[0][1]['language'] is None
    assert not list(setup.root.glob('transcript-*/complete-*.json'))
    assert not list((setup.root / '.transcription-cache').glob('*.json'))


@pytest.mark.parametrize('task', ['transcribe', 'translate'])
def test_english_only_model_rejects_explicit_uk_before_inference(setup, task):
    # faster-whisper otherwise logs a warning and substitutes English itself.
    setup.model.supported_languages = ['en']
    with pytest.raises(module.EngineUnavailable, match='requested source language'):
        setup.session.transcribe(setup.source, setup.root,
            settings=TranscriptionSettings(language='uk', task=task))
    assert not setup.model.calls
    assert not list((setup.root / '.transcription-cache').glob('*.json'))
    assert not list(setup.root.glob('transcript-*'))
    assert setup.source.read_bytes() == b'synthetic media bytes'


@pytest.mark.parametrize('supported', [None, 'en,uk', [], ['en', None]])
def test_explicit_language_requires_actual_model_capabilities(setup, supported):
    setup.model.supported_languages = supported
    with pytest.raises(module.EngineUnavailable, match='supported source languages'):
        setup.session.transcribe(setup.source, setup.root,
            settings=TranscriptionSettings(language='uk'))
    assert not setup.model.calls
    assert not list(setup.root.glob('transcript-*'))


@pytest.mark.parametrize('task', ['transcribe', 'translate'])
def test_reported_source_language_must_match_explicit_selection(setup, task):
    # A backend can advertise support but still violate the requested language.
    original = setup.model.transcribe

    def wrong_language(source, **options):
        segments, info = original(source, **options)
        info.language = 'en'
        return segments, info

    setup.model.transcribe = wrong_language
    with pytest.raises(ValueError, match='requested source language'):
        setup.session.transcribe(setup.source, setup.root,
            settings=TranscriptionSettings(language='uk', task=task))
    assert len(setup.model.calls) == 1 and setup.model.closed == 1
    assert not list((setup.root / '.transcription-cache').glob('*.json'))
    assert not list(setup.root.glob('transcript-*'))


@pytest.mark.parametrize('task', ['transcribe', 'translate'])
def test_previously_mislabelled_cache_cannot_publish_additional_formats(setup, task):
    settings = TranscriptionSettings(language='uk', task=task)
    first = setup.session.transcribe(setup.source, setup.root, settings=settings, formats=('txt',))
    original_bytes = Path(first['files']['txt']).read_bytes()
    cache = next((setup.root / '.transcription-cache').glob('*.json'))
    saved = json.loads(cache.read_text('utf-8'))
    # Earlier versions accepted this internally consistent but wrong-language
    # cache. Its digest alone cannot bind it to the explicit source selection.
    saved['result']['language'] = 'en'
    saved['result_sha256'] = module.digest(saved['result'])
    cache.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ValueError, match='requested source language'):
        setup.session.transcribe(setup.source, setup.root, settings=settings, formats=('json',))
    assert len(setup.model.calls) == 1
    assert Path(first['files']['txt']).read_bytes() == original_bytes
    assert not list(setup.root.glob('transcript-*/transcript.json'))
    assert len(list(setup.root.glob('transcript-*/complete-*.json'))) == 1


@pytest.mark.parametrize('language', ['en', 'auto'])
def test_english_only_model_allows_english_and_auto_without_substitution(setup, language):
    setup.model.supported_languages = ['en']
    original = setup.model.transcribe

    def english(source, **options):
        segments, info = original(source, **options)
        info.language = 'en'
        return segments, info

    setup.model.transcribe = english
    result = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(language=language), formats=('json',))
    details = json.loads(Path(result['files']['json']).read_text('utf-8'))
    assert result['status'] == 'complete' and details['language'] == 'en'
    assert details['settings']['language'] == language
    assert setup.model.calls[0][1]['language'] == (None if language == 'auto' else language)


def test_english_translation_keeps_the_explicit_source_language(setup):
    result = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(language='uk', task='translate'), formats=('json',))
    details = json.loads(Path(result['files']['json']).read_text('utf-8'))
    assert details['language'] == 'uk' and details['translation_target'] == 'en'
    assert details['settings']['language'] == 'uk'


def test_cancelled_segment_closes_generator_and_preserves_no_false_result(setup):
    cancelled = threading.Event()
    def status(message):
        if message.startswith('Transcribed through'):
            cancelled.set()
    with pytest.raises(InterruptedError):
        setup.session.transcribe(setup.source, setup.root, cancelled=cancelled.is_set, on_status=status)
    assert setup.model.closed == 1
    assert not list((setup.root / '.transcription-cache').glob('*.json'))
    assert not list(setup.root.glob('transcript-*/complete-*.json'))
    cancelled.clear()
    result = setup.session.transcribe(setup.source, setup.root)
    assert result['cached'] is False and len(setup.loaded) == 1


def test_cancelled_before_start_does_not_create_outputs_or_load_engine(setup):
    with pytest.raises(InterruptedError):
        setup.session.transcribe(setup.source, setup.root, cancelled=lambda: True)
    assert not setup.root.exists() and not setup.loaded


def test_transcription_never_writes_into_a_reviewed_model_installation(setup):
    destination = setup.manifest.parent / 'new-outputs'
    with pytest.raises(ValueError, match='outside the reviewed model'):
        setup.session.transcribe(setup.source, destination)
    assert not destination.exists() and not setup.loaded


def test_complete_cache_byte_budget_is_enforced_before_publication(setup, monkeypatch):
    # The result fits; its actual indented checkpoint plus provenance does not.
    monkeypatch.setattr(module, 'MAX_RESULT_BYTES', 700)
    with pytest.raises(ValueError, match='complete transcription checkpoint'):
        setup.session.transcribe(setup.source, setup.root)
    assert not list((setup.root / '.transcription-cache').glob('*.json'))
    assert not list(setup.root.glob('transcript-*/complete-*.json'))


@pytest.mark.parametrize('invalid', [(), ('txt', 'txt'), ('docx',), [['txt']]])
def test_invalid_output_selection_fails_before_creation(setup, invalid):
    with pytest.raises(ValueError, match='formats'):
        setup.session.transcribe(setup.source, setup.root, formats=invalid)
    assert not setup.root.exists()


def test_empty_untyped_settings_are_not_silently_replaced(setup):
    with pytest.raises(TypeError, match='TranscriptionSettings'):
        setup.session.transcribe(setup.source, setup.root, settings={})


def test_same_session_cannot_change_model_settings_during_active_inference(setup, tmp_path):
    entered, resume = threading.Event(), threading.Event()
    original = setup.model.transcribe
    active = []

    def blocked(source, **options):
        active.append(options['language'])
        entered.set()
        assert resume.wait(5)
        return original(source, **options)
    setup.model.transcribe = blocked
    errors, outputs = [], []
    def run(destination, settings):
        try:
            outputs.append(setup.session.transcribe(setup.source, destination, settings=settings, formats=('txt',)))
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=run, args=(setup.root, TranscriptionSettings()))
    second = threading.Thread(target=run, args=(tmp_path / 'outputs2', TranscriptionSettings(language='uk')))
    first.start()
    assert entered.wait(5)
    second.start()
    assert active == [None]
    resume.set()
    first.join(5)
    second.join(5)
    assert not errors and len(outputs) == 2 and active == [None, 'uk']
    assert len(setup.loaded) == 1


@pytest.mark.parametrize('options', [
    {'beam_size': True}, {'beam_size': 0}, {'language': 'uk-UA'}, {'language': '../uk'},
    {'task': 'translate_to_uk'}, {'device': 'auto'}, {'compute_type': 'auto'}, {'vad_filter': 'false'},
])
def test_invalid_settings_fail_before_optional_engine_load(options):
    with pytest.raises(ValueError):
        TranscriptionSettings(**options)


def test_translate_is_explicitly_english_and_word_timestamps_reach_engine(setup):
    result = setup.session.transcribe(setup.source, setup.root,
        settings=TranscriptionSettings(task='translate', word_timestamps=True), formats=('json',))
    assert setup.model.calls[0][1]['task'] == 'translate'
    assert setup.model.calls[0][1]['word_timestamps'] is True
    details = json.loads(Path(result['files']['json']).read_text('utf-8'))
    assert details['translation_target'] == 'en'


def test_missing_local_tokenizer_never_uses_a_remote_name(tmp_path):
    path = model_manifest(tmp_path / 'model')
    value = json.loads(path.read_text('utf-8'))
    del value['files']['tokenizer.json']
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(module.ModelManifestError, match='not in the verified manifest'):
        TranscriptionSession(path)


def test_required_model_review_not_inferred_from_file_presence(tmp_path):
    with pytest.raises(module.ModelManifestError, match='Review the exact'):
        TranscriptionSession(model_manifest(tmp_path / 'model', trusted_weights=False))


def test_wrong_runtime_version_blocks_before_model_loading(setup):
    value = json.loads(setup.manifest.read_text('utf-8'))
    value['settings'] = {'runtime_versions': {'faster-whisper': 'another-version'}}
    setup.manifest.write_text(json.dumps(value), encoding='utf-8')
    session = TranscriptionSession(setup.manifest, _runtime_identity=runtime)
    with pytest.raises(module.EngineUnavailable, match='pinned version'):
        session.transcribe(setup.source, setup.root)


def test_default_backend_uses_local_path_and_never_downloads(monkeypatch, tmp_path):
    import sys
    captured = []
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: captured.append((args, kwargs))))
    module._default_model_factory(tmp_path, device='cpu', compute_type='int8')
    assert captured == [((str(tmp_path),), {'local_files_only': True, 'num_workers': 1,
                                          'device': 'cpu', 'compute_type': 'int8'})]


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, True])
def test_invalid_engine_timestamps_fail_without_publication(setup, bad):
    setup.model.transcribe = lambda *args, **kwargs: (
        iter([SimpleNamespace(start=bad, end=1., text='invalid', words=None)]),
        SimpleNamespace(language='uk', duration=1.))
    with pytest.raises(ValueError, match='timestamp'):
        setup.session.transcribe(setup.source, setup.root)
    assert not list(setup.root.glob('transcript-*/complete-*.json'))


def test_runtime_inventory_cancellation_is_not_reported_as_missing_dependencies(monkeypatch):
    cancelled = threading.Event()
    def distribution(name):
        cancelled.set()
        return SimpleNamespace(files=[Path('synthetic-runtime.py')])
    monkeypatch.setattr(module.metadata, 'distribution', distribution)
    with pytest.raises(InterruptedError, match='Transcription cancelled'):
        module.transcription_runtime_identity(cancelled=cancelled.is_set)
