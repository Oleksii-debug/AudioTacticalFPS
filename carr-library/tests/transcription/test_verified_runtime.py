"""Real-process version dispatch and inert runtime review; no ASR inference."""
from hashlib import sha256
import importlib.util
import json
import os
import shutil
from pathlib import Path
import threading
import time

import pytest

from carr_library.audio.models import AudioBudget
from carr_library.transcription import TranscriptionService
from carr_library.transcription import service as service_module
from carr_library.transcription.verified_runtime import extend_profile, launch_arguments
from carr_library.transcription.verified_protocol import PROTOCOL

_spec = importlib.util.spec_from_file_location('verified_runtime_fixture', Path(__file__).with_name('fixture_verified_runtime.py'))
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)


@pytest.fixture
def runtime(tmp_path):
    return _builder.make_runtime(tmp_path / 'Synthetic runtime')


@pytest.fixture
def setup(runtime, tmp_path):
    service = TranscriptionService(tmp_path / 'library/state/transcription',
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0, max_log_bytes=65536), stop_grace=.15)
    profile = _builder.register(service, runtime)
    source = tmp_path / 'Synthetic source.wav'
    source.write_bytes(b'Purpose-written file identity; no speech recognition.')
    return service, runtime, profile, source


def starts(runtime):
    path = runtime.worker.with_name('fixture-starts.log')
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


def options(runtime, value):
    runtime.options.write_text(json.dumps(value), encoding='utf-8')


def create(setup, **kwargs):
    service, runtime, profile, source = setup
    return service.create_job(profile['id'], [source], **kwargs)


def test_descriptor_opts_into_complete_inspection_without_execution(setup):
    service, runtime, profile, source = setup
    assert profile['protocol'] == PROTOCOL
    assert len(profile['verified_packages']) == 9
    assert {row['name'] for row in profile['verified_packages']} >= {'synthetic-helper', 'packaging'}
    assert len(profile['verified_implementation']) == 3
    assert profile['verified_implementation'][1]['name'] == 'recovery_support.py'
    assert profile['qualification'] == 'unqualified'
    assert profile['verified_visible_files']['files'] > 20
    assert starts(runtime) == 0


@pytest.mark.parametrize('component', ['helper', 'transitive', 'descriptor', 'unlisted_visible_file'])
def test_changed_identity_requires_new_review_before_any_process(setup, component):
    service, runtime, profile, source = setup
    created = create(setup)
    target = {'helper': runtime.worker.with_name('recovery_support.py'),
              'transitive': runtime.dependencies['synthetic-helper'],
              'descriptor': runtime.worker.with_suffix('.protocol.json'),
              'unlisted_visible_file': runtime.site / 'new_importable_module.py'}[component]
    if component == 'descriptor':
        target.unlink()
    else:
        target.write_text('# Changed reviewed or visible bytes.\n', encoding='utf-8')
    result = service.run(created['id'])
    assert result['status'] == 'failed' and result['done'] == 0
    assert starts(runtime) == 0


