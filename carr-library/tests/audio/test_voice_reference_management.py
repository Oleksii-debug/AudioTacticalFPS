"""Actual PCM/FFprobe and labelled synthetic metadata owner; no acoustic claim."""
from array import array
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import wave
import zipfile

import pytest

from carr_library.audio import cleanup, dsp, process, voice_reference as reference
from carr_library.audio.dsp import AudioRuntime
from carr_library.audio.voice_packages import VoicePackageManager
from carr_library.audio.voice_reference import ReferenceDescription, VoiceReferenceService
from carr_library.operations import ResourceBroker

_FIXTURE_PATH = Path(__file__).with_name('voice_management_fixtures.py')
_SPEC = importlib.util.spec_from_file_location('carr_reference_owner_fixture', _FIXTURE_PATH)
_OWNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _OWNER
_SPEC.loader.exec_module(_OWNER)

# A purpose-written data protocol fixture, not a copy of the private owner API.
_CREATE = r"""
def create_package(source,document,output):
    import time
    verify_assets(source,document)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('voice-package.json',json.dumps(document,ensure_ascii=False))
        for name in document['files']:
            archive.write(Path(source)/name,name)
    if document['id']=='cancel-fixture':
        (Path(source)/'child-ready').write_text('actual-child-ready',encoding='ascii')
        time.sleep(30)
    return Path(output)
"""


def pcm(path, *, frames=4800, rate=16000, channels=1, width=2, level=None):
    values = array('h', (int(12000 * math.sin(index * .21)) if level is None else level
                         for index in range(frames * channels)))
    if sys.byteorder != 'little':
        values.byteswap()
    data = values.tobytes() if width == 2 else bytes([128]) * frames * channels
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(data)
    return path.read_bytes()


def description(**changes):
    values = dict(profile_id='own-reference', version='1.0.0', name='Synthetic reference recipe',
        language='uk', provenance_source='Purpose-written synthetic PCM protocol fixture.',
        rights_notice='Synthetic test material; no person or commercial voice.',
        reference_text='Синтетичний текст, не запис людського голосу.\r\nExact second line.')
    values.update(changes)
    return ReferenceDescription(**values)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    library, manifest, manager, calls = _OWNER.installed.__wrapped__(tmp_path, monkeypatch)
    helper = manifest.parent / 'voice_packages.py'
    helper.write_text(_OWNER.PACKAGES + _CREATE, encoding='utf-8')
    doc = json.loads(manifest.read_text(encoding='utf-8'))
    doc['files']['voice_packages.py'] = sha256(helper.read_bytes()).hexdigest()
    _OWNER.write_json(manifest, doc)
    current = manager.store.read()
    manager.store.register(current['config'], expected_revision=current['revision'])
    _OWNER.configure(manager)
    from carr_library.runtime_settings import load_settings, _selected_audio_tools
    ffmpeg, ffprobe = _selected_audio_tools(load_settings(library / 'state'))
    ffmpeg = AudioRuntime._executable(ffmpeg, 'ffmpeg')
    ffprobe = AudioRuntime._executable(ffprobe, 'ffprobe')
    runtime = AudioRuntime(tmp_path / 'audio-runtime-work', ffmpeg=ffmpeg, ffprobe=ffprobe)
    source = tmp_path / 'original.wav'
    original = pcm(source)
    service = VoiceReferenceService(library, runtime=runtime, manager=manager)
    return dict(library=library, manifest=manifest, manager=manager, calls=calls,
                runtime=runtime, source=source, original=original, service=service, root=tmp_path)


def reviewed(env, **changes):
    return env['service'].review(env['source'], description(**changes), consent=True)


