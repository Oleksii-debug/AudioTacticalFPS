"""Real isolated Dordiai contract children; no private voices, weights or quality claim.

The existing reviewed synthetic installation fixture substitutes only its
interpreter marker. Its helper imports and JSONL/PCM transport run in actual
isolated children. Native cleanup failures below are explicit injected failures
of the existing owner, with real streams, staging and a real broker lease.
"""
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import engines
from carr_library.audio.dordiai_discovery import DISCOVERY_PROTOCOL, parse_inventory
from carr_library.audio.engines import DordiaiVoicesEngine, EngineUnavailable
from carr_library.audio.model_manifest import ModelManifestError
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceTimeout
from tests.audio.test_dordiai_discovery import WORKER, engine, installed, write_manifest


MUTATIONS = {
    'manifest': ('../installation.json',
        "p=Path(__file__).parent.parent/'installation.json'; p.write_text(p.read_text(encoding='utf-8')+' ', encoding='utf-8')"),
    'worker': ('nvda_worker.py',
        "p=Path(__file__); p.write_text(p.read_text(encoding='utf-8')+'\\n# changed during render\\n', encoding='utf-8')"),
    'catalogue': ('models/voices/catalog.json',
        "p=Path(__file__).parent/'models/voices/catalog.json'; p.write_text('{}', encoding='utf-8')"),
    'model': ('models/availability.json',
        "p=Path(__file__).parent/'models/availability.json'; p.write_text('{}', encoding='utf-8')"),
    'routes': ('wide_languages.py',
        "p=Path(__file__).parent/'wide_languages.py'; p.write_text(p.read_text(encoding='utf-8')+'\\n# changed route code\\n', encoding='utf-8')"),
    'unlisted': ('models/new-unreviewed.bin',
        "p=Path(__file__).parent/'models/new-unreviewed.bin'; p.write_bytes(b'unreviewed synthetic asset')"),
}


@pytest.mark.parametrize('mutation', tuple(MUTATIONS))
def test_actual_render_runtime_mutation_never_publishes_pcm(installed, tmp_path, mutation):
    root, runtime = installed
    relative, operation = MUTATIONS[mutation]
    marker = "    assert request['language']=='uk' and request['text']=='Original synthetic sentence.'\n"
    assert WORKER.count(marker) == 1
    (runtime / 'nvda_worker.py').write_text(
        WORKER.replace(marker, marker + '    ' + operation + '\n'), encoding='utf-8')
    provider = engine(installed)
    voice = provider.voices()[0]
    changed = runtime / relative
    before = changed.read_bytes() if changed.exists() else None
    target = tmp_path / 'unaccepted.wav'
    with pytest.raises(ModelManifestError):
        provider.synthesize('Original synthetic sentence.', target, voice=voice.id, language='uk')
    assert changed.exists() and changed.read_bytes() != before
    assert not target.exists()
    assert list(tmp_path.glob('.speech-*')) == []


@pytest.mark.parametrize(('cancel', 'stop_error'), [
    (False, OSError), (True, OSError), (False, KeyboardInterrupt), (True, SystemExit)])
