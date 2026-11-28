"""Closed synthetic provider protocol: no paid requests or acoustic claims."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from time import time

import pytest

from carr_library.assistant.models import canonical
from carr_library.assistant.providers import Authorization, Gateway, ProviderError, _request
from carr_library.assistant.transcription_provider import (OPERATION, AudioUpload, read_audio,
    response, MAX_RESPONSE_BYTES, MAX_INPUT_BYTES)
from carr_library.assistant.profiles import ProfileStore
from carr_library.backup_producers import _usage

_spec = importlib.util.spec_from_file_location('remote_stt_gateway_fixture',
    Path(__file__).parents[1] / 'transcription' / 'remote_fixture.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def request(tmp_path, monkeypatch, *, mode='APPROVED_REMOTE'):
    fixture.install_gateway(monkeypatch)
    audio = tmp_path / 'private original.wav'
    audio.write_bytes(fixture.RAW)
    _, identity, _ = read_audio(audio)
    profile = fixture.profile()
    payload = {'audio': identity, 'language': 'auto'}
    _, body, _ = _request(profile, OPERATION, payload)
    approval = Authorization('approval', 'task', ((profile.id, profile.endpoint),), (OPERATION,),
        ('selected-audio',), '1.00', 1, time() + 60, (sha256(canonical(body).encode()).hexdigest(),))
    vault = fixture.Vault()
    gateway = Gateway(tmp_path / 'providers', mode=mode, vault=vault)
    options = dict(task_id='task', data_class='selected-audio', authorization=approval,
        max_cost='0.50', operation_id='one', upload=AudioUpload(audio))
    return profile, payload, approval, vault, gateway, options, audio


def test_actual_gateway_uses_exact_multipart_identity_and_existing_usage(tmp_path, monkeypatch):
    profile, payload, approval, vault, gateway, options, audio = request(tmp_path, monkeypatch)
    returned = gateway.request(profile, OPERATION, payload, **options)
    assert returned['text'] == fixture.TEXT and returned['local'] is False
    assert returned['actual_cost'] is None and returned['transcript']['language'] == 'eng'
    method, route, raw, headers = fixture.Connection.requests[0]
    assert method == 'POST' and route == '/v1/speech-to-text?enable_logging=true'
    assert b'filename="selected-audio.wav"' in raw and fixture.RAW in raw
    assert audio.name.encode() not in raw and b'cloud_storage_url' not in raw
    assert b'webhook"\r\n\r\nfalse' in raw and b'no_verbatim"\r\n\r\nfalse' in raw
    assert b'name="model_id"\r\n\r\nscribe_v2' in raw
    assert 'synthetic-stt-secret' == headers['xi-api-key']
    assert headers['Content-Type'].startswith('multipart/form-data; boundary=----resonance-stt-')
    ledger = gateway.ledger.get(approval.id)
    _usage(ledger)
    row = ledger['operations']['one']
    assert row['status'] == 'complete' and row['operation'] == OPERATION
    assert row['request_sha256'] == approval.request_hashes[0]
    assert row['output_sha256'] == returned['output_sha256']
    assert row['usage']['submitted_audio_bytes'] == len(fixture.RAW)
    assert row['usage']['provider_timestamp_count'] == 3
    assert len(vault.calls) == 1 and all(connection.closed for connection in fixture.Connection.instances)
    assert audio.read_bytes() == fixture.RAW


def test_local_only_remains_default_and_never_reads_selected_file_or_vault(tmp_path, monkeypatch):
    profile, payload, _, vault, gateway, options, audio = request(tmp_path, monkeypatch, mode='LOCAL_ONLY')
    audio.unlink()
    with pytest.raises(PermissionError, match='LOCAL ONLY'):
        gateway.request(profile, OPERATION, payload, **options)
    assert not vault.calls and not fixture.Connection.requests
    assert not list(gateway.ledger.directory.glob('*.json'))


@pytest.mark.parametrize('changed', ['hash', 'size', 'language', 'all-hashes', 'calls', 'data', 'expired', 'cost'])
def test_exact_approval_fails_before_credentials_and_transport(tmp_path, monkeypatch, changed):
    profile, payload, approval, vault, gateway, options, _ = request(tmp_path, monkeypatch)
    if changed == 'hash': payload['audio']['sha256'] = '0' * 64
    elif changed == 'size': payload['audio']['size_bytes'] += 1
    elif changed == 'language': payload['language'] = 'uk'
    elif changed == 'all-hashes': options['authorization'] = replace(approval, request_hashes=())
    elif changed == 'calls': options['authorization'] = replace(approval, max_calls=2)
    elif changed == 'data': options['data_class'] = 'selected-script'
    elif changed == 'expired': options['authorization'] = replace(approval, expires_at=0)
    elif changed == 'cost': options['max_cost'] = None
    with pytest.raises((PermissionError, ValueError)):
        gateway.request(profile, OPERATION, payload, **options)
    assert not vault.calls and not fixture.Connection.requests


@pytest.mark.parametrize('kind', ['changed', 'size', 'cancelled'])
def test_reviewed_file_change_and_cancel_remain_not_sent(tmp_path, monkeypatch, kind):
    profile, payload, approval, vault, gateway, options, audio = request(tmp_path, monkeypatch)
    if kind == 'changed': audio.write_bytes(b'X' * len(fixture.RAW))
    elif kind == 'size': audio.write_bytes(fixture.RAW + b'x')
    else: options['cancelled'] = lambda: True
    with pytest.raises((ProviderError, InterruptedError)):
        gateway.request(profile, OPERATION, payload, **options)
    assert gateway.ledger.get(approval.id)['operations']['one']['status'] == 'not_sent'
    assert not vault.calls and not fixture.Connection.requests


@pytest.mark.parametrize('kind', ['redirect', 'type', 'oversize', 'truncated', 'escaped-secret', 'malformed'])
def test_untrusted_response_stays_uncertain_and_cannot_retry(tmp_path, monkeypatch, kind):
    profile, payload, approval, vault, gateway, options, _ = request(tmp_path, monkeypatch)
    if kind == 'redirect':
        factory = lambda: fixture.Response(status=302)
    elif kind == 'type':
        factory = lambda: fixture.Response(content_type='audio/wav')
    elif kind == 'oversize':
        factory = lambda: fixture.Response(declared=str(MAX_RESPONSE_BYTES + 1))
    elif kind == 'truncated':
        factory = lambda: fixture.Response(declared='999999')
    elif kind == 'escaped-secret':
        raw = json.dumps({**fixture.RESPONSE, 'ignored': 'synthetic-stt-secret'}).replace(
            'synthetic-stt-secret', ''.join('\\u%04x' % ord(char) for char in 'synthetic-stt-secret')).encode()
        factory = lambda: fixture.Response(raw)
    else:
        factory = lambda: fixture.Response({**fixture.RESPONSE, 'words': []})
    fixture.Connection.response_factory = staticmethod(factory)
    with pytest.raises(ProviderError):
        gateway.request(profile, OPERATION, payload, **options)
    assert gateway.ledger.get(approval.id)['operations']['one']['status'] == 'uncertain'
    with pytest.raises(ProviderError, match='already been attempted'):
        gateway.request(profile, OPERATION, payload, **options)
    assert len(vault.calls) == 1 and len(fixture.Connection.requests) == 1
    assert all(connection.closed for connection in fixture.Connection.instances)


@pytest.mark.parametrize('change', [
    {'model': 'invented-stt'}, {'voice': 'voice'},
    {'capabilities': (OPERATION, 'speech.synthesize')},
    {'endpoint': 'https://another.example/v1'},
])
def test_dedicated_profile_rejects_other_engine_or_authority(change):
    with pytest.raises(ValueError):
        fixture.profile(**change)


@pytest.mark.parametrize('change', [
    {'words': [{'type': 'word', 'text': 'word', 'start': None, 'end': 1}]},
    {'words': [{'type': 'audio_event', 'text': '(music)', 'start': 0, 'end': 1}]},
    {'transcripts': [{'text': 'unrequested'}]},
    {'edited_transcript': {'text': 'rewritten'}},
    {'channel_index': 0},
    {'audio_duration_secs': .1},
    {'language_probability': float('nan')},
])
def test_timestamp_and_unrequested_vendor_modes_are_refused(change):
    with pytest.raises(ValueError):
        response({**fixture.RESPONSE, **change})


def test_optional_duration_is_explicitly_provider_timeline_not_measured_audio():
    value = dict(fixture.RESPONSE)
    del value['audio_duration_secs']
    result = response(value)
    assert result['transcript']['duration'] == 1.8
    assert result['duration_basis'] == 'end of provider reported word timeline; source duration not measured'
    assert result['text'] == fixture.TEXT


def test_profile_export_never_carries_an_enabled_key_or_executes_provider(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    store.save(fixture.profile())
    exported = json.loads(store.export_json())
    assert exported['profiles'][0]['capabilities'] == [OPERATION]
    assert exported['profiles'][0]['credential_ref'] is None
    assert exported['profiles'][0]['enabled'] is False


def test_upload_limit_refuses_sparse_oversized_source_before_read(tmp_path):
    audio = tmp_path / 'large.wav'
    with audio.open('wb') as stream:
        stream.truncate(MAX_INPUT_BYTES + 1)
    with pytest.raises(ValueError, match='24 MiB'):
        read_audio(audio)


@pytest.mark.parametrize('stage', ['upload', 'vault'])
@pytest.mark.parametrize('change', ['expires', 'local-only', 'cancel'])
def test_active_remote_consent_rechecked_after_upload_and_vault(tmp_path, monkeypatch, stage, change):
    from carr_library.assistant import providers
    profile, payload, approval, vault, gateway, options, _ = request(tmp_path, monkeypatch)
    stopped = [False]
    options['cancelled'] = lambda: stopped[0]
    def revoke():
        if change == 'expires':
            monkeypatch.setattr(providers, 'time', lambda: approval.expires_at + 1)
        elif change == 'local-only':
            gateway.mode = 'LOCAL_ONLY'
        else:
            stopped[0] = True
    if stage == 'upload':
        actual = AudioUpload.read
        def read(upload, expected, **kwargs):
            raw = actual(upload, expected, **kwargs)
            revoke()
            return raw
        monkeypatch.setattr(AudioUpload, 'read', read)
    else:
        actual = vault.get
        def get(*args):
            result = actual(*args)
            revoke()
            return result
        monkeypatch.setattr(vault, 'get', get)
    with pytest.raises((PermissionError, InterruptedError)):
        gateway.request(profile, OPERATION, payload, **options)
    assert len(vault.calls) == (0 if stage == 'upload' else 1)
    assert not fixture.Connection.requests
    assert gateway.ledger.get(approval.id)['operations']['one']['status'] == 'not_sent'
    assert all(connection.closed for connection in fixture.Connection.instances)