def test_actual_pcm_and_owner_child_create_exact_local_data_without_installing(environment):
    env = environment
    proposal = reviewed(env)
    assert proposal.pcm['frames'] == 4800 and proposal.pcm['sample_rate'] == 16000
    assert proposal.pcm['channels'] == 1 and proposal.pcm['peak'] > 0
    assert proposal.source_sha256 == sha256(env['original']).hexdigest()
    assert proposal.document['languages'] == ['uk']
    assert 'UNENCRYPTED' in proposal.summary() and 'No training or inference' in proposal.summary()
    old_settings = env['manager'].store.read()
    before_calls = len(env['calls'])
    output = env['root'] / 'reviewed-reference.dordiai-voice'
    result = env['service'].create(proposal, output, consent=True, quality_reviewed=True)
    assert len(env['calls']) > before_calls  # The production owner bridge actually launched its child.
    assert result.path == str(output) and result.sha256 == sha256(output.read_bytes()).hexdigest()
    assert result.size_bytes == output.stat().st_size and result.encrypted is False
    assert result.model_inference == 'NOT_RUN' and result.review_sha256 == proposal.fingerprint
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {'voice-package.json', 'reference.wav', 'reference.txt'}
        assert archive.read('reference.wav') == env['original']
        assert archive.read('reference.txt') == proposal.description.reference_text.encode('utf-8')
        assert json.loads(archive.read('voice-package.json')) == proposal.document
    assert env['source'].read_bytes() == env['original']
    assert env['manager'].store.read() == old_settings
    assert env['manager'].versions(proposal.description.profile_id) == ()
    assert not ResourceBroker.for_app(env['library']).snapshot()['leases']
    assert not list((env['library'] / 'runtime').glob('.voice-reference-*'))
    assert not list(env['root'].glob('.voice-reference-package-*'))


@pytest.mark.parametrize('consent', [False, None, 1], ids=['unchecked', 'null', 'integer'])
def test_no_reference_or_owner_read_before_exact_consent(environment, monkeypatch, consent):
    env = environment
    monkeypatch.setattr(env['service'], '_current',
                        lambda *_: pytest.fail('No owner assets may be read before explicit consent.'))
    with pytest.raises(ValueError, match='permission'):
        env['service'].review(env['root'] / 'missing.wav', description(), consent=consent)


@pytest.mark.parametrize('backend', ['holos', 'openvoice'])
def test_arbitrary_wav_is_not_converted_into_other_backend_profiles(environment, monkeypatch, backend):
    env = environment
    _OWNER.configure(env['manager'], backend)
    monkeypatch.setattr(reference, '_measure_pcm',
                        lambda *_args, **_kwargs: pytest.fail('No recording inspection before backend admission.'))
    with pytest.raises(ValueError, match='OmniVoice'):
        reviewed(env)


@pytest.mark.parametrize('approval', ['consent', 'quality'])
def test_create_requires_both_current_permission_and_recording_review(environment, monkeypatch, approval):
    env = environment
    proposal = reviewed(env)
    monkeypatch.setattr(env['service'], '_current',
                        lambda *_: pytest.fail('No child or profile read before explicit approval.'))
    with pytest.raises(ValueError, match='explicitly'):
        env['service'].create(proposal, env['root'] / 'denied.zip',
            consent=approval != 'consent', quality_reviewed=approval != 'quality')
    assert not (env['root'] / 'denied.zip').exists()


@pytest.mark.parametrize('changed', ['recording', 'description', 'registration'])
def test_exact_source_metadata_or_registration_change_rejects_old_review(environment, monkeypatch, changed):
    env = environment
    proposal = reviewed(env)
    if changed == 'recording':
        before = env['source'].stat()
        value = bytearray(env['source'].read_bytes())
        value[-2] ^= 1
        env['source'].write_bytes(value)
        os.utime(env['source'], ns=(before.st_atime_ns, before.st_mtime_ns))
    elif changed == 'description':
        proposal = replace(proposal, description=replace(proposal.description, reference_text='Different text.'))
    else:
        _OWNER.configure(env['manager'], 'openvoice')
    monkeypatch.setattr(reference, '_query',
                        lambda *_args, **_kwargs: pytest.fail('Changed review cannot reach package creation.'))
    output = env['root'] / 'stale.zip'
    with pytest.raises(ValueError):
        env['service'].create(proposal, output, consent=True, quality_reviewed=True)
    assert not output.exists() and not ResourceBroker.for_app(env['library']).snapshot()['leases']


@pytest.mark.parametrize(('channels', 'width'), [(3, 2), (1, 1)])
def test_unsupported_pcm_shape_is_an_explicit_error_before_native_inspection(
        environment, monkeypatch, channels, width):
    env = environment
    pcm(env['source'], channels=channels, width=width)
    monkeypatch.setattr(AudioRuntime, 'inspect',
                        lambda *_args, **_kwargs: pytest.fail('Unsupported PCM must not start FFprobe.'))
    with pytest.raises(ValueError, match='16-bit'):
        reviewed(env)