def test_absent_vocabulary_cannot_be_admitted(runtime):
    (runtime.model / 'vocabulary.txt').unlink()
    with pytest.raises(ValueError, match='model inventory|incomplete'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    assert starts(runtime) == 0


def test_missing_transitive_dependency_blocks_inspection(runtime):
    info = next(runtime.site.glob('synthetic_helper-*.dist-info'))
    (info / 'METADATA').unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    assert starts(runtime) == 0


def test_duplicate_distribution_metadata_is_not_silently_selected(runtime):
    info = next(runtime.site.glob('synthetic_helper-*.dist-info'))
    shutil.copytree(info, runtime.site / 'duplicate-helper-1.0.0.dist-info')
    with pytest.raises(ValueError, match='ambiguous'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)


def test_environment_markers_are_resolved_before_closure_admission(runtime):
    info = next(runtime.site.glob('faster_whisper-*.dist-info'))
    with (info / 'METADATA').open('a', encoding='utf-8') as stream:
        stream.write('Requires-Dist: unavailable-platform-package; python_version < "3.0"\n')
    profile = TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    assert len(profile['verified_packages']) == 9
    with (info / 'METADATA').open('a', encoding='utf-8') as stream:
        stream.write('Requires-Dist: unavailable-platform-package; python_version >= "3.0"\n')
    with pytest.raises(ValueError, match='missing transitive'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)


def test_declared_version_requirement_cannot_be_ignored(runtime):
    info = next(runtime.site.glob('faster_whisper-*.dist-info'))
    path = info / 'METADATA'
    path.write_text(path.read_text(encoding='utf-8').replace('synthetic-helper>=1.0.0',
                                                          'synthetic-helper>=2.0.0'), encoding='utf-8')
    with pytest.raises(ValueError, match='version requirement'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)


def test_runtime_inspection_includes_explicit_extra_dependencies(runtime):
    parent = next(runtime.site.glob('faster_whisper-*.dist-info')) / 'METADATA'
    parent.write_text(parent.read_text(encoding='utf-8').replace('synthetic-helper>=1.0.0',
                                                              'synthetic-helper[needed]>=1.0.0'), encoding='utf-8')
    helper = next(runtime.site.glob('synthetic_helper-*.dist-info')) / 'METADATA'
    with helper.open('a', encoding='utf-8') as stream:
        stream.write('Provides-Extra: needed\nRequires-Dist: extra-companion; extra == "needed"\n')
    with pytest.raises(ValueError, match='missing transitive'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    _builder.add_distribution(runtime.site, 'extra-companion')
    profile = TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    assert 'extra-companion' in {row['name'] for row in profile['verified_packages']}


def test_worker_launch_skips_pth_and_uses_fresh_bytecode_namespace(runtime, tmp_path):
    marker = tmp_path / 'must-not-run.txt'
    pth = runtime.site / 'unwanted-startup.pth'
    pth.write_text('import pathlib; pathlib.Path(' + repr(str(marker)) + ').write_text("executed")\n',
                   encoding='utf-8')
    service = TranscriptionService(tmp_path / 'isolated/state/transcription',
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0))
    profile = _builder.register(service, runtime)
    assert not marker.exists()
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic retained input.')
    result = service.run(service.create_job(profile['id'], [source])['id'])
    assert result['status'] == 'complete', result['error']
    assert not marker.exists()
    worker = service._job_root(result['id']) / 'worker'
    arguments = launch_arguments(profile, worker, nonce='b' * 32)
    assert arguments[1:6] == ['-I', '-S', '-B', '-X', 'utf8']
    assert 'pycache_prefix=' in arguments[7]
    assert not list(worker.glob('unwritten-bytecode-*'))


def test_sourceless_bytecode_is_rejected_before_launch(runtime):
    (runtime.site / 'unexpected.pyc').write_bytes(b'Not reviewed source.')
    with pytest.raises(ValueError, match='Sourceless'):
        TranscriptionService.inspect_runtime(runtime.python, runtime.worker, runtime.model)


def test_real_process_uses_new_protocol_and_verifies_completion_file_union(setup):
    service, runtime, profile, source = setup
    original = source.read_bytes()
    created = create(setup, settings={'output_srt': True, 'output_vtt': True})
    result = service.run(created['id'])
    assert result['status'] == 'complete', result['error']
    assert result['done'] == result['total'] == 1
    assert result['worker_completion']['profile_fingerprint'] == profile['fingerprint']
    assert starts(runtime) == 1 and source.read_bytes() == original
    transcript = service.read_transcript(created['id'])
    assert transcript['text'] == 'Synthetic verified protocol result.'
    assert transcript['provenance']['worker_protocol'] == PROTOCOL
    assert transcript['provenance']['quality'] == 'unqualified'
    assert transcript['provenance']['citation_authority'] is False


@pytest.mark.parametrize('flag', ['wrong_helper', 'missing_dependency', 'wrong_source',
    'invalid_timing', 'omit_receipt', 'tamper_output', 'omit_completion',
    'extra_completion_file', 'old_completion_run', 'duplicate_completion_input'])
def test_actual_process_cannot_complete_an_invalid_handoff(setup, flag):
    service, runtime, profile, source = setup
    options(runtime, {flag: True})
    result = service.run(create(setup)['id'])
    assert result['status'] in ('failed', 'partial'), result
    assert result['error']
    assert starts(runtime) == 1


def test_partial_run_preserves_accepted_source_and_resumes_only_unfinished(setup, tmp_path):
    service, runtime, profile, source = setup
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Another synthetic retained input.')
    options(runtime, {'fail_after': 1})
    created = service.create_job(profile['id'], [source, second])
    partial = service.run(created['id'])
    assert partial['status'] == 'partial' and partial['done'] == 1, partial
    first = dict(partial['results'][0])
    options(runtime, {})
    complete = service.resume(created['id'])
    assert complete['status'] == 'complete', complete['error']
    assert complete['done'] == 2 and complete['results'][0] == first
    assert starts(runtime) == 2
    assert len(runtime.worker.with_name('fixture-inferences.log').read_text(encoding='utf-8').splitlines()) == 2


def test_cancel_uses_worker_cancel_flag_then_releases_resource_lease(setup):
    service, runtime, profile, source = setup
    options(runtime, {'delay': 10, 'ignore_stop': True})
    created = create(setup)
    observed = []
    def cancelled():
        if not starts(runtime):
            return False
        if not observed:
            observed.append(time.monotonic())
        return time.monotonic() - observed[0] > .2
    result = service.run(created['id'], cancel=cancelled)
    assert result['status'] == 'cancelled', result
    assert (service._job_root(created['id']) / 'worker/cancel.flag').is_file()
    assert observed and time.monotonic() - observed[0] < 5
    from carr_library.operations import ResourceBroker
    assert not ResourceBroker.for_app(service.library_root).snapshot()['leases']
    service.continue_job(created['id'])
    assert not (service._job_root(created['id']) / 'worker/cancel.flag').exists()


def test_restored_historical_cancel_flags_are_scrubbed(setup, tmp_path):
    from carr_library.transcription.service import prepare_restored_state
    service, runtime, profile, source = setup
    created = create(setup)
    worker = service._job_root(created['id']) / 'worker'
    worker.mkdir()
    (worker / 'cancel.flag').write_bytes(b'1')
    prepare_restored_state(service.root.parent)
    assert not (worker / 'cancel.flag').exists()
    assert service.list_runtimes()[0]['trusted'] is False
