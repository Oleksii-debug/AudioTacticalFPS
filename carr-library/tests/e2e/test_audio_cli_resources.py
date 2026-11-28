"""Headless resource/profile parity through the existing production service."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import signal
from types import SimpleNamespace

import pytest

from carr_library.audio_cli import add_audio_arguments, run_audio
from carr_library.audio import engines, ffmpeg as audio, production
from carr_library.audio.model_manifest import ModelManifestError
from carr_library.operations import ResourceBroker, ResourceRequest
from carr_library.runtime_settings import EngineProfileStore, profile_reference


def binary():
    selected = os.environ.get('CARR_FFMPEG') or os.environ.get('CARR_AUDIO_FFMPEG') or shutil.which('ffmpeg')
    if not selected:
        pytest.fail('Actual FFmpeg is required for headless resource qualification.')
    return Path(selected).resolve()


def options(tmp_path, *, library=None, script=None, output=None, executable=None, operation='render'):
    source = tmp_path / 'selected-plan.json'
    plan = production.parse_script(script or
        '[output sample_rate=8000 stems=false]\n# Measured\n[pause 0.2]\n',
        title='Headless admission fixture', voice='unused:local')
    source.write_text(json.dumps(plan.to_dict()), encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    add_audio_arguments(parser)
    args = ['--data-dir', str(library or tmp_path / 'library')]
    args += (['--audio-voices'] if operation == 'voices' else
             ['--audio-plan', str(source), '--audio-preview'] if operation == 'preview' else
             ['--audio-plan', str(source), '--audio-output', str(output or tmp_path / 'output')])
    args += ['--ffmpeg', str(executable or binary())]
    return parser.parse_args(args), source


def silent_registry(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('This pause/profile boundary fixture must not synthesize speech.')
    registry = SimpleNamespace(synthesize=unexpected, voices=lambda: (), capabilities=lambda: ())
    monkeypatch.setattr(engines, 'default_registry', lambda **kwargs: registry)


class CancelWaiting(io.StringIO):
    def write(self, value):
        result = super().write(value)
        if 'Waiting for shared local resources.' in value:
            # Invoke the actual handler installed by run_audio; no platform
            # signal delivery or native cancellation is claimed by this fixture.
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return result


@pytest.mark.parametrize('changed_output', [False, True])
def test_actual_cli_replays_only_verified_output_while_selected_library_is_busy(
        tmp_path, monkeypatch, changed_output):
    library = tmp_path / 'selected-library'
    library.mkdir()
    broker = ResourceBroker.for_app(library)
    args, source = options(tmp_path, library=library)
    original = source.read_bytes()
    silent_registry(monkeypatch)
    actual, native_work = audio.run_ffmpeg, []
    def guarded(executable, arguments, **settings):
        # An installed version fingerprint is read while checking a completed
        # run. Actual DSP/measurement must hold the shared application lease.
        if '-version' not in arguments:
            assert len(broker.snapshot()['leases']) == 1
            native_work.append(tuple(arguments))
        return actual(executable, arguments, **settings)
    monkeypatch.setattr(audio, 'run_ffmpeg', guarded)
    first_output = io.StringIO()
    with redirect_stdout(first_output), redirect_stderr(io.StringIO()):
        assert run_audio(args) == 0
    first = json.loads(first_output.getvalue())
    target = Path(first['output'])
    before = target.read_bytes()
    assert first['status'] == 'complete' and first['frames'] == 1600
    assert native_work and broker.snapshot()['leases'] == []
    completed_work = len(native_work)
    policy_before = broker.policy_path.read_bytes()
    with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots,
                                        memory_bytes=broker.limits.memory_bytes), operation_id='other-heavy-work'):
        if changed_output:
            target.write_bytes(before[:-1] + bytes((before[-1] ^ 1,)))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(CancelWaiting()):
            status = run_audio(args)
        value = json.loads(output.getvalue())
        if changed_output:
            assert status == 130 and value['status'] == 'cancelled'
            assert target.read_bytes() != before
        else:
            assert status == 0 and value['output'] == first['output']
            assert target.read_bytes() == before
        assert len(native_work) == completed_work
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['other-heavy-work']
    assert source.read_bytes() == original
    assert broker.policy_path.read_bytes() == policy_before and broker.snapshot()['leases'] == []


def test_fresh_cli_waits_on_selected_library_and_cancel_preserves_the_other_lease(tmp_path, monkeypatch):
    library = tmp_path / 'selected-library'
    library.mkdir()
    broker = ResourceBroker.for_app(library)
    args, source = options(tmp_path, library=library)
    before = source.read_bytes()
    silent_registry(monkeypatch)
    monkeypatch.setattr(production, '_render_locked',
                        lambda *args, **kwargs: pytest.fail('A contended cancelled render must not run.'))
    previous = signal.getsignal(signal.SIGINT)
    with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots), operation_id='occupied-by-desktop'):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(CancelWaiting()):
            assert run_audio(args) == 130
        assert json.loads(output.getvalue())['status'] == 'cancelled'
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['occupied-by-desktop']
    assert signal.getsignal(signal.SIGINT) == previous
    assert broker.snapshot()['leases'] == [] and source.read_bytes() == before
    assert {path.name for path in args.audio_output.iterdir()} == {'.render.lock'}
    assert (args.audio_output / '.render.lock').is_file()


def test_failed_cli_renderer_releases_its_admission_after_cleanup_without_releasing_another(tmp_path, monkeypatch):
    library = tmp_path / 'selected-library'
    library.mkdir()
    broker = ResourceBroker.for_app(library)
    args, _ = options(tmp_path, library=library)
    silent_registry(monkeypatch)
    def failed(plan, root, **settings):
        assert len(broker.snapshot()['leases']) == 2
        assert not settings['cancelled']()
        raise RuntimeError('Owned synthetic renderer is fully settled.')
    monkeypatch.setattr(production, '_render_locked', failed)
    # Production deliberately reserves the whole declared CPU/RAM pool. Keep
    # another meaningful lease on a disjoint exclusive device, so this case
    # reaches the failing renderer without weakening admission or its policy.
    other = ResourceRequest(cpu_slots=0, devices=('fixture:passive-owner',))
    with broker.reserve(other, operation_id='passive-owner'):
        with redirect_stderr(io.StringIO()), pytest.raises(RuntimeError, match='fully settled'):
            run_audio(args)
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['passive-owner']
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('operation', ['preview', 'voices'])
def test_cli_preview_and_voice_inventory_do_not_create_resource_policy_or_music_profiles(
        tmp_path, monkeypatch, operation):
    executable = tmp_path / 'selected-ffmpeg'
    executable.write_bytes(b'Explicit fixture file; never executed.')
    args, source = options(tmp_path, operation=operation, executable=executable)
    silent_registry(monkeypatch)
    monkeypatch.setattr(ResourceBroker, 'for_app',
        lambda *args, **kwargs: pytest.fail('Information-only CLI created an application broker.'))
    monkeypatch.setattr(EngineProfileStore, '__init__',
        lambda *args, **kwargs: pytest.fail('Information-only CLI created a music-profile store.'))
    with redirect_stdout(io.StringIO()):
        assert run_audio(args) == 0
    assert source.is_file() and not args.data_dir.exists()


def music_manifest(tmp_path):
    folder = tmp_path / 'explicit-music-pack'
    folder.mkdir()
    assets = {'engine.bin': b'Synthetic installed engine, never executed.',
              'model.bin': b'Synthetic reviewed music model.'}
    for name, raw in assets.items():
        (folder / name).write_bytes(raw)
    value = dict(schema_version=1, engine='fluidsynth', model_id='headless-selected-fixture',
        revision='a' * 40, license='Synthetic boundary fixture', license_accepted=True,
        trusted_weights=True, files={name: sha256(raw).hexdigest() for name, raw in assets.items()},
        settings={})
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    return manifest


@pytest.mark.parametrize('changed_after_admission', [False, True])
def test_cli_music_uses_only_selected_library_profile_and_rechecks_its_manifest_after_wait(
        tmp_path, monkeypatch, changed_after_admission):
    library = tmp_path / 'selected-library'
    library.mkdir()
    selected = music_manifest(tmp_path)
    profile = EngineProfileStore(library).register(selected)
    reference = profile_reference(profile)
    args, _ = options(tmp_path, library=library, script=
        '[output sample_rate=8000 stems=false]\n[music engine=fluidsynth model_manifest=' +
        reference + ']\n[pause 0.2]\n')
    silent_registry(monkeypatch)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: pytest.fail('A saved music ID used PATH discovery.'))
    broker = ResourceBroker.for_app(library)
    observed = []
    def rendered(plan, root, **settings):
        assert len(broker.snapshot()['leases']) == 1
        if changed_after_admission:
            value = json.loads(selected.read_text(encoding='utf-8'))
            value['revision'] = 'b' * 40
            selected.write_text(json.dumps(value), encoding='utf-8')
        admitted = settings['music_manifest_resolver'](reference, 'fluidsynth',
                                                      cancelled=settings['cancelled'])
        assert admitted.path == selected and admitted.manifest_sha256 == profile.manifest_sha256
        observed.append(admitted.manifest_sha256)
        return {'status': 'complete', 'synthetic_boundary_only': True}
    monkeypatch.setattr(production, '_render_locked', rendered)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        if changed_after_admission:
            with pytest.raises(ModelManifestError):
                run_audio(args)
        else:
            assert run_audio(args) == 0
    assert observed == ([] if changed_after_admission else [profile.manifest_sha256])
    assert broker.snapshot()['leases'] == []


def test_cli_project_manifest_path_cannot_replace_registered_music_authority(tmp_path, monkeypatch):
    library = tmp_path / 'selected-library'
    library.mkdir()
    output = tmp_path / 'output'
    model_root = output / 'models'
    model_root.mkdir(parents=True)
    selected = music_manifest(tmp_path)
    (model_root / 'project-choice.json').write_bytes(selected.read_bytes())
    args, _ = options(tmp_path, library=library, output=output, script=
        '[music engine=fluidsynth model_manifest=project-choice.json]\n[pause 0.2]\n')
    silent_registry(monkeypatch)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: pytest.fail('Project metadata used PATH discovery.'))
    monkeypatch.setattr(production, '_render_locked',
        lambda *args, **kwargs: pytest.fail('An unregistered music installation reached a renderer.'))
    with redirect_stderr(io.StringIO()), pytest.raises(ModelManifestError, match='saved local music profile'):
        run_audio(args)
    assert not EngineProfileStore(library).path.exists()
    assert ResourceBroker.for_app(library).snapshot()['leases'] == []
    assert {path.name for path in output.iterdir()} == {'.render.lock', 'models'}
    assert (output / '.render.lock').is_file()
    assert {path.name for path in model_root.iterdir()} == {'project-choice.json'}
    assert (model_root / 'project-choice.json').read_bytes() == selected.read_bytes()


# Completed-audio inspection is an explicit read-only CLI operation. These
# cases append to, and do not replace, the existing renderer/admission tests.
def silence_cli_arguments(manifest, *extra):
    return ['--audio-inspect-silence', str(manifest),
            '--audio-silence-threshold-dbfs', '-50',
            '--audio-silence-minimum-seconds', '0.1', *extra]


def forbid_silence_runtime(monkeypatch):
    from carr_library import audio_cli, native_runtime
    from carr_library.audio import runtime
    def unexpected(*args, **kwargs):
        pytest.fail('Completed-audio inspection reached rendering or runtime selection.')
    monkeypatch.setattr(engines, 'default_registry', unexpected)
    monkeypatch.setattr(runtime, 'resolve_ffmpeg', unexpected)
    monkeypatch.setattr(production, 'render_project', unexpected)
    monkeypatch.setattr(ResourceBroker, 'for_app', unexpected)
    monkeypatch.setattr(EngineProfileStore, '__init__', unexpected)
    monkeypatch.setattr(audio_cli, '_render_library_root', unexpected)
    # The public entrypoint permits an absent optional bundle. Inspection must
    # still work without discovering an inference executable or model.
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: None)
    return unexpected


def silence_retained_tree(root):
    result = {}
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        result[name] = None if path.is_dir() else sha256(path.read_bytes()).hexdigest()
    return result


@pytest.mark.parametrize('arguments', [
    ['--audio-silence-threshold-dbfs', '-50'],
    ['--audio-silence-minimum-seconds', '3'],
    ['--audio-silence-run-key', 'a' * 64],
    ['--audio-silence-max-intervals', '2'],
    ['--audio-inspect-silence', 'complete.json',
     '--audio-silence-threshold-dbfs', '-50'],
    ['--audio-inspect-silence', 'complete.json',
     '--audio-silence-minimum-seconds', '3'],
])
def test_silence_cli_requires_selected_completion_and_both_explicit_criteria(
        monkeypatch, arguments):
    from carr_library.__main__ import main
    from carr_library import native_runtime
    unexpected = forbid_silence_runtime(monkeypatch)
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', unexpected)
    monkeypatch.setattr(production, 'inspect_completed_silence', unexpected)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2


@pytest.mark.parametrize('extra', [
    ['--audio-preview'],
    ['--audio-output', 'unused-render-directory'],
    ['--voice', 'unused:voice'],
    ['--ffmpeg', 'unused-ffmpeg.exe'],
    ['--dordiai-manifest', 'unused-runtime.json'],
    ['--piper-executable', 'unused-piper.exe'],
    ['--piper-manifest', 'unused-voice.json'],
])
def test_silence_cli_refuses_unrelated_rendering_or_runtime_authority(monkeypatch, extra):
    from carr_library.__main__ import main
    from carr_library import native_runtime
    unexpected = forbid_silence_runtime(monkeypatch)
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', unexpected)
    monkeypatch.setattr(production, 'inspect_completed_silence', unexpected)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
        main(silence_cli_arguments(Path('complete.json'), *extra))
    assert raised.value.code == 2


@pytest.mark.parametrize('extra', [
    ['--audio-voices'],
    ['--audio-plan', 'another-plan.json'],
    ['--self-test'],
    ['--backup', 'another-backup.zip'],
])
def test_silence_cli_cannot_share_a_command_with_another_operation(monkeypatch, extra):
    from carr_library.__main__ import main
    from carr_library import native_runtime
    unexpected = forbid_silence_runtime(monkeypatch)
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', unexpected)
    monkeypatch.setattr(production, 'inspect_completed_silence', unexpected)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
        main(silence_cli_arguments(Path('complete.json'), *extra))
    assert raised.value.code == 2


def test_silence_cli_real_completed_pcm_report_preserves_all_retained_bytes(tmp_path, monkeypatch):
    from carr_library.__main__ import main
    args, source = options(tmp_path, script=
        '[output sample_rate=8000 stems=false]\n# Quiet\n[pause 0.2]\n')
    silent_registry(monkeypatch)
    rendered = io.StringIO()
    with redirect_stdout(rendered), redirect_stderr(io.StringIO()):
        assert run_audio(args) == 0
    completed = json.loads(rendered.getvalue())
    manifest = Path(completed['manifest'])
    original_manifest = manifest.read_bytes()
    original_source = source.read_bytes()
    actual = production.inspect_completed_silence
    reports, calls = [], []
    def inspect(selected, **settings):
        calls.append((selected, settings['threshold_dbfs'], settings['minimum_seconds'],
                      settings['expected_run_key'], settings['max_intervals']))
        result = actual(selected, **settings)
        reports.append(result)
        return result
    forbid_silence_runtime(monkeypatch)
    monkeypatch.setattr(production, 'inspect_completed_silence', inspect)
    before = silence_retained_tree(tmp_path)
    previous = signal.getsignal(signal.SIGINT)
    output, progress = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(progress):
        assert main(silence_cli_arguments(
            manifest, '--audio-silence-run-key', completed['run_key'],
            '--audio-silence-max-intervals', '3',
            '--data-dir', str(tmp_path / 'must-not-create-library'))) == 0
    report = json.loads(output.getvalue())
    assert report == reports[0]
    assert calls == [(manifest, -50.0, 0.1, completed['run_key'], 3)]
    assert report['schema'] == 'carr-completed-silence-inspection/1'
    assert report['status'] == 'complete' and report['run_key'] == completed['run_key']
    assert report['sample_rate'] == 8000 and report['channels'] == 2 and report['frames'] == 1600
    assert report['interval_count'] == report['retained_interval_count'] == 1
    assert report['omitted_interval_count'] == 0 and report['qualifying_frames'] == 1600
    assert len(report['intervals']) == 1
    assert report['intervals'][0]['start_frame'] == 0
    assert report['intervals'][0]['end_frame'] == 1600
    files = {row['name']: row for row in completed['files']}
    assert report['completion_sha256'] == sha256(original_manifest).hexdigest()
    assert report['master_sha256'] == files['master.wav']['sha256']
    assert report['timeline_sha256'] == files['timeline.json']['sha256']
    assert manifest.read_bytes() == original_manifest and source.read_bytes() == original_source
    assert silence_retained_tree(tmp_path) == before
    assert not (tmp_path / 'must-not-create-library').exists()
    assert signal.getsignal(signal.SIGINT) == previous


@pytest.mark.parametrize('phase', ['during-inspection', 'after-service-return'])
def test_silence_cli_cancel_restores_handler_without_a_partial_complete_report(
        tmp_path, monkeypatch, phase):
    from carr_library.__main__ import main
    from carr_library.audio.jobs import RenderCancelled
    selected = tmp_path / 'complete.json'
    selected.write_bytes(b'Explicit retained boundary fixture; never decoded by the stub.')
    before = silence_retained_tree(tmp_path)
    observed = []
    def inspect(path, **settings):
        assert path == selected and settings['expected_run_key'] is None
        assert settings['max_intervals'] == 1000 and not settings['cancelled']()
        settings['on_status']('Inspecting the selected completed audio.')
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert settings['cancelled']()
        observed.append(phase)
        if phase == 'during-inspection':
            raise RenderCancelled('Completed-audio inspection was cancelled.')
        return {'status': 'complete', 'must_not_be_emitted': True}
    forbid_silence_runtime(monkeypatch)
    monkeypatch.setattr(production, 'inspect_completed_silence', inspect)
    previous = signal.getsignal(signal.SIGINT)
    output, progress = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(progress):
        assert main(silence_cli_arguments(selected)) == 130
    result = json.loads(output.getvalue())
    assert result['status'] == 'cancelled' and set(result) == {'status', 'message'}
    assert 'Inspecting the selected completed audio.' in progress.getvalue()
    assert observed == [phase] and signal.getsignal(signal.SIGINT) == previous
    assert silence_retained_tree(tmp_path) == before


def test_silence_cli_invalid_completion_is_reported_without_repair_or_engine_start(tmp_path, monkeypatch):
    from carr_library.__main__ import main
    selected = tmp_path / 'complete.json'
    selected.write_bytes(b'{}\n')
    before = silence_retained_tree(tmp_path)
    forbid_silence_runtime(monkeypatch)
    previous = signal.getsignal(signal.SIGINT)
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        assert main(silence_cli_arguments(selected)) == 1
    assert output.getvalue() == '' and 'Research Library:' in errors.getvalue()
    assert signal.getsignal(signal.SIGINT) == previous
    assert silence_retained_tree(tmp_path) == before
