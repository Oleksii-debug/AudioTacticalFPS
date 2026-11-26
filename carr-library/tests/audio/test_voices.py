"""Adapter boundaries with explicit engine doubles; not speech-quality evidence."""
import hashlib
import json
from pathlib import Path
import struct
import wave

import pytest

from carr_library.audio import voices


def row(**updates):
    return dict(id='installed-token', name='Installed synthetic voice', language='en-US',
                gender='Female', age='Adult', description='Fixture', **updates)


def pcm(path, *, rate=24000, channels=1, frames=2400):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, frames, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 3000)*channels*frames)


def engine(monkeypatch, reply=None, *, rate=24000, truncate=False):
    requests = []

    def run(request, **kwargs):
        requests.append(request)
        pcm(request['output'], rate=rate)
        if truncate:
            path = Path(request['output'])
            path.write_bytes(path.read_bytes()[:-20])
        return reply or {'status':'complete', 'voice_id':request['voice_id']}

    monkeypatch.setattr(voices, '_run', run)
    return requests


def test_installed_voices_are_discovered_without_a_fixed_catalogue(monkeypatch):
    entries = [row(), {**row(), 'id':'other-token','name':'Iný hlas','language':'sk-SK'}]
    calls = []
    monkeypatch.setattr(voices, '_run', lambda request, **kwargs:
        calls.append(request) or {'voices':entries})
    actual = voices.list_voices()
    assert [v.id for v in actual] == ['installed-token','other-token']
    assert [v.language for v in actual] == ['en-US','sk-SK']
    assert calls == [{'operation':'voices'}]


@pytest.mark.parametrize('catalogue', [None, {}, [row(), row()], [{'id':'missing'}],
    [{**row(), 'name':''}], [{**row(), 'language':3}], [{**row(), 'id':'bad\0id'}]])
def test_bad_voice_catalogue_fails_closed(monkeypatch, catalogue):
    monkeypatch.setattr(voices, '_run', lambda *args, **kwargs: {'voices':catalogue})
    with pytest.raises(ValueError):
        voices.list_voices()


def test_empty_catalogue_is_honest(monkeypatch):
    monkeypatch.setattr(voices, '_run', lambda *args, **kwargs: {'voices':[]})
    assert voices.list_voices() == ()


def test_non_windows_reports_unavailable_before_starting_a_process(monkeypatch):
    monkeypatch.setattr(voices.sys, 'platform', 'linux')
    monkeypatch.setattr(voices.subprocess, 'Popen', lambda *a, **k: pytest.fail('No process expected'))
    with pytest.raises(RuntimeError, match='require Windows'):
        voices.list_voices()


def test_text_is_data_and_exact_wav_is_atomically_published(monkeypatch, tmp_path):
    calls = engine(monkeypatch)
    text = 'Українська / Slovenský text. <speak> & $(Invoke-Something) "quoted"'
    output = tmp_path/'Голос ž.wav'
    result = voices.render_speech(text, output, voice_id='installed-token', rate=2, volume=80)
    assert result.path == output and result.frames == 2400 and result.sample_rate == 24000
    assert result.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert calls[0]['text'] == text and calls[0]['rate'] == 2 and calls[0]['volume'] == 80
    assert output.stat().st_nlink == 1
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize('changes', [dict(text=''), dict(text='x'*12001), dict(text='bad\0text'),
    dict(text='bad\ud800'), dict(voice_id=''), dict(rate=True), dict(rate=11),
    dict(volume=-1), dict(volume=101), dict(volume=1.0)])
def test_invalid_request_never_invokes_engine(monkeypatch, tmp_path, changes):
    monkeypatch.setattr(voices, '_run', lambda *a, **k: pytest.fail('Engine must not run'))
    options = dict(text='Source words', output=tmp_path/'new.wav', voice_id='installed-token')
    options.update(changes)
    with pytest.raises(ValueError):
        voices.render_speech(**options)
    assert not list(tmp_path.iterdir())


def test_existing_output_and_symlink_parent_are_never_modified(monkeypatch, tmp_path):
    monkeypatch.setattr(voices, '_run', lambda *a, **k: pytest.fail('Engine must not run'))
    target = tmp_path/'existing.wav'
    target.write_bytes(b'Keep these exact bytes')
    with pytest.raises(FileExistsError):
        voices.render_speech('Text', target, voice_id='installed-token')
    assert target.read_bytes() == b'Keep these exact bytes'
    if hasattr(Path, 'symlink_to'):
        link = tmp_path/'alias'
        try:
            link.symlink_to(tmp_path, target_is_directory=True)
        except OSError:
            return  # Windows without symlink privilege still verifies no-overwrite above.
        with pytest.raises(ValueError, match='without links'):
            voices.render_speech('Text', link/'new.wav', voice_id='installed-token')


@pytest.mark.parametrize('problem', ['wrong_voice','wrong_rate','truncated'])
def test_engine_output_is_verified_before_publication(monkeypatch, tmp_path, problem):
    engine(monkeypatch,
           reply={'status':'complete','voice_id':'another'} if problem=='wrong_voice' else None,
           rate=16000 if problem=='wrong_rate' else 24000, truncate=problem=='truncated')
    with pytest.raises(ValueError):
        voices.render_speech('Text', tmp_path/'new.wav', voice_id='installed-token')
    assert not list(tmp_path.iterdir())


def test_cancellation_after_engine_success_does_not_publish(monkeypatch, tmp_path):
    stopped = False

    def run(request, **kwargs):
        nonlocal stopped
        pcm(request['output'])
        stopped = True
        return {'voice_id':request['voice_id'], 'status':'complete'}

    monkeypatch.setattr(voices, '_run', run)
    with pytest.raises(InterruptedError):
        voices.render_speech('Text', tmp_path/'new.wav', voice_id='installed-token',
                             cancelled=lambda:stopped)
    assert not list(tmp_path.iterdir())


def test_racing_output_is_preserved(monkeypatch, tmp_path):
    target = tmp_path/'new.wav'

    def run(request, **kwargs):
        pcm(request['output'])
        target.write_bytes(b'Another operation owns this path')
        return {'voice_id':request['voice_id'], 'status':'complete'}

    monkeypatch.setattr(voices, '_run', run)
    with pytest.raises(FileExistsError):
        voices.render_speech('Text', target, voice_id='installed-token')
    assert target.read_bytes() == b'Another operation owns this path'

