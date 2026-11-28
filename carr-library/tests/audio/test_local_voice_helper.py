"""Actual public deterministic helper processes through the current engine owner."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import Mock
import wave

import pytest

from carr_library.audio import engines, local_voice_helper as helper
from carr_library.audio.model_manifest import ModelManifestError

_spec = importlib.util.spec_from_file_location('_local_voice_contract_fixture',
    Path(__file__).resolve().parents[2] / 'scripts' / 'local_voice_contract_fixture.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture(scope='module')
def interpreter(tmp_path_factory):
    return fixture.interpreter_template(tmp_path_factory.mktemp('helper-host') / 'python')


@pytest.fixture
def pack(tmp_path, interpreter):
    def build(*, mode='good', voices=None, settings=None):
        return fixture.installation(tmp_path / ('pack-' + str(len(list(tmp_path.iterdir())))),
                                    template=interpreter, mode=mode, voices=voices, settings=settings)
    return build


def edit(manifest, mutate):
    value = json.loads(manifest.read_text(encoding='utf-8'))
    mutate(value)
    manifest.write_text(json.dumps(value), encoding='utf-8')
    return manifest


def selected(pack, **options):
    engine = helper.LocalVoiceHelperEngine(pack(**options))
    return engine, engine.voices()[0].id


def test_constructor_and_manifest_verification_never_execute(pack, monkeypatch):
    run = Mock(side_effect=AssertionError('Registration is passive'))
    monkeypatch.setattr(engines, '_run', run)
    engine = helper.LocalVoiceHelperEngine(pack())
    report = engine.manifest.verify(require_complete=True)
    assert report['files'] >= 3 and engine.launch == 'python'
    run.assert_not_called()


@pytest.mark.parametrize('change', ['protocol', 'launch', 'extra-args', 'extra-env', 'extra-cwd',
    'extra-module', 'outside-script', 'missing-script', 'script-suffix', 'unlisted-executable',
    'shell-launcher', 'trust', 'voice-limit-zero', 'voice-limit-bool', 'voice-limit-large',
    'timeout-zero', 'timeout-large', 'engine-name-control'])
def test_closed_manifest_contract_rejects_unreviewed_authority(pack, change):
    manifest = pack()
    def mutate(value):
        settings = value['settings']
        if change == 'protocol':
            settings['protocol'] = 'carr.other.v1'
        elif change == 'launch':
            settings['launch'] = 'shell'
        elif change.startswith('extra-'):
            settings[change[6:]] = ['--arbitrary']
        elif change == 'outside-script':
            settings['script'] = '../outside.py'
        elif change == 'missing-script':
            del settings['script']
        elif change == 'script-suffix':
            settings['script'] = settings['executable']
        elif change == 'unlisted-executable':
            settings['executable'] = 'not-listed.exe'
        elif change == 'shell-launcher':
            source = manifest.parent / settings['executable']
            target = manifest.parent / 'command.cmd'
            target.write_bytes(source.read_bytes())
            value['files']['command.cmd'] = sha256(target.read_bytes()).hexdigest()
            settings['executable'] = 'command.cmd'
        elif change == 'trust':
            value['trusted_weights'] = False
        elif change.startswith('voice-limit'):
            settings['max_voices'] = {'voice-limit-zero': 0, 'voice-limit-bool': True,
                                      'voice-limit-large': 10001}[change]
        elif change.startswith('timeout'):
            settings['timeout_seconds'] = 0 if change == 'timeout-zero' else 86401
        else:
            settings['engine_name'] = 'Invalid\nname'
    edit(manifest, mutate)
    with pytest.raises((ValueError, OSError)):
        helper.LocalVoiceHelperEngine(manifest)


def test_native_launch_is_one_exact_pinned_executable_without_shell_arguments(pack, monkeypatch):
    manifest = pack()
    def mutate(value):
        value['settings']['launch'] = 'native'
        del value['settings']['script']
    edit(manifest, mutate)
    engine = helper.LocalVoiceHelperEngine(manifest)
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return json.dumps({'schema': helper.PROTOCOL, 'voices': [fixture.VOICE]}).encode()
    monkeypatch.setattr(engines, '_run', run)
    assert len(engine.voices()) == 1
    command, options = calls[0]
    assert command == [engine.binary]
    assert json.loads(options['data']) == {'schema': helper.PROTOCOL, 'operation': 'voices'}
    assert options['cwd'].name.startswith('carr-local-voice-catalogue-')
    assert not options['cwd'].exists()


def test_actual_catalogue_has_stable_exact_tokens_and_only_supported_controls(pack):
    manifest = pack()
    first, second = helper.LocalVoiceHelperEngine(manifest), helper.LocalVoiceHelperEngine(manifest)
    one, two = first.voices(), second.voices()
    prefix = sha256(first.engine_name.encode()).hexdigest()[:12]
    token = sha256(fixture.VOICE['id'].encode()).hexdigest()[:24]
    assert one == two and one[0].id == 'helper:' + prefix + ':' + token
    assert one[0].languages == ('uk-UA',)
    assert one[0].controls == ('rate', 'volume', 'pitch', 'punctuation')
    assert one[0].license == fixture.VOICE['license']


@pytest.mark.parametrize('mode', ['duplicate-json', 'nonfinite-json', 'invalid-utf8',
                                  'log-overflow', 'wrong-schema', 'reply-authority'])
def test_actual_invalid_or_authority_bearing_reply_never_publishes_catalogue(pack, mode):
    engine = helper.LocalVoiceHelperEngine(pack(mode=mode))
    with pytest.raises((ValueError, RuntimeError)):
        engine.voices()
    assert engine._records == {}


@pytest.mark.parametrize('change', ['duplicate-id', 'extra-field', 'missing-license', 'language',
    'capability-unknown', 'capability-duplicate', 'capability-type', 'token-control', 'name-empty',
    'too-many'])
def test_catalogue_fields_tokens_and_budgets_are_exact(pack, change):
    row = dict(fixture.VOICE, capabilities=list(fixture.VOICE['capabilities']))
    rows, settings = [row], {}
    if change == 'duplicate-id':
        rows.append(dict(row))
    elif change == 'extra-field':
        row['executable'] = 'other.exe'
    elif change == 'missing-license':
        del row['license']
    elif change == 'language':
        row['language'] = '../../not-language'
    elif change == 'capability-unknown':
        row['capabilities'].append('execute')
    elif change == 'capability-duplicate':
        row['capabilities'].append('rate')
    elif change == 'capability-type':
        row['capabilities'] = 'rate'
    elif change == 'token-control':
        row['id'] = 'token\ncommand'
    elif change == 'name-empty':
        row['name'] = ''
    else:
        rows.append(dict(row, id='second'))
        settings['max_voices'] = 1
    engine = helper.LocalVoiceHelperEngine(pack(voices=rows, settings=settings))
    with pytest.raises((ValueError, RuntimeError)):
        engine.voices()
    assert engine._records == {}


def test_actual_literal_unicode_and_controls_cross_json_stdin_without_shell(pack, tmp_path, monkeypatch):
    monkeypatch.setenv('CARR_FIXTURE_API_KEY', 'synthetic-secret-do-not-pass')
    monkeypatch.setenv('CARR_FIXTURE_PASSWORD', 'synthetic-password-do-not-pass')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'untrusted-imports'))
    engine, voice = selected(pack)
    text = 'Київ, <speak>literal</speak>; "$()" \\ path\nDruhý riadok — 100%.'
    target = tmp_path / 'result.wav'
    engine.synthesize(text, target, voice=voice, language='uk', rate=-2, volume=.63,
                      pitch=3, punctuation=True)
    with wave.open(str(target), 'rb') as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (16000, 1, 2, 512)
        assert audio.readframes(512) == fixture.expected_pcm(text, rate=-2, volume=.63,
                                                            pitch=3, punctuation=True)
    assert not list(tmp_path.glob('.speech-*'))
    assert 'synthetic-secret-do-not-pass' not in json.dumps(engine.cache_identity(voice))


@pytest.mark.parametrize('change', ['fractional-rate', 'punctuation-type', 'wrong-language',
                                   'missing-rate', 'missing-pitch', 'missing-volume', 'missing-punctuation'])
def test_controls_not_declared_by_selected_voice_fail_before_render(pack, tmp_path, monkeypatch, change):
    row = dict(fixture.VOICE, capabilities=[])
    engine, voice = selected(pack, voices=[row])
    calls = []
    original = engine._call
    def traced(request, **options):
        calls.append(request['operation'])
        return original(request, **options)
    monkeypatch.setattr(engine, '_call', traced)
    options = {'language': 'uk'}
    options.update({'fractional-rate': {'rate': .5}, 'punctuation-type': {'punctuation': 1},
                    'wrong-language': {'language': 'en'}, 'missing-rate': {'rate': 1},
                    'missing-pitch': {'pitch': 1}, 'missing-volume': {'volume': .5},
                    'missing-punctuation': {'punctuation': True}}[change])
    target = tmp_path / 'denied.wav'
    with pytest.raises((ValueError, RuntimeError)):
        engine.synthesize('Retained input.', target, voice=voice, **options)
    assert 'render' not in calls and not target.exists()


@pytest.mark.parametrize('mode', ['render-exit', 'truncated-pcm', 'wrong-pcm',
                                  'render-authority', 'wrong-status'])
def test_actual_invalid_render_has_no_visible_output_and_removes_private_stage(pack, tmp_path, mode):
    engine, voice = selected(pack, mode=mode)
    target = tmp_path / 'invalid.wav'
    with pytest.raises((ValueError, RuntimeError)):
        engine.synthesize('Test.', target, voice=voice, language='uk')
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_existing_output_is_never_replaced_or_sent_to_helper(pack, tmp_path, monkeypatch):
    engine, voice = selected(pack)
    target = tmp_path / 'retained.wav'
    target.write_bytes(b'Exact existing unrelated output.')
    called = Mock(side_effect=AssertionError('No helper execution for an occupied target'))
    monkeypatch.setattr(engine, '_call', called)
    with pytest.raises(FileExistsError):
        engine.synthesize('Test.', target, voice=voice, language='uk')
    assert target.read_bytes() == b'Exact existing unrelated output.'
    called.assert_not_called()


@pytest.mark.parametrize('asset', ['script', 'manifest', 'unlisted'])
def test_changed_reviewed_pack_is_rejected_before_process_start(pack, tmp_path, monkeypatch, asset):
    engine, voice = selected(pack)
    if asset == 'script':
        path = engine.script
        before = path.stat()
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"SCHEMA =", b"SCHEMb =", 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif asset == 'manifest':
        edit(engine.manifest.path, lambda value: value.update(revision='other'))
    else:
        (engine.manifest.root / 'unreviewed.py').write_text('raise SystemExit(0)', encoding='utf-8')
    start = Mock(side_effect=AssertionError('Changed installation must not execute'))
    monkeypatch.setattr(engines, '_start_process', start)
    with pytest.raises((ValueError, RuntimeError)):
        engine.synthesize('Test.', tmp_path / 'denied.wav', voice=voice, language='uk')
    start.assert_not_called()


def test_hardlinked_launch_file_is_denied(pack, tmp_path):
    manifest = pack()
    script = manifest.parent / 'helper.py'
    os.link(script, tmp_path / 'alias.py')
    with pytest.raises(ModelManifestError, match='hard-link'):
        helper.LocalVoiceHelperEngine(manifest)


def test_selected_voice_removed_after_render_is_not_published(pack, tmp_path, monkeypatch):
    engine, voice = selected(pack)
    original = engine.voices
    calls = 0
    def changed(**options):
        nonlocal calls
        calls += 1
        result = original(**options)
        if calls == 2:
            engine._records = {}
        return result
    monkeypatch.setattr(engine, 'voices', changed)
    with pytest.raises(engines.EngineUnavailable, match='No substitute'):
        engine.synthesize('Test.', tmp_path / 'denied.wav', voice=voice, language='uk')
    assert not (tmp_path / 'denied.wav').exists() and not list(tmp_path.glob('.speech-*'))


def test_rendered_pcm_remains_held_through_final_catalogue_recheck(pack, tmp_path, monkeypatch):
    engine, voice = selected(pack)
    original, attempts = engine.voices, []
    def during_recheck(**options):
        outputs = list(tmp_path.glob('.speech-*/speech.wav'))
        if outputs:
            try:
                with outputs[0].open('r+b') as stream:
                    stream.seek(44)
                    stream.write(b'\x01\x02')
                attempts.append('changed')
            except PermissionError:
                attempts.append('denied')
        return original(**options)
    monkeypatch.setattr(engine, 'voices', during_recheck)
    target = tmp_path / 'protected.wav'
    if os.name == 'nt':
        engine.synthesize('Test.', target, voice=voice, language='uk')
        assert attempts == ['denied'] and target.is_file()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            engine.synthesize('Test.', target, voice=voice, language='uk')
        assert attempts == ['changed'] and not target.exists()


@pytest.mark.parametrize('cause', ['deadline', 'cancel'])
def test_actual_running_child_stops_before_private_pcm_cleanup(pack, tmp_path, monkeypatch, cause):
    engine, voice = selected(pack, mode='hang-render')
    engine.timeout = 2 if cause == 'deadline' else 15
    started = Event()
    processes = []
    original = engines._start_process
    def start(command, **options):
        process = original(command, **options)
        processes.append(process)
        if Path(options['cwd']).name.startswith('.speech-'):
            started.set()
        return process
    monkeypatch.setattr(engines, '_start_process', start)
    target = tmp_path / 'cancelled.wav'
    before = monotonic()
    with pytest.raises(TimeoutError if cause == 'deadline' else InterruptedError):
        engine.synthesize('Test.', target, voice=voice, language='uk',
                          cancelled=lambda: cause == 'cancel' and started.is_set())
    assert monotonic() - before < 15 and started.is_set()
    assert all(process.poll() is not None for process in processes)
    assert not target.exists() and not list(tmp_path.glob('.speech-*'))


def test_precancelled_operation_never_starts_a_process(pack, tmp_path, monkeypatch):
    engine = helper.LocalVoiceHelperEngine(pack())
    start = Mock(side_effect=AssertionError('Precancelled helper must not execute'))
    monkeypatch.setattr(engines, '_start_process', start)
    with pytest.raises(InterruptedError):
        engine.voices(cancelled=lambda: True)
    start.assert_not_called()


def test_helper_profile_registration_is_passive_and_old_raw_settings_are_exact(pack, tmp_path, monkeypatch):
    from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry
    library = tmp_path / 'library'
    library.mkdir()
    store = EngineProfileStore(library)
    store.path.parent.mkdir(parents=True)
    legacy = {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
              'voice_helpers': [{'executable': '/old/do-not-execute', 'engine_name': 'Old helper'}],
              'music_models': [], 'fluidsynth': None}
    raw = (json.dumps(legacy, indent=3) + '\n\n').encode()
    store.path.write_bytes(raw)
    selected_manifest = pack()
    run = Mock(side_effect=AssertionError('No process during explicit registration or passive reopen'))
    monkeypatch.setattr(engines, '_run', run)
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    before = configured_voice_registry(library)
    assert before.voices() == () and store.snapshot().legacy
    reviewed = store.inspect(selected_manifest)
    profile = store.register(selected_manifest, expected_sha256=reviewed.manifest.manifest_sha256,
                             name='Явно переглянутий помічник')
    restored = EngineProfileStore(library)
    assert restored.snapshot().profiles == (profile,)
    configured = configured_voice_registry(library)
    assert set(configured.engines) == {'local-' + profile.id}
    archive = store.path.parent / 'legacy' / ('engines-schema1-' + sha256(raw).hexdigest() + '.json')
    assert archive.read_bytes() == raw
    run.assert_not_called()


def test_current_registered_helper_voice_runs_through_same_registry_and_reopens_stably(pack, tmp_path, monkeypatch):
    from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry
    library = tmp_path / 'library'
    library.mkdir()
    store = EngineProfileStore(library)
    profile = store.register(pack(), name='Selected helper')
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    registry = configured_voice_registry(library)
    voice = registry.voices()[0]
    assert voice.engine == 'local-' + profile.id and voice.id.startswith(voice.engine + ':')
    assert voice.name.startswith('Selected helper — ')
    before = registry.cache_identity(voice.id)
    target = tmp_path / 'registered.wav'
    registry.synthesize('Точний текст.', target, voice=voice.id, language='uk', punctuation=True)
    with wave.open(str(target), 'rb') as audio:
        assert audio.readframes(512) == fixture.expected_pcm('Точний текст.', punctuation=True)
    reopened = configured_voice_registry(library)
    assert reopened.voices()[0].id == voice.id
    assert reopened.cache_identity(voice.id) == before
    assert before['local_profile_id'] == profile.id and before['adapter'] == helper.ADAPTER


@pytest.mark.parametrize('change', ['disable', 'remove', 'changed-pin'])
def test_registered_helper_rechecks_current_profile_before_every_render(pack, tmp_path, monkeypatch, change):
    from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry
    library = tmp_path / 'library'
    library.mkdir()
    store = EngineProfileStore(library)
    manifest = pack()
    profile = store.register(manifest)
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    registry = configured_voice_registry(library)
    voice = registry.voices()[0].id
    if change == 'disable':
        store.set_enabled(profile.id, False)
    elif change == 'remove':
        store.remove(profile.id)
    else:
        edit(manifest, lambda value: value.update(revision='b' * 64))
    start = Mock(side_effect=AssertionError('Stale profile must not execute'))
    monkeypatch.setattr(engines, '_start_process', start)
    with pytest.raises((ValueError, RuntimeError)):
        registry.synthesize('No output.', tmp_path / 'denied.wav', voice=voice, language='uk')
    start.assert_not_called()
    assert not (tmp_path / 'denied.wav').exists()


def test_two_registered_helpers_keep_distinct_saved_ids_without_silent_rebinding(pack, tmp_path, monkeypatch):
    from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry
    library = tmp_path / 'library'
    library.mkdir()
    store = EngineProfileStore(library)
    one = store.register(pack(), name='First selected helper')
    two = store.register(pack(), name='Second selected helper')
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    registry = configured_voice_registry(library)
    rows = registry.voices()
    assert len(rows) == 2 and len({row.id for row in rows}) == 2
    assert {row.engine for row in rows} == {'local-' + one.id, 'local-' + two.id}
    store.remove(one.id)
    saved = next(row.id for row in rows if row.engine == 'local-' + one.id)
    with pytest.raises(ValueError, match='no longer exists'):
        registry.cache_identity(saved)
    assert configured_voice_registry(library).voices()[0].engine == 'local-' + two.id


def test_helper_machine_registration_and_runtime_are_excluded_from_actual_backup(pack, tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.runtime_settings import EngineProfileStore
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic helper profile backup boundary')
    manifest = pack()
    profile = EngineProfileStore(app.root).register(manifest)
    before = fixture.identity(manifest)
    backup = tmp_path / 'backup'
    digest = app.backup(backup)
    verify_backup(backup, digest)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    assert not (restored.root / 'state/audio/engines.json').exists()
    assert EngineProfileStore(restored.root).snapshot().profiles == ()
    assert not list(restored.root.rglob('helper.py'))
    assert EngineProfileStore(app.root).snapshot().profiles == (profile,)
    assert fixture.identity(manifest) == before
    second = tmp_path / 'second'
    verify_backup(second, restored.backup(second))


def _cleanup_sharing_error(code=32):
    error = PermissionError('Controlled native sharing violation.')
    error.winerror = code
    return error


def _hold_private_directory_without_delete_sharing(path):
    """Use an actual deny-delete Windows directory handle in the Windows gate."""
    if os.name != 'nt':
        return lambda: None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    # FILE_READ_ATTRIBUTES; share read/write, deliberately not delete;
    # OPEN_EXISTING; directory backup semantics and no reparse following.
    handle = create(str(path), 0x80, 0x3, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    retained = [handle]
    def release():
        if retained[0] is not None:
            if not close(retained[0]):
                raise ctypes.WinError(ctypes.get_last_error())
            retained[0] = None
    return release


@pytest.mark.parametrize('cause', ['deadline', 'cancel'])
def test_helper_private_directory_cleanup_waits_for_owned_process_and_reader_settlement(
        pack, tmp_path, monkeypatch, cause):
    from contextlib import contextmanager
    import shutil
    import threading
    from types import SimpleNamespace
    engine, voice = selected(pack, mode='hang-render')
    engine.timeout = 2 if cause == 'deadline' else 15
    real_start, real_temporary = engines._start_process, engines.tempfile.TemporaryFile
    real_reader, real_rmtree = helper.verified_reader, shutil.rmtree
    processes, streams, launch_readers, attempts, locks, threads, cleanup_errors = [], [], [], [], [], [], []
    started, first_refusal, released = Event(), Event(), Event()
    actual_windows = os.name == 'nt'

    def start(command, **options):
        process = real_start(command, **options)
        processes.append(process)
        if Path(options['cwd']).name.startswith('.speech-'):
            started.set()
        return process

    def temporary(*args, **kwargs):
        stream = real_temporary(*args, **kwargs)
        streams.append(stream)
        return stream

    @contextmanager
    def reader(path, **kwargs):
        with real_reader(path, **kwargs) as stream:
            launch_readers.append(stream)
            yield stream

    def remove(path):
        path = Path(path)
        if not path.name.startswith('.speech-'):
            return real_rmtree(path)
        assert started.is_set() and processes
        assert all(process.poll() is not None for process in processes)
        if actual_windows:
            assert all(process._carr_job.handle is None for process in processes)
        assert streams and all(stream.closed for stream in streams)
        assert launch_readers and all(stream.closed for stream in launch_readers)
        attempts.append(path)
        if not locks:
            close = _hold_private_directory_without_delete_sharing(path)
            locks.append(close)
            def release_after_observed_refusal():
                try:
                    if not first_refusal.wait(5):
                        raise AssertionError('No actual post-settlement removal refusal was observed.')
                    close()
                except BaseException as error:
                    cleanup_errors.append(error)
                finally:
                    released.set()
            thread = threading.Thread(target=release_after_observed_refusal)
            threads.append(thread)
            thread.start()
        try:
            if not actual_windows and not released.is_set():
                # POSIX does not impose Windows directory sharing. Only this
                # refusal is synthetic there; all processes/readers stay real.
                raise _cleanup_sharing_error()
            return real_rmtree(path)
        except OSError as error:
            if getattr(error, 'winerror', None) in (32, 33):
                first_refusal.set()
            raise

    monkeypatch.setattr(engines, '_start_process', start)
    monkeypatch.setattr(engines, 'tempfile', SimpleNamespace(TemporaryFile=temporary))
    monkeypatch.setattr(helper, 'verified_reader', reader)
    monkeypatch.setattr(helper, 'shutil', SimpleNamespace(rmtree=remove))
    if not actual_windows:
        monkeypatch.setattr(helper, 'os', SimpleNamespace(name='nt'))
    target, before = tmp_path / 'retained-cleanup.wav', monotonic()
    try:
        with pytest.raises(TimeoutError if cause == 'deadline' else InterruptedError) as failed:
            engine.synthesize('Test.', target, voice=voice, language='uk',
                cancelled=lambda: cause == 'cancel' and started.is_set())
        if cause == 'deadline':
            assert 'selected per-segment time budget' in str(failed.value)
        assert monotonic() - before < 15
        assert first_refusal.is_set() and released.is_set() and len(attempts) >= 2
        assert all(path == attempts[0] for path in attempts)
        assert not cleanup_errors and not target.exists()
        assert not list(tmp_path.glob('.speech-*'))
    finally:
        first_refusal.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        for close in locks:
            close()
    assert all(process.poll() is not None for process in processes)


@pytest.mark.parametrize('code', [32, 33])
def test_private_helper_directory_retries_only_transient_windows_sharing(tmp_path, monkeypatch, code):
    import shutil
    from types import SimpleNamespace
    actual_remove, attempts, sleeps, clock = shutil.rmtree, [], [], [0.0]
    def remove(path):
        attempts.append((Path(path), Path(path).lstat().st_ino))
        if len(attempts) < 3:
            raise _cleanup_sharing_error(code)
        return actual_remove(path)
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(helper, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(helper, 'shutil', SimpleNamespace(rmtree=remove))
    monkeypatch.setattr(helper, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with helper._private_directory(prefix='.speech-', dir=tmp_path) as path:
        (path / 'owned.bin').write_bytes(b'Owned staging only.')
        identity = path.lstat().st_ino
    assert len(attempts) == 3 and all(item == (path, identity) for item in attempts)
    assert len(sleeps) == 2 and all(0 < value <= .02 for value in sleeps)
    assert not path.exists()


@pytest.mark.parametrize('platform,code', [('nt', 5), ('nt', 13), ('posix', 32)])
def test_private_helper_directory_does_not_retry_other_cleanup_errors(tmp_path, monkeypatch, platform, code):
    import shutil
    from types import SimpleNamespace
    attempts, original = [], _cleanup_sharing_error(code)
    def remove(path):
        attempts.append(Path(path))
        raise original
    monkeypatch.setattr(helper, 'os', SimpleNamespace(name=platform))
    monkeypatch.setattr(helper, 'shutil', SimpleNamespace(rmtree=remove))
    monkeypatch.setattr(helper, 'time', SimpleNamespace(monotonic=lambda: 0,
        sleep=lambda seconds: pytest.fail('A non-sharing cleanup error was retried.')))
    path = None
    try:
        with pytest.raises(PermissionError) as failed:
            with helper._private_directory(prefix='.speech-', dir=tmp_path) as path:
                (path / 'retained.bin').write_bytes(b'Retain this failed cleanup.')
        assert failed.value is original and attempts == [path]
        assert (path / 'retained.bin').read_bytes() == b'Retain this failed cleanup.'
    finally:
        if path is not None and path.exists():
            shutil.rmtree(path)


@pytest.mark.parametrize('changed', ['stage', 'parent'])
def test_private_helper_directory_refuses_replaced_ownership(tmp_path, changed):
    import gc
    parent = tmp_path / 'selected-parent'
    parent.mkdir()
    moved = tmp_path / 'displaced-owned-directory'
    with pytest.raises(ValueError, match='changed before cleanup'):
        with helper._private_directory(prefix='.speech-', dir=parent) as path:
            (path / 'owned.bin').write_bytes(b'Original owned data.')
            if changed == 'stage':
                path.rename(moved)
                path.mkdir()
                original = moved / 'owned.bin'
            else:
                parent.rename(moved)
                parent.mkdir()
                path.mkdir()
                original = moved / path.name / 'owned.bin'
            (path / 'foreign.bin').write_bytes(b'Foreign replacement must survive.')
    gc.collect()
    assert original.read_bytes() == b'Original owned data.'
    assert (path / 'foreign.bin').read_bytes() == b'Foreign replacement must survive.'


def test_private_helper_directory_permanent_sharing_error_is_bounded_and_visible(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace
    attempts, waits, clock = [], [], [0.0]
    original = TimeoutError('Original selected inference deadline.')
    def remove(path):
        attempts.append(Path(path))
        raise _cleanup_sharing_error(32)
    def sleep(seconds):
        assert 0 < seconds <= .02
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(helper, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(helper, 'shutil', SimpleNamespace(rmtree=remove))
    monkeypatch.setattr(helper, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    path = None
    try:
        with pytest.raises(PermissionError) as failed:
            with helper._private_directory(prefix='.speech-', dir=tmp_path) as path:
                (path / 'retained.bin').write_bytes(b'Unconfirmed removal must remain visible.')
                raise original
        assert failed.value.winerror == 32 and failed.value.__context__ is original
        assert 2 <= len(attempts) <= 300 and waits and clock[0] == 5
        assert all(item == path for item in attempts)
        assert (path / 'retained.bin').read_bytes() == b'Unconfirmed removal must remain visible.'
    finally:
        if path is not None and path.exists():
            shutil.rmtree(path)