def test_truncated_pcm_is_not_a_successful_reference(environment):
    env = environment
    env['source'].write_bytes(env['original'][:-100])
    with pytest.raises(ValueError, match='truncated'):
        reviewed(env)


@pytest.mark.parametrize('level', [0, 32767, -32768], ids=['silence', 'positive-full-scale', 'negative-full-scale'])
def test_measured_quality_warnings_are_numeric_not_speaker_or_model_claims(environment, level):
    env = environment
    pcm(env['source'], level=level)
    proposal = reviewed(env)
    assert proposal.pcm['peak'] == abs(level)
    assert proposal.pcm['zero_samples'] == (4800 if level == 0 else 0)
    assert proposal.pcm['full_scale_samples'] == (0 if level == 0 else 4800)
    assert 'Warning:' in proposal.summary()
    assert 'do not verify speech content' in proposal.summary()
    assert env['manager'].versions(proposal.description.profile_id) == ()


@pytest.mark.parametrize(('field', 'value'), [
    ('profile_id', '../escape'), ('version', 'latest'), ('language', ''),
    ('language', 'auto'), ('language', 'EN-us'), ('name', 'bad\x00name'),
    ('reference_text', ''), ('reference_text', 'x' * 16001),
    ('provenance_source', ''), ('rights_notice', ''),
], ids=['profile-path', 'implicit-version', 'missing-language', 'automatic-language',
        'unnormalized-language', 'nul-name', 'empty-transcript', 'transcript-limit',
        'missing-provenance', 'missing-rights'])
def test_profile_and_transcript_fields_are_explicit_and_bounded(field, value):
    with pytest.raises(ValueError):
        description(**{field: value})


def test_occupied_destination_fails_before_any_external_or_native_reference_read(environment, monkeypatch):
    env = environment
    proposal = reviewed(env)
    target = env['root'] / 'occupied.zip'
    target.write_bytes(b'keep this previous file')
    monkeypatch.setattr(env['service'], '_current',
                        lambda *_: pytest.fail('An occupied destination must fail before runtime reads.'))
    with pytest.raises(FileExistsError):
        env['service'].create(proposal, target, consent=True, quality_reviewed=True)
    assert target.read_bytes() == b'keep this previous file'


def test_competing_destination_is_never_read_or_overwritten_after_owner_package_creation(
        environment, monkeypatch):
    env = environment
    proposal = reviewed(env)
    target = env['root'] / 'competing.zip'
    original_query = reference._query
    def competing(*args, **kwargs):
        result = original_query(*args, **kwargs)
        # Create the independent competitor outside the product-open observer.
        with original_open(target, 'xb') as stream:
            stream.write(b'new independent file')
        return result
    monkeypatch.setattr(reference, '_query', competing)
    original_open = Path.open
    def no_competing_read(path, *args, **kwargs):
        assert path != target, 'Create-only export must not open the independent competing file.'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_competing_read)
    with pytest.raises((ValueError, FileExistsError)):
        env['service'].create(proposal, target, consent=True, quality_reviewed=True)
    monkeypatch.setattr(Path, 'open', original_open)
    assert target.read_bytes() == b'new independent file'
    assert env['source'].read_bytes() == env['original']


def test_cancel_after_prepared_owner_archive_keeps_external_destination_absent(environment, monkeypatch):
    env = environment
    proposal = reviewed(env)
    target, stopped = env['root'] / 'cancelled.zip', threading.Event()
    original = reference._query
    def completed_archive(*args, **kwargs):
        result = original(*args, **kwargs)
        assert Path(kwargs['output']).is_file()
        stopped.set()
        return result
    monkeypatch.setattr(reference, '_query', completed_archive)
    with pytest.raises((reference.AudioCancelled, InterruptedError)):
        env['service'].create(proposal, target, consent=True, quality_reviewed=True,
                              cancelled=stopped.is_set)
    assert not target.exists() and not ResourceBroker.for_app(env['library']).snapshot()['leases']
    assert not list((env['library'] / 'runtime').glob('.voice-reference-*'))

