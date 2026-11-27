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