def test_pending_owner_retains_actual_streams_stage_and_lease(
        installed, tmp_path, monkeypatch, cancel, stop_error):
    provider = engine(installed)
    voice = provider.voices()[0]
    broker = ResourceBroker(tmp_path / 'resources', limits=ResourceLimits(
        cpu_slots=1, memory_bytes=1024 * 1024, disk_bytes=10 * 1024 * 1024,
        min_free_disk_bytes=0))
    requested, retry_entered, release, returned = Event(), Event(), Event(), Event()
    processes, streams, attempts, values, errors = [], [], [], [], []
    actual_start, actual_stop = engines._start_process, engines._stop
    target = tmp_path / 'owned-output.wav'

    def start(command, **kwargs):
        process = actual_start(command, **kwargs)
        processes.append(process)
        streams.extend((process.stdin, process.stdout, kwargs['stderr']))
        if cancel:
            requested.set()
        return process

    def stop(process):
        assert len(processes) == 1 and process is processes[0]
        assert all(stream is not None and not stream.closed for stream in streams)
        attempts.append(True)
        if len(attempts) == 1:
            raise stop_error('Synthetic first owned settlement failure.')
        retry_entered.set()
        if not release.wait(10):
            raise OSError('Synthetic owner is still explicitly retained.')
        actual_stop(process)

    monkeypatch.setattr(engines, '_start_process', start)
    monkeypatch.setattr(engines, '_stop', stop)

    def work():
        try:
            with broker.reserve(ResourceRequest(cpu_slots=1),
                                operation_id='dordiai-owned-render', timeout=2):
                values.append(provider.synthesize('Original synthetic sentence.', target,
                    voice=voice.id, language='uk', cancelled=requested.is_set))
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    owner = Thread(target=work, name='synthetic-dordiai-caller')
    owner.start()
    try:
        assert retry_entered.wait(10), errors
        assert owner.is_alive() and not returned.is_set()
        assert len(streams) == 3 and all(not stream.closed for stream in streams)
        staged = list(tmp_path.glob('.speech-*/speech.wav'))
        assert len(staged) == 1 and staged[0].exists() and not target.exists()
        assert broker.snapshot()['usage']['cpu_slots'] == 1
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='contender', timeout=0):
                pytest.fail('The active render lease was released before confirmed cleanup.')
        release.set()
        owner.join(10)
        assert not owner.is_alive() and returned.is_set()
        assert len(attempts) >= 2 and all(stream.closed for stream in streams)
        assert broker.snapshot()['leases'] == []
        assert list(tmp_path.glob('.speech-*')) == []
        if stop_error is OSError and not cancel:
            assert errors == [] and values == [target]
            with wave.open(str(target), 'rb') as pcm:
                assert (pcm.getframerate(), pcm.getnframes()) == (24000, 48)
                assert pcm.readframes(48) == b'\x01\x00' * 48
        else:
            expected = InterruptedError if stop_error is OSError else stop_error
            assert values == [] and len(errors) == 1 and type(errors[0]) is expected
            assert not target.exists()
    finally:
        release.set()
        owner.join(10)
        assert not owner.is_alive(), 'Settle the fixture owner before removing its patched boundary.'


def _availability(runtime, document):
    (runtime / 'models/availability.json').write_text(json.dumps(document), encoding='utf-8')


@pytest.mark.parametrize(('code', 'native', 'wide', 'route'), [
    ('uk', [], [], 'ukrainian'),
    ('en', [], [], 'native_profile'),
    ('pt', ['pt'], [], 'native_locale'),
    ('sk', ['sk'], ['sk'], 'wide'),
    ('en-gb', [], ['en-gb'], 'wide'),
])
def test_actual_installed_routes_control_the_exact_worker_request(
        installed, tmp_path, code, native, wide, route):
    _, runtime = installed
    _availability(runtime, {'installed': [code], 'native_locales': native, 'wide': wide,
                            'builtin': {code: ['fixture-alpha']}})
    source = WORKER.replace("request['language']=='uk'", "request['language']==" + repr(code))
    marker = "    print(json.dumps({'chunk':"
    expected = (route == 'native_profile', route == 'native_locale', route == 'wide')
    assertion = ("    assert (request['nativeProfile'], request['nativeLocale'], "
                 "request['wideLocale']) == " + repr(expected) + "\n"
                 "    assert request['reportLoading'] is True\n")
    assert source.count(marker) == 1
    source = source.replace(marker, assertion + marker)
    (runtime / 'nvda_worker.py').write_text(source, encoding='utf-8')
    provider = engine(installed)
    voice = provider.voices()[0]
    assert voice.languages == (code,)
    identity = provider.cache_identity(voice.id)
    assert identity['discovery'] == 'carr.dordiai-installed-voices.v2'
    assert identity['language_routes'] == {code: route}
    target = provider.synthesize('Original synthetic sentence.', tmp_path / 'routed.wav',
                                voice=voice.id, language=code.upper())
    with wave.open(str(target), 'rb') as pcm:
        assert pcm.getframerate() == 24000 and pcm.getnframes() == 48


