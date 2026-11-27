"""Canonical SAPI behavior through the current engine and owned worker APIs.

COM observations below are synthetic boundary doubles. Actual Windows COM and
PCM are qualified separately by windows_native_sapi_smoke.py.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import engines as api
from carr_library.audio import sapi_native_worker as worker


def engine(monkeypatch):
    value = object.__new__(api.NativeSAPI5Engine)
    value.binary = Path(sys.executable).resolve()
    value.id, value.timeout, value.architecture = 'sapi5', 5, str(struct.calcsize('P') * 8)
    value._records, value._default = {}, None
    monkeypatch.setattr(api, '_native_com_version', lambda: '312')
    return value


def row(leaf='TEST_VOICE', token=None):
    return dict(id=leaf, token=token or 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\' + leaf,
                name='Synthetic installed voice', language='en-US', default=True,
                version='sapi-native-1;pywin32-312')


def header():
    return dict(version=1, architecture=str(struct.calcsize('P') * 8),
                adapter='native-pywin32', client_version='312')


def wave_file(path):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b'\x01\x00' * 1000)


def responses(monkeypatch, mutate=lambda result, request: None):
    observed = []
    def run(command, **options):
        request, reply = Path(command[-2]), Path(command[-1])
        body = json.loads(request.read_bytes())
        observed.append((command, dict(options), body))
        if body['operation'] == 'voices':
            value = {**header(), 'voices': [row()]}
        else:
            wave_file(body['output'])
            value = {**header(), 'status': 'rendered',
                     **{key: body[key] for key in ('id', 'token', 'name')}}
        mutate(value, body)
        reply.write_text(json.dumps(value), encoding='utf-8')
        return b''
    monkeypatch.setattr(api, '_run', run)
    return observed


def test_native_voice_id_preserves_exact_system_speech_token_leaf(monkeypatch):
    value = engine(monkeypatch)
    responses(monkeypatch)
    voice, = value.voices()
    leaf = row()['id']
    assert voice.id == 'sapi5:' + sha256(leaf.encode()).hexdigest()[:24]
    assert voice.id != 'sapi5:' + sha256(row()['token'].encode()).hexdigest()[:24]
    assert voice.controls == ('rate', 'volume', 'punctuation')
    assert value._records[voice.id]['token'] == row()['token']


def test_native_literal_text_and_punctuation_are_only_private_json(monkeypatch, tmp_path):
    value = engine(monkeypatch)
    observed = responses(monkeypatch)
    target = tmp_path / 'accepted.wav'
    text = '<speak>literal</speak> $(not a command) Привіт 😀.'
    voice, = value.voices()
    assert value.synthesize(text, target, voice=voice.id, language='en', punctuation=True) == target
    command, options, body = observed[-1]
    assert command[1:5] == ['-I', '-B', '-X', 'utf8']
    assert not any('powershell' in str(part).lower() for part in command)
    assert text not in ' '.join(map(str, command))
    assert body['text'] == text and body['punctuation'] is True
    assert body['token'] == row()['token'] and body['id'] == row()['id']
    assert body['name'] == row()['name']
    assert not Path(body['output']).exists()
    assert not list(tmp_path.glob('.speech-*')) and not list(tmp_path.glob('.native-sapi-*'))
    with wave.open(str(target), 'rb') as actual:
        assert (actual.getnframes(), actual.getframerate()) == (1000, 22050)
    with pytest.raises(FileExistsError):
        value.synthesize('Same voice.', target, voice=voice.id)
    assert target.exists()


@pytest.mark.parametrize('field,bad', [
    ('version', True), ('version', 2), ('architecture', 'other'), ('adapter', 'other'),
    ('client_version', '999'),
])
def test_wrong_native_host_identity_never_publishes(monkeypatch, tmp_path, field, bad):
    value = engine(monkeypatch)
    responses(monkeypatch, lambda result, request: result.update({field: bad}))
    with pytest.raises(api.EngineUnavailable):
        value.voices()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change', ['leaf', 'duplicate', 'token_control', 'extra'])
def test_ambiguous_or_unbound_native_inventory_is_rejected(monkeypatch, change):
    value = engine(monkeypatch)
    def mutate(result, request):
        if change == 'leaf':
            result['voices'][0]['id'] = 'other'
        elif change == 'duplicate':
            result['voices'].append(dict(result['voices'][0], token='HKEY_CURRENT_USER\\' + row()['id']))
        elif change == 'token_control':
            result['voices'][0]['token'] = 'HKEY_LOCAL_MACHINE\\\n' + row()['id']
        else:
            result['voices'][0]['unreviewed'] = True
    responses(monkeypatch, mutate)
    with pytest.raises(api.EngineUnavailable):
        value.voices()
    assert value._records == {}


@pytest.mark.parametrize('field', ['token', 'id', 'name'])
def test_changed_render_identity_does_not_publish_staged_pcm(monkeypatch, tmp_path, field):
    value = engine(monkeypatch)
    def mutate(result, request):
        if request['operation'] == 'render':
            result[field] = 'another voice'
    responses(monkeypatch, mutate)
    voice, = value.voices()
    target = tmp_path / 'rejected.wav'
    with pytest.raises(api.EngineUnavailable):
        value.synthesize('Selected text.', target, voice=voice.id)
    assert not target.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError])
def test_native_cancellation_or_deadline_keeps_target_absent(monkeypatch, tmp_path, failure):
    value = engine(monkeypatch)
    responses(monkeypatch)
    voice, = value.voices()
    def stop(command, **kwargs):
        body = json.loads(Path(command[-2]).read_bytes())
        wave_file(body['output'])
        raise failure('Synthetic stopped worker after owned cleanup.')
    monkeypatch.setattr(api, '_run', stop)
    target = tmp_path / 'stopped.wav'
    with pytest.raises(failure):
        value.synthesize('Selected text.', target, voice=voice.id)
    assert not target.exists() and not list(tmp_path.iterdir())


def test_native_cache_identity_distinguishes_host_without_relabeling_voice(monkeypatch):
    value = engine(monkeypatch)
    responses(monkeypatch)
    voice, = value.voices()
    record = value.cache_identity(voice.id)
    assert record['voice'] == sha256(row()['id'].encode()).hexdigest()
    assert record['native_token_sha256'] == sha256(row()['token'].encode()).hexdigest()
    assert record['native_protocol'] == 'carr-sapi-native/1'
    assert record['com_client_version'] == '312'
    helper = Path(worker.__file__).resolve()
    assert record['native_worker_sha256'] == sha256(helper.read_bytes()).hexdigest()
    # A synthetic host only for comparing the historical cache schema.
    monkeypatch.setattr(api, '_system_powershell', lambda path=None, **options: Path(sys.executable).resolve())
    legacy = api.SAPI5Engine(Path(sys.executable).resolve())
    legacy._records, legacy._default = value._records, voice.id
    assert legacy.cache_identity(voice.id) != record
    assert legacy._record(voice.id)['id'] == value._record(voice.id)['id']


def request(directory):
    return dict(version=1, operation='render', token=row()['token'], id=row()['id'], name=row()['name'],
                text='Literal words.', output=str(directory / 'speech.wav'),
                rate=0, volume=80, punctuation=False)


@pytest.mark.parametrize('field,bad', [
    ('version', True), ('operation', 'shell'), ('id', 'different'), ('token', ''), ('name', ''),
    ('text', '<speak>\0bad</speak>'), ('rate', True), ('rate', 11), ('volume', -1),
    ('punctuation', 1),
])
def test_worker_rejects_invalid_request_before_com(tmp_path, field, bad):
    value = request(tmp_path)
    value[field] = bad
    with pytest.raises(ValueError):
        worker._validate(value, tmp_path)


def test_worker_refuses_existing_or_external_output(tmp_path):
    value = request(tmp_path)
    Path(value['output']).write_bytes(b'existing')
    with pytest.raises(ValueError):
        worker._validate(value, tmp_path)
    assert Path(value['output']).read_bytes() == b'existing'
    value['output'] = str(tmp_path.parent / 'speech.wav')
    with pytest.raises(ValueError):
        worker._validate(value, tmp_path)


def test_worker_import_does_not_initialize_com():
    helper = Path(worker.__file__).resolve()
    completed = subprocess.run([sys.executable, '-I', '-B', '-c',
        'import runpy,sys; runpy.run_path(sys.argv[1]); '
        'assert "pythoncom" not in sys.modules and "win32com" not in sys.modules',
        str(helper)], timeout=15, capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode('utf-8', errors='replace')


class Token:
    def __init__(self, leaf='TEST_VOICE', prefix='HKEY_LOCAL_MACHINE'):
        self.Id = prefix + '\\Speech\\Voices\\Tokens\\' + leaf
    def GetAttribute(self, name):
        return {'Name': 'Synthetic installed voice', 'Language': '409;422'}[name]
    def GetDescription(self, language):
        return 'Synthetic installed voice'


@pytest.mark.parametrize('punctuation,flags', [(False, 16), (True, 80)])
def test_fixed_com_classes_and_literal_flags_preserve_selected_voice(tmp_path, monkeypatch, punctuation, flags):
    token = Token()
    calls = []
    stream = SimpleNamespace(Open=lambda *args: calls.append(('open', args)),
                             Close=lambda: calls.append(('close',)))
    speaker = SimpleNamespace(Voice=token, GetVoices=lambda *args: SimpleNamespace(Count=1, Item=lambda index: token),
                              Speak=lambda text, value: calls.append(('speak', text, value)))
    objects = {'SAPI.SpVoice': speaker, 'SAPI.SpFileStream': stream,
               'SAPI.SpAudioFormat': SimpleNamespace(Type=0)}
    monkeypatch.setattr(worker, '_dispatch', lambda name, *unused: (calls.append(('create', name)), objects[name])[1])
    value = request(tmp_path)
    value.update(token=token.Id, punctuation=punctuation)
    result = worker._perform(worker._validate(value, tmp_path), ['start'], object(), object())
    assert [call[1] for call in calls if call[0] == 'create'] == list(objects)
    assert ('speak', value['text'], flags) in calls
    assert calls[-1] == ('close',)
    assert result['token'] == token.Id and result['id'] == row()['id']
    assert (speaker.Rate, speaker.Volume) == (0, 80)
    assert objects['SAPI.SpAudioFormat'].Type == 22
    assert speaker.AllowAudioOutputFormatChangesOnNextSet is False


def test_duplicate_com_token_leaves_are_refused_before_render(tmp_path, monkeypatch):
    tokens = [Token(prefix='HKEY_LOCAL_MACHINE'), Token(prefix='HKEY_CURRENT_USER')]
    speaker = SimpleNamespace(Voice=tokens[0],
        GetVoices=lambda *args: SimpleNamespace(Count=2, Item=lambda index: tokens[index]))
    monkeypatch.setattr(worker, '_dispatch', lambda *args: speaker)
    with pytest.raises(ValueError, match='ambiguous'):
        worker._perform({'version': 1, 'operation': 'voices'}, ['start'], object(), object())


def test_default_windows_discovery_never_constructs_powershell(monkeypatch):
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(api, 'NativeSAPI5Engine', lambda: SimpleNamespace(id='sapi5'))
    monkeypatch.setattr(api, 'SAPI5Engine', lambda *args, **kwargs: pytest.fail('Implicit PowerShell host.'))
    monkeypatch.setattr(api, '_windows_directory', lambda: pytest.fail('Eager compatibility-host discovery.'))
    monkeypatch.setattr(api.shutil, 'which', lambda name: None)
    registry = api.default_registry()
    assert list(registry.engines) == ['sapi5', 'sapi5-32']
    assert registry.engines['sapi5-32'].voices() == ()


@pytest.mark.parametrize('program', ['espeak-ng', 'espeak'])
def test_explicit_installed_espeak_fallback_keeps_distinct_engine_ids(monkeypatch, program):
    binary = str(Path(sys.executable).resolve())
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='posix', environ={}))
    monkeypatch.setattr(api.shutil, 'which', lambda name: binary if name == program else None)
    registry = api.default_registry()
    assert list(registry.engines) == [program]
    assert registry.engines[program].binary == Path(binary)


@pytest.mark.parametrize('punctuation', [False, True])
def test_espeak_punctuation_is_a_fixed_flag_and_default_options_stay_unchanged(tmp_path, monkeypatch, punctuation):
    value = api.ESpeakEngine(Path(sys.executable).resolve())
    value._voices = {'espeak-ng:en': api.Voice('espeak-ng:en', 'English', ('en',), 'espeak-ng',
                                            controls=('rate', 'pitch', 'volume', 'punctuation'))}
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        wave_file(command[command.index('-w') + 1])
        return b''
    monkeypatch.setattr(api, '_run', run)
    text = 'Literal --command, words.'
    value.synthesize(text, tmp_path / 'spoken.wav', voice='espeak-ng:en', punctuation=punctuation)
    command, options = commands[0]
    assert ('--punct' in command) is punctuation
    assert command.count('--punct') == int(punctuation)
    assert text not in command and options['data'] == text.encode()


@pytest.mark.parametrize('punctuation', [1, None, 'true'])
def test_untyped_punctuation_cannot_reach_an_engine(tmp_path, punctuation):
    with pytest.raises(ValueError, match='boolean'):
        api.VoiceRegistry().synthesize('Test', tmp_path / 'out.wav',
                                      voice='missing:voice', punctuation=punctuation)


def test_saved_32_bit_voice_resolves_only_after_explicit_identity_selection(monkeypatch, tmp_path):
    selected = 'sapi5-32:' + 'a' * 24
    creations, calls = [], []
    class ExistingCompatibility:
        id = 'sapi5-32'
        _default = selected
        def __init__(self, binary=None, *, engine_id):
            creations.append((binary, engine_id))
        def voices(self, *, cancelled):
            cancelled()
            calls.append('voices')
            return (api.Voice(selected, 'Exact old 32-bit voice', ('en',), self.id),)
        def cache_identity(self, voice, *, cancelled):
            cancelled()
            assert voice == selected
            calls.append('identity')
            return {'old_voice': voice}
        def synthesize(self, text, output, *, voice, cancelled, **settings):
            cancelled()
            assert voice == selected
            calls.append('render')
            wave_file(output)
            return output
    monkeypatch.setattr(api, 'SAPI5Engine', ExistingCompatibility)
    registry = api.VoiceRegistry((api._LazySAPI32Engine(),))
    assert registry.voices() == () and creations == [] and calls == []
    assert registry.cache_identity(selected) == {'old_voice': selected}
    assert len(creations) == 1 and creations[0][1] == 'sapi5-32'
    assert registry.selected_voice(selected).id == selected
    assert registry.synthesize('Explicit old role.', tmp_path / 'selected.wav', voice=selected).exists()
    assert calls == ['identity', 'voices', 'render']
    assert len(creations) == 1


@pytest.mark.parametrize('voice', ['sapi5-32:missing', 'sapi5-32:' + 'A' * 24])
def test_invalid_saved_32_bit_identity_never_constructs_a_compatibility_host(monkeypatch, voice):
    monkeypatch.setattr(api, 'SAPI5Engine', lambda *args, **kwargs: pytest.fail('Invalid identity launched.'))
    registry = api.VoiceRegistry((api._LazySAPI32Engine(),))
    with pytest.raises(api.EngineUnavailable, match='exact saved'):
        registry.cache_identity(voice)


def test_absent_selected_32_bit_voice_never_chooses_a_different_installed_voice(monkeypatch):
    wanted, other = 'sapi5-32:' + 'a' * 24, 'sapi5-32:' + 'b' * 24
    monkeypatch.setattr(api, 'SAPI5Engine', lambda *args, **kwargs: SimpleNamespace(
        voices=lambda **options: (api.Voice(other, 'Different voice', ('en',), 'sapi5-32'),)))
    with pytest.raises(api.EngineUnavailable, match='No substitute'):
        api.VoiceRegistry((api._LazySAPI32Engine(),)).selected_voice(wanted)


@pytest.mark.parametrize('name', ['SAPI.SpVoice', 'SAPI.SpFileStream', 'SAPI.SpAudioFormat'])
def test_fixed_factory_uses_supported_pywin32_progid_contract(name):
    calls = []
    interface, wrapped, inproc, dispatch_iid = object(), object(), object(), object()
    def create(*args):
        calls.append(('create', args))
        return interface
    def wrap(value, **options):
        calls.append(('wrap', value, options))
        return wrapped
    # Deliberately no CLSIDFromProgID: the pinned pywin32 client exposes
    # ProgID conversion through CoCreateInstance's PyWinObject_AsIID argument.
    pythoncom = SimpleNamespace(CoCreateInstance=create,
                                CLSCTX_INPROC_SERVER=inproc, IID_IDispatch=dispatch_iid)
    dynamic = SimpleNamespace(Dispatch=wrap)
    assert worker._dispatch(name, pythoncom, dynamic) is wrapped
    assert calls == [('create', (name, None, inproc, dispatch_iid)),
                     ('wrap', interface, {'userName': name})]


def test_fixed_factory_rejects_foreign_class_before_com_admission():
    with pytest.raises(ValueError, match='fixed local SAPI'):
        worker._dispatch('WScript.Shell', SimpleNamespace(), SimpleNamespace())


def test_native_engine_stop_waits_for_existing_job_accounting(monkeypatch):
    import ctypes
    from carr_library.audio import recording_process
    events = []
    active = iter((1, 0))
    def query(handle, kind, address, size, returned):
        assert handle == 17 and kind == 1
        accounting = ctypes.cast(address, ctypes.POINTER(recording_process._Accounting)).contents
        accounting.active_processes = next(active)
        events.append(('active', accounting.active_processes))
        return 1
    def terminate(handle, code):
        assert handle == 17 and code == 1
        events.append('terminate-family')
        return 1
    def close(handle):
        assert handle == 17
        events.append('close-empty-job')
        return 1
    job = SimpleNamespace(handle=17, api=SimpleNamespace(kernel=SimpleNamespace(
        TerminateJobObject=terminate, QueryInformationJobObject=query, CloseHandle=close)))
    def wait(*, timeout):
        assert timeout == 5
        events.append('leader-exited')
        return 0
    process = SimpleNamespace(_carr_job=job, wait=wait,
                              _handle=SimpleNamespace(Close=lambda: events.append('close-leader-handle')))
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(recording_process, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(recording_process, 'time', SimpleNamespace(
        monotonic=lambda: 0, sleep=lambda delay: events.append(('wait', delay))))
    api._stop(process)
    assert events == ['terminate-family', 'leader-exited', 'close-leader-handle',
                      ('active', 1), ('wait', .02), ('active', 0), 'close-empty-job']
    assert job.handle is None


def _pending_engine_stop(monkeypatch, *, stop_error, cancel):
    from threading import Event, Thread
    streams, results, errors = [], [], []
    retry_entered, release, returned = Event(), Event(), Event()
    process = SimpleNamespace(returncode=0, poll=lambda: 0)
    def start(command, **options):
        streams.extend(options[name] for name in ('stdin', 'stdout', 'stderr'))
        streams[1].write(b'public synthetic result')
        return process
    attempts = []
    def stop(current):
        assert current is process and all(not stream.closed for stream in streams)
        attempts.append(True)
        if len(attempts) == 1:
            raise stop_error('Synthetic first native settlement failure.')
        retry_entered.set()
        if not release.wait(5):
            raise OSError('Synthetic cleanup owner has not been released.')
    monkeypatch.setattr(api, '_start_process', start)
    monkeypatch.setattr(api, '_stop', stop)
    def work():
        try:
            results.append(api._run([Path(sys.executable).resolve(), '-I', '-c', 'pass'],
                                    cancelled=lambda: bool(streams) and cancel, timeout=5))
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()
    owner = Thread(target=work)
    owner.start()
    try:
        assert retry_entered.wait(5)
        assert owner.is_alive() and not returned.is_set()
        assert len(streams) == 3 and all(not stream.closed for stream in streams)
        release.set()
        owner.join(5)
        assert not owner.is_alive() and returned.is_set()
        assert all(stream.closed for stream in streams)
        return results, errors
    finally:
        release.set()
        owner.join(5)
        assert not owner.is_alive(), 'The synthetic cleanup must finish before monkeypatch teardown.'


@pytest.mark.parametrize('stop_error', [OSError, KeyboardInterrupt, SystemExit])
def test_native_engine_cancel_retains_streams_until_pending_cleanup_settles(monkeypatch, stop_error):
    results, errors = _pending_engine_stop(monkeypatch, stop_error=stop_error, cancel=True)
    assert results == [] and len(errors) == 1
    expected = InterruptedError if stop_error is OSError else stop_error
    assert type(errors[0]) is expected


def test_native_engine_success_cannot_return_before_pending_cleanup_settles(monkeypatch):
    results, errors = _pending_engine_stop(monkeypatch, stop_error=OSError, cancel=False)
    assert errors == [] and results == [b'public synthetic result']
