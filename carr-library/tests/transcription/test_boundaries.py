"""Profile, bounded input and subtitle checks; no model inference is performed."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.transcription import TranscriptionOptions, TranscriptionBudget, TranscriptionRuntime
from carr_library.transcription.files import canonical
from carr_library.transcription.protocol import validate_subtitles


@pytest.mark.parametrize('arguments', [
    {'language': '../../uk'}, {'language': 'uk-UA'}, {'task': 'translate-to-any-language'},
    {'formats': ('txt', 'txt')}, {'formats': []}, {'formats': ()}, {'device': 'auto'},
    {'compute_type': 'auto'}, {'beam_size': True}, {'vad_filter': 1}, {'condition_on_previous_text': 'yes'},
])
def test_options_require_explicit_supported_typed_choices(arguments):
    with pytest.raises(ValueError):
        TranscriptionOptions(**arguments)


@pytest.mark.parametrize('arguments', [
    {'max_sources': True}, {'max_files': 0}, {'max_seconds': float('nan')},
    {'max_seconds': float('inf')}, {'stop_grace_seconds': 11}, {'max_output_bytes': -1},
    {'max_input_bytes': 20, 'max_job_bytes': 10},
])
def test_budgets_are_bounded_exact_numbers(arguments):
    with pytest.raises(ValueError):
        TranscriptionBudget(**arguments)


@pytest.mark.parametrize('mutation', ['consent', 'license', 'version', 'missing_tokenizer', 'unknown_setting', 'moving_revision'])
def test_private_profile_must_bind_actual_complete_installation(setup, mutation):
    service, workspace, profile, source = setup()
    value = json.loads(profile.read_bytes())
    if mutation == 'consent':
        value['trusted_weights'] = False
    elif mutation == 'license':
        value['license_accepted'] = False
    elif mutation == 'version':
        value['settings']['runtime']['packages']['faster-whisper'] = '99.0.0'
    elif mutation == 'missing_tokenizer':
        name = 'models/controlled-fixture/tokenizer.json'
        (profile.parent / name).unlink()
        value['files'].pop(name)
    elif mutation == 'unknown_setting':
        value['settings']['arbitrary_shell'] = 'forbidden'
    elif mutation == 'moving_revision':
        value['revision'] = 'main'
    profile.write_bytes(canonical(value))
    with pytest.raises((ValueError, OSError)):
        service.prepare(workspace, profile, [source])
    assert not service.jobs_root.exists() or not list(service.jobs_root.iterdir())


def test_runtime_snapshot_does_not_retain_mutable_manifest_dictionaries(setup):
    _, _, profile, _ = setup()
    runtime = TranscriptionRuntime.load(profile)
    returned = runtime.worker_identity()
    returned['packages']['faster-whisper'] = 'changed outside the profile'
    assert runtime.worker_identity()['packages']['faster-whisper'] == '0.0.0'
    assert isinstance(runtime.runtime_json, bytes) and isinstance(runtime.model_json, bytes)


@pytest.mark.parametrize('options', [TranscriptionOptions(language='zz'),
                                     TranscriptionOptions(device='cuda', compute_type='float16')])
def test_unadvertised_language_or_device_is_not_a_fallback(setup, options):
    service, workspace, profile, source = setup()
    with pytest.raises(ValueError, match='advertise|reviewed profile'):
        service.prepare(workspace, profile, [source], options=options)


@pytest.mark.parametrize('kind', ['duplicate', 'extension', 'oversize', 'linked'])
def test_invalid_selected_inputs_leave_no_published_job(setup, kind):
    service, workspace, profile, source = setup()
    sources, budget = [source], TranscriptionBudget()
    if kind == 'duplicate':
        sources.append(source)
    elif kind == 'extension':
        renamed = source.with_suffix('.exe')
        source.rename(renamed)
        sources = [renamed]
    elif kind == 'oversize':
        budget = TranscriptionBudget(max_input_bytes=1, max_job_bytes=1000)
    else:
        os.link(source, source.with_name('extra-hardlink.wav'))
    with pytest.raises(ValueError):
        service.prepare(workspace, profile, sources, budget=budget)
    assert not list(service.jobs_root.iterdir())


def test_actual_source_write_during_snapshot_cannot_be_relabelled_as_original(setup):
    service, workspace, profile, source = setup()
    original = source.read_bytes()
    copying = changed = False
    def progress(event):
        nonlocal copying
        copying = event.phase == 'Copying an explicit source snapshot'
    def cancelled():
        nonlocal changed
        if copying and not changed:
            source.write_bytes(b'b' * len(original))
            changed = True
        return False
    with pytest.raises(ValueError, match='changed'):
        service.prepare(workspace, profile, [source], cancelled=cancelled, on_progress=progress)
    assert changed and not list(service.jobs_root.iterdir())


@pytest.mark.parametrize('timestamp', ['00:00:60,000', '00:60:00,000', '-1:00:00,000', '00:00:01,1000'])
def test_subtitle_timestamp_ranges_are_independent_of_worker_success(timestamp):
    segments = [{'start': 0., 'end': 60., 'text': 'Controlled text'}]
    raw = ('1\n00:00:00,000 --> ' + timestamp + '\nControlled text\n').encode()
    with pytest.raises(ValueError, match='timestamp'):
        validate_subtitles(raw, segments, 'srt')


def test_rounded_timestamp_carry_and_blank_segments_preserve_actual_identifiers():
    segments = [{'start': 0., 'end': 0., 'text': ''},
                {'start': 59.9996, 'end': 60.5001, 'text': 'Межа хвилини'}]
    raw = '2\n00:01:00,000 --> 00:01:00,500\nМежа хвилини\n'.encode()
    validate_subtitles(raw, segments, 'srt')
    validate_subtitles(b'WEBVTT\n\n', [], 'vtt')


def test_output_appearing_after_request_cannot_be_taken_from_status_path(setup):
    service, workspace, profile, source = setup()
    job = service.prepare(workspace, profile, [source])
    directory = service.jobs_root / job.id
    outside = source.with_name('unrelated.txt')
    outside.write_text('Keep my original', encoding='utf-8')
    (directory / 'job_status.json').write_bytes(canonical({'state': 'завершено', 'done': 1,
        'total': 1, 'failed': 0, 'results_dir': str(outside)}))
    result = service.run(job.id, approved_request_sha256=job.request_sha256)
    assert outside.read_text('utf-8') == 'Keep my original'
    assert all(name.startswith('results/inputs/') for name in result.outputs)