@pytest.mark.parametrize('usable', [False, True])
def test_missing_partial_and_unknown_routes_remain_passive_metadata(installed, usable):
    _, runtime = installed
    coverage = [
        {'runtime_code': 'fr', 'settings_label_uk': 'Synthetic partial French',
         'resources_present': False, 'status': 'partial', 'partial_variant': True,
         'interactive_reading_ready': False},
        {'runtime_code': 'zz', 'settings_label_uk': 'Synthetic unsupported route',
         'resources_present': True, 'status': 'present', 'partial_variant': False,
         'interactive_reading_ready': None},
    ]
    if usable:
        coverage.append({'runtime_code': 'uk', 'settings_label_uk': 'Synthetic Ukrainian',
                         'resources_present': True, 'status': 'present'})
    _availability(runtime, {'installed': [], 'coverage': coverage, 'wide': ['fr'],
                            'native_locales': [], 'builtin': {'uk': ['fixture-alpha'],
                                                             'fr': ['fixture-alpha'],
                                                             'zz': ['fixture-alpha']}})
    provider = engine(installed)
    statuses = provider.language_statuses()
    assert statuses[0] == {'code': 'fr', 'name': 'Synthetic partial French',
        'resources_present': False, 'status': 'partial', 'partial_variant': True,
        'interactive_reading_ready': False, 'route': 'wide'}
    assert statuses[1]['code'] == 'zz' and statuses[1]['route'] is None
    assert statuses[1]['resources_present'] is False
    statuses[0]['route'] = 'native_locale'
    assert provider._language_records[0]['route'] == 'wide'
    if usable:
        assert provider.voices()[0].languages == ('uk',)
    else:
        with pytest.raises(EngineUnavailable, match='compatible language resources'):
            provider.voices()


def test_unadvertised_regional_variant_is_not_silently_reduced(installed, tmp_path, monkeypatch):
    _, runtime = installed
    _availability(runtime, {'installed': ['en'], 'builtin': {'en': ['fixture-alpha']}})
    provider = engine(installed)
    voice = provider.voices()[0]
    monkeypatch.setattr(provider, '_stream',
                        lambda *a, **k: pytest.fail('An unadvertised region must not render.'))
    with pytest.raises(EngineUnavailable, match='compatible installed resources'):
        provider.synthesize('Original synthetic sentence.', tmp_path / 'wrong-region.wav',
                            voice=voice.id, language='en-GB')
    assert not (tmp_path / 'wrong-region.wav').exists()


@pytest.mark.parametrize('change', [
    'duplicate', 'unknown-route', 'unhashable-route', 'resources-type',
    'partial-type', 'readiness-type', 'extra-field', 'voice-absent-code',
    'voice-missing-resource', 'present-without-route',
])
def test_route_inventory_refuses_malformed_or_unbound_records(change):
    row = {'code': 'uk', 'name': 'Synthetic Ukrainian', 'resources_present': True,
           'status': 'present', 'partial_variant': False,
           'interactive_reading_ready': None, 'route': 'ukrainian'}
    value = {'protocol': DISCOVERY_PROTOCOL, 'languages': [deepcopy(row)],
             'voices': [{'id': 'fixture-alpha', 'name': 'Synthetic Alpha', 'languages': ['uk']}]}
    current = value['languages'][0]
    if change == 'duplicate':
        value['languages'].append(deepcopy(row))
    elif change == 'unknown-route':
        current['route'] = 'automatic'
    elif change == 'unhashable-route':
        current['route'] = []
    elif change == 'resources-type':
        current['resources_present'] = 1
    elif change == 'partial-type':
        current['partial_variant'] = 'yes'
    elif change == 'readiness-type':
        current['interactive_reading_ready'] = 1
    elif change == 'extra-field':
        current['command'] = 'unreviewed'
    elif change == 'voice-absent-code':
        value['voices'][0]['languages'] = ['en']
    elif change == 'voice-missing-resource':
        current['resources_present'] = False
    elif change == 'present-without-route':
        current['route'] = None
    with pytest.raises(ValueError):
        parse_inventory(json.dumps(value).encode('utf-8'))


