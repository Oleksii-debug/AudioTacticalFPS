"""Real pipe and OS-lock boundaries for the existing dordiai adapter.

The actor is the existing synthetic protocol fixture. These tests do not load
voice models, qualify pronunciation or copy owner runtime implementation.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event, Thread

import pytest

from carr_library.audio.models import AudioCleanupPending
from carr_library.audio.voice_ownership import capture_voice_ownership, hold_voice_ownership
from carr_library.integrations import dordiai_voices as api
from carr_library.integrations.dordiai_speech import DordiaiSpeechProvider, install_dordiai_provider
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech.runtime import VoiceRegistry
from .test_dordiai_voices import ACTOR, runtime, requests


def owner_namespace(runtime, *, active=None, inactive=None):
    root = runtime / 'models/voices/packages'
    root.mkdir()
    (root / '.voice-packages.lock').write_bytes(b'0')
    value = {'format_version': 2, 'active': active or {}, 'inactive': inactive or {}}
    (root / 'voice-packages.json').write_bytes(json.dumps(value).encode('utf-8'))
    return root


def selected(runtime):
    return api.DordiaiVoicesAdapter(runtime, python_executable=sys.executable,
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest(), request_timeout=5)


def test_legacy_enumeration_keeps_original_ids_and_creates_no_owner_namespace(runtime):
    with _provider(runtime) as provider:
        voices = provider.voices()
        assert len(voices) == 5
        installation = sha256(os.path.normcase(str(runtime.resolve())).encode()).hexdigest()
        for voice in voices:
            profile, language = provider._tokens[voice.id]
            key = json.dumps([installation, profile, language], ensure_ascii=True, separators=(',', ':'))
            assert voice.id == 'dordiai-voices:' + sha256(key.encode('ascii')).hexdigest()
            assert provider._token_authority[voice.id] is None
        assert not (runtime / 'models/voices/packages').exists()
        assert provider.adapter.capabilities().managed_asset_versions == ()
        assert not (runtime / 'models/voices/packages').exists()


from contextlib import contextmanager
@contextmanager
def _provider(runtime, **kwargs):
    provider = DordiaiSpeechProvider(runtime, python_executable=sys.executable,
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest(), **kwargs)
    try:
        yield provider
    finally:
        provider.close()


@pytest.mark.parametrize('state', ['active', 'inactive'])
@pytest.mark.parametrize('profile', ['fixture_one', 'fixture_managed'])
def test_unbound_adapter_never_falls_back_for_any_owned_profile(runtime, state, profile):
    owner_namespace(runtime, **{state: {profile: '1.0.0'}})
    with selected(runtime) as adapter:
        identities = {voice.id for voice in adapter.voices()}
        assert profile not in identities
        assert identities == {'fixture_one', 'fixture_managed'} - {profile}
        with pytest.raises(ValueError, match='lacks a current version-bound approval'):
            list(adapter.stream_pcm('Denied owned reference.', profile, 'uk'))
        assert adapter._worker is None
        assert not (runtime / 'requests.jsonl').exists()


@pytest.mark.parametrize('ending', ['complete', 'close', 'cancel'])
def test_package_stream_holds_actual_owner_lock_until_child_is_quiescent(runtime, ending):
    owner_namespace(runtime, active={'fixture_managed': '1.0.0'})
    ticket = capture_voice_ownership(runtime)
    with selected(runtime) as adapter:
        cancelled = Event()
        stream = adapter.stream_pcm('Lease fixture.', 'fixture_one', 'uk', cancelled=cancelled)
        try:
            assert next(stream).frames == 3
            child = adapter._worker
            assert child.process.poll() is None
            with pytest.raises(TimeoutError):
                with hold_voice_ownership(ticket, timeout=0):
                    pytest.fail('An active speech child cannot release its owner lock.')
            if ending == 'complete':
                assert [chunk.frames for chunk in stream] == [3]
            elif ending == 'cancel':
                cancelled.set()
                with pytest.raises(api.DordiaiCancelled):
                    next(stream)
            else:
                stream.close()
            assert child.process.poll() is not None and child._cleanup_done
            assert adapter._worker is None
            with hold_voice_ownership(ticket, timeout=0) as lease:
                lease.checkpoint()
            assert not adapter._authority._gate.locked()
        finally:
            stream.close()


def test_actual_pending_child_cleanup_keeps_package_lease_until_native_retry_finishes(runtime, monkeypatch):
    owner_namespace(runtime, active={'fixture_managed': '1.0.0'})
    ticket = capture_voice_ownership(runtime)
    with selected(runtime) as adapter:
        stream = adapter.stream_pcm('Retained cleanup fixture.', 'fixture_one', 'uk')
        assert next(stream).frames == 3
        child, observations, calls = adapter._worker, [], []
        original = child.close
        def delayed_close():
            calls.append(True)
            if len(calls) == 1:
                raise api.DordiaiError('Synthetic first native close refusal.')
            locked = False
            try:
                with hold_voice_ownership(ticket, timeout=0):
                    pass
            except TimeoutError:
                locked = True
            observations.append((locked, child.process.poll() is None, adapter._authority._lease.held))
            return original()
        monkeypatch.setattr(child, 'close', delayed_close)
        try:
            with pytest.raises(AudioCleanupPending) as caught:
                list(stream)
            assert caught.value.cleanup_owner.quiescent
            assert observations and all(row == (True, True, True) for row in observations)
            assert child.process.poll() is not None and child._cleanup_done
            assert adapter._worker is None
            with hold_voice_ownership(ticket, timeout=0):
                pass
        finally:
            monkeypatch.setattr(child, 'close', original)
            stream.close()
            original()


def test_first_management_namespace_appearance_retires_existing_legacy_worker(runtime):
    with selected(runtime) as adapter:
        stream = adapter.stream_pcm('Existing legacy stream.', 'fixture_one', 'uk')
        assert next(stream).frames == 3
        child = adapter._worker
        owner_namespace(runtime, active={'new_profile': '1.0.0'})
        try:
            with pytest.raises(ValueError, match='ownership changed'):
                next(stream)
            assert child.process.poll() is not None and child._cleanup_done
            assert adapter._worker is None
            before = requests(runtime)
            with pytest.raises(ValueError, match='authority changed'):
                adapter.voices()
            assert requests(runtime) == before
        finally:
            stream.close()


def test_index_mutation_after_pcm_revokes_current_stream_before_another_chunk(runtime):
    root = owner_namespace(runtime, active={'fixture_managed': '1.0.0'})
    with selected(runtime) as adapter:
        stream = adapter.stream_pcm('Current source guard.', 'fixture_one', 'uk')
        assert next(stream).frames == 3
        child = adapter._worker
        (root / 'voice-packages.json').write_bytes(json.dumps({
            'format_version': 2, 'active': {}, 'inactive': {'fixture_one': '1.0.0'}}).encode())
        try:
            with pytest.raises(ValueError, match='version changed'):
                next(stream)
            assert child.process.poll() is not None and child._cleanup_done
            assert [row['mode'] for row in requests(runtime)].count('reference') == 1
        finally:
            stream.close()


@pytest.mark.parametrize('changed', ['lock', 'packages', 'references'])
def test_same_bytes_do_not_reauthorize_replaced_owner_scope(runtime, changed):
    root = owner_namespace(runtime, active={'fixture_managed': '1.0.0'})
    with selected(runtime) as adapter:
        target = {'lock': root / '.voice-packages.lock', 'packages': root,
                  'references': root.parent}[changed]
        saved = target.with_name(target.name + '-retired')
        target.rename(saved)
        if saved.is_dir():
            shutil.copytree(saved, target)
        else:
            target.write_bytes(saved.read_bytes())
        assert target.stat().st_ino != saved.stat().st_ino
        with pytest.raises(ValueError):
            adapter.voices()
        assert adapter._probe_child is None and adapter._worker is None
        assert not (runtime / 'requests.jsonl').exists()
        assert (saved.read_bytes() if saved.is_file() else b'0') == b'0'


def test_owner_wait_cancellation_starts_no_probe_and_never_reclaims_the_other_lease(runtime):
    owner_namespace(runtime, active={'fixture_managed': '1.0.0'})
    ticket = capture_voice_ownership(runtime)
    stopped, waiting, errors = Event(), Event(), []
    with selected(runtime) as adapter, hold_voice_ownership(ticket, timeout=0) as owner:
        def cancelled():
            waiting.set()
            return stopped.is_set()
        def read():
            try:
                adapter.voices(cancelled=cancelled)
            except BaseException as error:
                errors.append(error)
        thread = Thread(target=read)
        thread.start()
        try:
            assert waiting.wait(3)
            stopped.set()
            thread.join(5)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], api.DordiaiCancelled)
            owner.checkpoint()
            assert owner.held and adapter._probe_child is None
            assert not (runtime / 'requests.jsonl').exists()
        finally:
            stopped.set()
            thread.join(5)


@pytest.mark.parametrize('action', ['disabled', 'changed-worker'])
def test_unmanaged_factory_still_checks_current_library_configuration_before_cache(runtime, tmp_path, action):
    state = tmp_path / 'current-library/state'
    state.mkdir(parents=True)
    settings = load_settings(state)
    row = {'enabled': True, 'runtime_directory': str(runtime), 'python_executable': sys.executable,
           'expected_worker_sha256': sha256(ACTOR.encode()).hexdigest()}
    settings['dordiai'] = row
    save_settings(state, settings)
    registry = VoiceRegistry()
    provider = install_dordiai_provider(registry, row, state_root=state)
    try:
        voice = next(v for v in registry.voices() if v.language == 'uk')
        first = tmp_path / 'first.wav'
        registry.synthesize('Actual legacy fixture.', voice.id, first)
        child = provider.adapter._worker
        before = first.read_bytes()
        changed = load_settings(state)
        expected = deepcopy(changed['dordiai'])
        changed['dordiai']['enabled' if action == 'disabled' else 'expected_worker_sha256'] = (
            False if action == 'disabled' else 'a' * 64)
        save_settings(state, changed, expected_dordiai=expected)
        with pytest.raises(ValueError, match='configuration changed'):
            provider.fingerprint(voice.id)
        assert child.process.poll() is not None and child._cleanup_done
        assert first.read_bytes() == before
        with pytest.raises(ValueError):
            registry.synthesize('Stale queued fixture.', voice.id, tmp_path / 'stale.wav')
        assert not (tmp_path / 'stale.wav').exists()
    finally:
        provider.close()


@pytest.mark.parametrize('operation', ['checkpoint', 'profile', 'inventory', 'capability', 'lock'])
def test_authority_metadata_cannot_escape_its_active_worker_scope(runtime, operation):
    with selected(runtime) as adapter:
        authority = adapter._authority
        call = {'checkpoint': authority.checkpoint,
                'profile': lambda: authority.profile_identity('fixture_one'),
                'inventory': lambda: authority.filter_inventory({'voices': []}),
                'capability': authority.capability_identity,
                'lock': lambda: authority.lock_digest(runtime / 'unrelated')}[operation]
        with pytest.raises(ValueError, match='active operation scope'):
            call()
        assert not (runtime / 'models/voices/packages').exists()