@pytest.mark.parametrize('filename', ['windows_runtime.py', 'wide_languages.py'])
def test_missing_reviewed_bootstrap_or_route_helper_never_executes(installed, monkeypatch, filename):
    _, runtime = installed
    (runtime / filename).unlink()
    monkeypatch.setattr(engines, '_run',
                        lambda *a, **k: pytest.fail('An incomplete runtime cannot discover.'))
    with pytest.raises(ModelManifestError, match='not in the verified manifest'):
        engine(installed)


def test_installed_metadata_cannot_change_the_pcm_sample_rate(installed):
    _, runtime = installed
    (runtime / 'nvda_worker.py').write_text(
        WORKER.replace('OUTPUT_RATE = 24000', 'OUTPUT_RATE = 16000'), encoding='utf-8')
    provider = engine(installed)
    with pytest.raises(EngineUnavailable):
        provider.voices()
    assert provider._profiles == {}


def test_discovery_and_render_cannot_select_different_worker_files(installed):
    root, runtime = installed
    selected = runtime / 'other_worker.py'
    (runtime / 'nvda_worker.py').rename(selected)
    manifest = write_manifest(root, runtime, worker=selected.relative_to(root).as_posix())
    with pytest.raises(ModelManifestError, match='same fixed nvda_worker'):
        DordiaiVoicesEngine(manifest)


@pytest.mark.parametrize('thread_name', ['dordiai-voices PCM reader', 'dordiai-voices request writer'])
def test_actual_io_thread_settlement_retains_the_same_cleanup_owner(
        installed, tmp_path, monkeypatch, thread_name):
    from carr_library.audio import cleanup
    provider = engine(installed)
    voice = provider.voices()[0]
    broker = ResourceBroker(tmp_path / 'thread-resources', limits=ResourceLimits(
        cpu_slots=1, memory_bytes=1024 * 1024, disk_bytes=10 * 1024 * 1024,
        min_free_disk_bytes=0))
    at_thread_exit, release, pending_seen, returned = Event(), Event(), Event(), Event()
    streams, values, errors, retained = [], [], [], []
    actual_start, actual_pending = engines._start_process, cleanup.pending_cleanup
    target = tmp_path / 'thread-owned.wav'

    def start(command, **kwargs):
        process = actual_start(command, **kwargs)
        streams.extend((process.stdin, process.stdout, kwargs['stderr']))
        return process

    def thread_factory(*args, **kwargs):
        if kwargs.get('name') == thread_name:
            action = kwargs['target']
            def held_after_io():
                try:
                    action()
                finally:
                    at_thread_exit.set()
                    release.wait(30)
            kwargs['target'] = held_after_io
        return Thread(*args, **kwargs)

    def pending(attempt, **kwargs):
        error = actual_pending(attempt, **kwargs)
        retained.append(error.cleanup_owner)
        pending_seen.set()
        return error

    monkeypatch.setattr(engines, '_start_process', start)
    monkeypatch.setattr(engines, 'threading', SimpleNamespace(Thread=thread_factory, Event=Event))
    monkeypatch.setattr(cleanup, 'pending_cleanup', pending)

    def work():
        try:
            with broker.reserve(ResourceRequest(cpu_slots=1),
                                operation_id='dordiai-thread-owner', timeout=2):
                values.append(provider.synthesize('Original synthetic sentence.', target,
                                                   voice=voice.id, language='uk'))
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    owner = Thread(target=work, name='synthetic-dordiai-thread-caller')
    owner.start()
    try:
        assert at_thread_exit.wait(10), errors
        assert pending_seen.wait(10), errors
        assert len(retained) == 1 and not retained[0].quiescent
        assert owner.is_alive() and not returned.is_set()
        assert len(streams) == 3 and all(not stream.closed for stream in streams)
        assert len(list(tmp_path.glob('.speech-*/speech.wav'))) == 1 and not target.exists()
        assert broker.snapshot()['usage']['cpu_slots'] == 1
        release.set()
        owner.join(10)
        assert not owner.is_alive() and returned.is_set()
        assert retained[0].quiescent
        assert all(stream.closed for stream in streams)
        assert broker.snapshot()['leases'] == []
        assert errors == [] and values == [target]
        assert list(tmp_path.glob('.speech-*')) == []
    finally:
        release.set()
        owner.join(10)
        assert not owner.is_alive(), 'Settle the actual I/O-thread barrier before fixture teardown.'
