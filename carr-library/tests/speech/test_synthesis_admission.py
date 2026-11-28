"""Strict portable reader admission with actual broker files, PCM and owned children."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import (ResourceCancelled, ResourceLimits,
    ResourceRequest, ResourceTimeout, ResourceUnavailable)
from carr_library.speech import LocalHelperProvider, SpeechSettings, Voice, VoiceRegistry
from carr_library.speech.runtime import FliteProvider
from carr_library.speech import synthesis_admission as admission


class Tone:
    name = 'Purpose-written PCM fixture; no model inference'
    def __init__(self, asset, *, engine='synthetic'):
        self.asset, self.executable, self.calls, self.inspect = asset, asset, [], lambda: None
        self.voice = Voice('fixture:voice', 'Fixture voice', engine, 'en')
    def voices(self):
        return (self.voice,)
    def fingerprint(self, voice_id):
        assert voice_id == self.voice.id
        return sha256(self.asset.read_bytes()).hexdigest()
    def render(self, text, voice, output, settings, cancelled):
        assert not cancelled()
        self.inspect()
        self.calls.append((text, voice, settings, threading.get_ident()))
        with wave.open(str(output), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
            stream.writeframes((1700).to_bytes(2, 'little', signed=True) * 400)


@pytest.fixture
def fixture(tmp_path):
    root, output = tmp_path / 'library', tmp_path / 'output'
    root.mkdir(); output.mkdir()
    asset = tmp_path / 'reviewed-fixture.txt'
    asset.write_bytes(b'Purpose-written fixed PCM adapter, version one.')
    provider = Tone(asset)
    registry = VoiceRegistry([provider])
    limits = ResourceLimits(cpu_slots=2, memory_bytes=1024 * 1024,
        disk_bytes=64 * 1024 * 1024, min_free_disk_bytes=0)
    broker = ResourceBroker.for_app(root, limits=limits)
    value = SimpleNamespace(root=root, output=output, asset=asset, provider=provider,
        registry=registry, broker=broker, limits=limits, cancel=threading.Event(), verified=[])
    yield value
    registry.close()


def request(fixture, *, dispatch=lambda callback: callback(), verify=None, registry=None):
    registry = registry or fixture.registry
    return admission.ReaderSynthesisRequest(registry, registry.voices()[0],
        SpeechSettings(max_output_bytes=4096), root=fixture.root,
        cancelled=fixture.cancel.is_set, dispatch=dispatch,
        verify=verify or (lambda **kwargs: fixture.verified.append(threading.get_ident())))


def test_heavy_speech_uses_actual_shared_cpu_ram_and_selected_output_volume(fixture):
    observations = []
    fixture.provider.inspect = lambda: observations.append(fixture.broker.snapshot())
    item = request(fixture)
    result = item.run('Exact source stays unchanged.', fixture.output / 'voice.wav')
    assert not item.basic and item.finished
    assert result.path.read_bytes()[:4] == b'RIFF' and result.frames == 400
    assert len(observations) == 1 and len(observations[0]['leases']) == 1
    record = observations[0]['leases'][0]
    assert record['operation_id'].startswith('reader.synthesis.')
    assert record['request'] == ResourceRequest(cpu_slots=fixture.limits.cpu_slots,
        memory_bytes=fixture.limits.memory_bytes,
        disk_bytes=8192 + admission.PROTOCOL_SCRATCH_BYTES,
        disk_path=fixture.output.resolve()).to_dict()
    assert record['disk_volumes'] == [str(fixture.output.stat().st_dev)]
    assert record['request']['gpu_device'] is None and record['request']['gpu_memory_bytes'] == 0
    assert fixture.verified == [threading.get_ident(), threading.get_ident()]
    assert not fixture.broker.snapshot()['leases'] and not item.cleanup_owners


def test_resource_contention_refuses_before_engine_or_current_reader_callback(fixture):
    with fixture.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='fixture.other'):
        item = request(fixture)
        with pytest.raises(ResourceTimeout):
            item.run('Do not start while occupied.', fixture.output / 'never.wav', admission_timeout=0)
        assert not fixture.provider.calls and not fixture.verified
        assert len(fixture.broker.snapshot()['leases']) == 1
    assert not fixture.broker.snapshot()['leases'] and not list(fixture.output.iterdir())


def test_zero_memory_policy_refuses_heavy_work_without_inventing_a_capacity(fixture):
    fixture.broker.configure(ResourceLimits(cpu_slots=2, memory_bytes=0,
        disk_bytes=fixture.limits.disk_bytes, min_free_disk_bytes=0))
    with pytest.raises(ResourceUnavailable, match='positive local speech memory'):
        request(fixture).run('No imaginary memory allowance.', fixture.output / 'never.wav')
    assert not fixture.provider.calls and not fixture.broker.snapshot()['leases']


def test_initial_cancel_does_not_enter_resource_policy_or_provider(fixture, monkeypatch):
    fixture.cancel.set()
    monkeypatch.setattr(admission.ResourceBroker, 'for_app',
                        lambda *args, **kwargs: pytest.fail('Cancelled work opened the broker.'))
    with pytest.raises(InterruptedError):
        request(fixture).run('Cancel before admission.', fixture.output / 'never.wav')
    assert not fixture.provider.calls and not fixture.verified


def observe_wait(monkeypatch):
    waiting = threading.Event()
    original = ResourceBroker._reason
    def reason(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if value is not None:
            waiting.set()
        return value
    monkeypatch.setattr(ResourceBroker, '_reason', reason)
    return waiting


def test_cancel_during_real_broker_wait_never_dispatches_or_runs_engine(fixture, monkeypatch):
    waiting, callbacks = observe_wait(monkeypatch), Queue()
    item = request(fixture, dispatch=callbacks.put)
    with fixture.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='fixture.other'):
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(item.run, 'Waiting request.', fixture.output / 'never.wav')
            try:
                assert waiting.wait(5), 'The request never reached actual resource contention.'
            finally:
                fixture.cancel.set()
            with pytest.raises((ResourceCancelled, InterruptedError)):
                future.result(timeout=5)
        assert len(fixture.broker.snapshot()['leases']) == 1
    assert callbacks.empty() and not fixture.provider.calls and not fixture.verified
    assert not fixture.broker.snapshot()['leases']


@pytest.mark.parametrize('change', ['runtime', 'provider', 'voice', 'output_parent'])
def test_admitted_request_rechecks_exact_identity_after_real_resource_wait(fixture, monkeypatch, change):
    waiting, callbacks = observe_wait(monkeypatch), Queue()
    item = request(fixture, dispatch=callbacks.put)
    with ThreadPoolExecutor(max_workers=1) as worker:
        with fixture.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='fixture.other'):
            future = worker.submit(item.run, 'Captured request.', fixture.output / 'never.wav')
            assert waiting.wait(5), 'The request never reached actual resource contention.'
            if change == 'runtime':
                fixture.asset.write_bytes(b'A different adapter revision.')
            elif change == 'provider':
                fixture.registry._owners[fixture.provider.voice.id] = Tone(fixture.asset)
            elif change == 'voice':
                fixture.registry._voices[fixture.provider.voice.id] = Voice(
                    fixture.provider.voice.id, 'Replaced voice metadata', 'synthetic', 'en')
            else:
                fixture.output.rename(fixture.output.with_name('original-output'))
                fixture.output.mkdir()
        with pytest.raises(ValueError, match='changed'):
            future.result(timeout=5)
    assert not fixture.provider.calls and not fixture.verified and callbacks.empty()
    assert not fixture.broker.snapshot()['leases']
    assert not (fixture.output / 'never.wav').exists()


def test_current_source_refusal_releases_the_admitted_but_unused_reservation(fixture):
    def denied(**kwargs):
        assert len(fixture.broker.snapshot()['leases']) == 1
        raise ValueError('The current source changed.')
    with pytest.raises(ValueError, match='current source'):
        request(fixture, verify=denied).run('Captured text.', fixture.output / 'never.wav')
    assert not fixture.provider.calls and not fixture.broker.snapshot()['leases']


def test_cancel_after_admission_is_checked_before_provider_invocation(fixture):
    def stopped(**kwargs):
        assert len(fixture.broker.snapshot()['leases']) == 1
        fixture.cancel.set()
    with pytest.raises(InterruptedError):
        request(fixture, verify=stopped).run('Cancel after admission.', fixture.output / 'never.wav')
    assert not fixture.provider.calls and not fixture.broker.snapshot()['leases']


def test_late_queued_owner_check_stays_revoked_when_global_cancel_is_cleared(fixture):
    callbacks = Queue()
    item = request(fixture, dispatch=callbacks.put)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(item.run, 'Queued owner check.', fixture.output / 'never.wav')
        callback = callbacks.get(timeout=5)
        fixture.cancel.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    fixture.cancel.clear()  # A later reader request must not resurrect this one.
    callback()
    assert item.finished and not fixture.verified and not fixture.provider.calls
    assert not fixture.broker.snapshot()['leases']


def test_unserviced_owner_handoff_is_bounded_and_late_callback_is_inert(fixture, monkeypatch):
    callbacks = Queue()
    monkeypatch.setattr(admission, 'OWNER_CHECK_SECONDS', .1)
    item = request(fixture, dispatch=callbacks.put)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(item.run, 'Window is no longer pumping.', fixture.output / 'never.wav')
        callback = callbacks.get(timeout=5)
        with pytest.raises(TimeoutError, match='confirm the current request'):
            future.result(timeout=5)
    callback()
    assert not fixture.verified and not fixture.provider.calls
    assert not fixture.broker.snapshot()['leases']


def test_worker_cannot_execute_owner_verification_as_a_direct_callback(fixture):
    item = request(fixture)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(item.run, 'Wrong dispatch.', fixture.output / 'never.wav')
        with pytest.raises(RuntimeError, match='window owner thread'):
            future.result(timeout=5)
    assert not fixture.verified and not fixture.provider.calls
    assert not fixture.broker.snapshot()['leases']


def test_real_helper_cannot_claim_basic_admission_with_a_sapi_display_label(fixture):
    script, invoked = fixture.output / 'helper.py', fixture.output / 'render-was-invoked'
    script.write_text(
        'import json, sys, wave\n'
        'from pathlib import Path\n'
        'request = json.load(sys.stdin)\n'
        'reply = {"schema": "carr.local-voice.v1"}\n'
        'if request["operation"] == "voices":\n'
        '    reply["voices"] = [{"id": "sapi5:pretend", "name": "Synthetic helper", '
        '"language": "en", "capabilities": ["rate", "volume"], "license": "Purpose-written fixture"}]\n'
        'else:\n'
        '    Path(' + repr(str(invoked)) + ').write_text("actual render child")\n'
        '    with wave.open(request["output"], "wb") as stream:\n'
        '        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)\n'
        '        stream.writeframes(b"\\x10\\x01" * 400)\n'
        '    reply["status"] = "rendered"\n'
        'print(json.dumps(reply))\n', encoding='utf-8')
    executable = Path(sys.executable).resolve(strict=True)
    provider = LocalHelperProvider(executable, sha256=sha256(executable.read_bytes()).hexdigest(),
        engine_name='sapi5', enabled=True, script=script,
        script_sha256=sha256(script.read_bytes()).hexdigest())
    registry = VoiceRegistry([provider])
    try:
        assert registry.voices()[0].engine == 'sapi5'
        denied = request(fixture, registry=registry)
        assert not denied.basic
        with fixture.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='fixture.other'):
            with pytest.raises(ResourceTimeout):
                denied.run('No bypass.', fixture.output / 'never.wav', admission_timeout=0)
        assert not invoked.exists()
        result = request(fixture, registry=registry).run('Now admitted.', fixture.output / 'helper.wav')
        assert invoked.read_text() == 'actual render child' and result.frames == 400
        assert not fixture.broker.snapshot()['leases']
    finally:
        registry.close()


def test_a_custom_native_provider_subclass_has_no_basic_exemption(fixture):
    class CustomFlite(FliteProvider):
        pass
    provider = object.__new__(CustomFlite)
    provider.voices = fixture.provider.voices
    registry = VoiceRegistry([provider])
    try:
        assert not request(fixture, registry=registry).basic
    finally:
        registry.close()


@pytest.mark.parametrize('basic', [False, True])
@pytest.mark.parametrize('masked', [False, True])
def test_real_pending_native_families_retain_lease_and_outer_files_until_every_owner_is_quiet(
        fixture, tmp_path, monkeypatch, basic, masked):
    """The stop acknowledgement is deliberately held; children and files are real."""
    actual_stop, children, allowed = process._stop_owned, [], {}
    def stop(child):
        return actual_stop(child) if allowed.get(child.pid, False) else False
    monkeypatch.setattr(process, '_stop_owned', stop)
    outer = tmp_path / 'outer-reader-audio'
    outer.mkdir()
    releases, owners, released = [], (), threading.Event()
    fixture.provider.inspect = lambda: None

    def render(text, voice, output, settings, cancelled):
        try:
            with ExitStack() as native:
                ready = []
                for index in range(2):
                    marker = Path(output).parent / ('child-' + str(index))
                    child = native.enter_context(process.owned_process(
                        [sys.executable, '-I', '-S', '-c',
                         'from pathlib import Path; import sys,time; '
                         'Path(sys.argv[1]).write_bytes(b"owned"); time.sleep(60)', str(marker)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    children.append(child); allowed[child.pid] = False; ready.append(marker)
                deadline = time.monotonic() + 5
                while not all(path.exists() for path in ready):
                    if time.monotonic() >= deadline:
                        raise AssertionError('Real owned children did not reach their stage.')
                    time.sleep(.01)
                assert all(child.poll() is None for child in children)
                raise RuntimeError('Stop the actual fixture families before publishing.')
        except AudioCleanupPending as error:
            if masked:
                raise RuntimeError('A later diagnostic preserved causal cleanup.') from error
            raise

    provider = fixture.provider
    if basic:
        # Ownership boundary fixture only, not a native Flite-quality claim.
        provider = object.__new__(FliteProvider)
        provider.voices = fixture.provider.voices
    provider.render = render
    registry = VoiceRegistry([provider])
    item = request(fixture, registry=registry)
    try:
        assert item.basic is basic
        with pytest.raises((AudioCleanupPending, RuntimeError)) as failure:
            item.run('Keep both native owners.', outer / 'never.wav')
        owners = item.cleanup_owners
        assert len(owners) == 2 and set(owners) == set(_pending_owners(failure.value))
        assert all(not owner.quiescent for owner in owners)
        assert len(fixture.broker.snapshot()['leases']) == (0 if basic else 1)
        assert not (outer / 'never.wav').exists() and any(outer.glob('.speech-*'))

        def release():
            assert all(owner.quiescent for owner in owners)
            assert not fixture.broker.snapshot()['leases']
            import shutil
            shutil.rmtree(outer)
            releases.append(threading.get_ident())
            released.set()

        admission.defer_synthesis_files([item], release)
        # Each actual CleanupOwner corresponds to one actual child's final stop.
        allowed[children[0].pid] = True
        deadline = time.monotonic() + 5
        while sum(owner.quiescent for owner in owners) != 1 and time.monotonic() < deadline:
            for owner in owners:
                owner.close()
            time.sleep(.01)
        assert sum(owner.quiescent for owner in owners) == 1
        assert outer.exists() and not releases
        assert len(fixture.broker.snapshot()['leases']) == (0 if basic else 1)
        allowed[children[1].pid] = True
        for owner in owners:
            owner.close()
        assert released.wait(5), 'The real owners did not release the retained outer stage.'
        assert all(owner.quiescent for owner in owners)
        assert not outer.exists() and len(releases) == 1
        for owner in owners:
            owner.close()
        assert len(releases) == 1 and not fixture.broker.snapshot()['leases']
    finally:
        for child in children:
            allowed[child.pid] = True
        for owner in owners:
            owner.close()
        for child in children:
            actual_stop(child)
        registry.close()


@pytest.mark.parametrize('identity_pass', [1, 2, 3])
def test_known_piper_asset_verification_is_cancellable_only_inside_its_reservation(
        fixture, monkeypatch, identity_pass):
    from carr_library.speech import runtime
    model, config = fixture.output / 'synthetic.onnx', fixture.output / 'synthetic.onnx.json'
    model.write_bytes(b'P' * (3 * 1024 * 1024))
    config.write_bytes(b'{"synthetic": true}')
    provider = runtime.PiperProvider(Path(sys.executable).resolve(), [{
        'path': str(model), 'sha256': sha256(model.read_bytes()).hexdigest(),
        'config_sha256': sha256(config.read_bytes()).hexdigest(), 'name': 'Synthetic hash fixture',
        'language': 'en', 'license': 'Purpose-written bytes; never neural weights'}])
    registry = VoiceRegistry([provider])
    original_digest, selected_calls, checkpoints, native_calls = runtime.digest, [], [], []
    def digest(path, *, cancelled=lambda: False):
        assert len(fixture.broker.snapshot()['leases']) == 1, 'Asset hashing escaped admission.'
        if Path(path) == model:
            selected_calls.append(Path(path))
            if len(selected_calls) == identity_pass:
                def checkpoint():
                    checkpoints.append(True)
                    if len(checkpoints) == 3:
                        fixture.cancel.set()
                    return cancelled()
                return original_digest(path, cancelled=checkpoint)
        return original_digest(path, cancelled=cancelled)
    monkeypatch.setattr(runtime, 'digest', digest)
    monkeypatch.setattr(runtime, 'run_process',
                        lambda *args, **kwargs: native_calls.append((args, kwargs)))
    item = request(fixture, registry=registry)
    try:
        with pytest.raises(InterruptedError, match='cancelled'):
            item.run('Never execute these synthetic model bytes.', fixture.output / 'never.wav')
        assert len(selected_calls) == identity_pass and len(checkpoints) == 3
        assert not native_calls and not (fixture.output / 'never.wav').exists()
        assert not fixture.broker.snapshot()['leases'] and item.finished
        # Pass three is the provider's mandatory pre-launch verification;
        # passes one/two bracket the reader's current-source confirmation.
        assert len(fixture.verified) == (0 if identity_pass == 1 else 1 if identity_pass == 2 else 2)
    finally:
        registry.close()


def test_runtime_mutation_during_owner_confirmation_refuses_before_render(fixture):
    confirmations = []
    def current(*, rendering):
        confirmations.append(rendering)
        fixture.asset.write_bytes(b'Changed after admitted identity validation.')
    with pytest.raises(ValueError, match='runtime changed'):
        request(fixture, verify=current).run('Original request.', fixture.output / 'never.wav')
    assert confirmations == [False] and not fixture.provider.calls
    assert not fixture.broker.snapshot()['leases']


def test_cancellation_of_actual_owned_helper_releases_only_after_its_child_context_exits(
        fixture, monkeypatch):
    script, ready = fixture.output / 'waiting-helper.py', fixture.output / 'helper-ready'
    script.write_text(
        'import json,sys,time,os\nfrom pathlib import Path\n'
        'request=json.load(sys.stdin)\n'
        'if request["operation"]=="voices":\n'
        ' print(json.dumps({"schema":"carr.local-voice.v1","voices":['
        '{"id":"one","name":"Waiting synthetic helper","language":"en",'
        '"capabilities":["rate","volume"],"license":"Purpose-written fixture"}]}))\n'
        'else:\n'
        ' marker=Path(' + repr(str(ready)) + ')\n'
        ' staged=marker.with_name(marker.name+".pending")\n'
        ' staged.write_bytes(b"actual owned render child")\n'
        ' os.replace(staged,marker)\n'
        ' time.sleep(60)\n', encoding='utf-8')
    executable = Path(sys.executable).resolve()
    provider = LocalHelperProvider(executable, sha256=sha256(executable.read_bytes()).hexdigest(),
        engine_name='synthetic', enabled=True, script=script,
        script_sha256=sha256(script.read_bytes()).hexdigest())
    registry = VoiceRegistry([provider])
    original, children, exited, callbacks = process.owned_process, [], [], Queue()
    @contextmanager
    def owned(*args, **kwargs):
        child = None
        try:
            with original(*args, **kwargs) as child:
                children.append(child)
                yield child
        except BaseException as error:
            if child is not None and not _pending_owners(error):
                exited.append(child)
            raise
        else:
            exited.append(child)
    monkeypatch.setattr(process, 'owned_process', owned)
    item = request(fixture, registry=registry, dispatch=callbacks.put)
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(item.run, 'A cancelled helper.', fixture.output / 'never.wav')
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and not future.done():
                    if time.monotonic() >= deadline:
                        raise AssertionError('The actual owned helper never reached its render request.')
                    try:
                        callbacks.get(timeout=.01)()
                    except Empty:
                        pass
                assert ready.read_bytes() == b'actual owned render child'
                assert len(children) == 1 and children[0].poll() is None
                assert len(fixture.broker.snapshot()['leases']) == 1 and not exited
            finally:
                fixture.cancel.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=10)
        assert children == exited and children[0].returncode is not None
        assert not fixture.broker.snapshot()['leases']
        assert not (fixture.output / 'never.wav').exists() and not list(fixture.output.glob('.speech-*'))
    finally:
        fixture.cancel.set()
        registry.close()


def test_final_actual_pcm_hash_observes_cancel_before_publication_and_releases_reservation(
        fixture, monkeypatch):
    from carr_library.speech import runtime
    original, interrupted_in_hash = runtime.digest, []
    def digest(path, *, cancelled=lambda: False):
        selected = Path(path).name == 'output.wav'
        if selected:
            assert len(fixture.broker.snapshot()['leases']) == 1
            assert Path(path).read_bytes()[:4] == b'RIFF'
            fixture.cancel.set()
        try:
            return original(path, cancelled=cancelled)
        except InterruptedError:
            if selected:
                interrupted_in_hash.append(True)
            raise
    monkeypatch.setattr(runtime, 'digest', digest)
    with pytest.raises(InterruptedError):
        request(fixture).run('Actual PCM is ready but not published.', fixture.output / 'never.wav')
    assert len(fixture.provider.calls) == 1 and interrupted_in_hash == [True]
    assert not (fixture.output / 'never.wav').exists()
    assert not list(fixture.output.glob('.speech-*')) and not fixture.broker.snapshot()['leases']


def test_piper_fixed_request_delegates_to_the_existing_actual_owned_child_launcher(fixture, monkeypatch):
    from carr_library.speech import runtime
    from carr_library.speech._io import run_process as actual_run
    model, config = fixture.output / 'fixture.onnx', fixture.output / 'fixture.onnx.json'
    model.write_bytes(b'Purpose-written bytes, not neural weights.')
    config.write_bytes(b'{"synthetic":true}')
    provider = runtime.PiperProvider(Path(sys.executable).resolve(), [{
        'path': str(model), 'sha256': sha256(model.read_bytes()).hexdigest(),
        'config_sha256': sha256(config.read_bytes()).hexdigest(), 'name': 'CLI boundary fixture',
        'language': 'en', 'license': 'Synthetic test bytes only'}])
    registry = VoiceRegistry([provider])
    launches, children = [], []
    original_owner = process.owned_process
    @contextmanager
    def owned(*args, **kwargs):
        with original_owner(*args, **kwargs) as child:
            children.append(child)
            assert len(fixture.broker.snapshot()['leases']) == 1
            yield child

    def launch(arguments, **kwargs):
        # Substitute only the synthetic executable request. The production
        # owned launcher, input, timeout, output cap and cleanup are real.
        launches.append((arguments, kwargs))
        assert kwargs['owned_tree'] is True
        target = arguments[arguments.index('--output-file') + 1]
        return actual_run([sys.executable, '-I', '-S', '-c',
            'import sys,wave; data=sys.stdin.buffer.read(); '
            'assert data == b"Exact literal request.\\n"; '
            'out=wave.open(sys.argv[1],"wb"); out.setnchannels(1); '
            'out.setsampwidth(2); out.setframerate(8000); '
            'out.writeframes(b"\\x10\\x01"*400); out.close()', str(target)], **kwargs)

    monkeypatch.setattr(process, 'owned_process', owned)
    monkeypatch.setattr(runtime, 'run_process', launch)
    try:
        result = request(fixture, registry=registry).run('Exact literal request.', fixture.output / 'piper.wav')
        assert result.frames == 400 and result.path.read_bytes()[:4] == b'RIFF'
        assert len(launches) == len(children) == 1 and children[0].returncode == 0
        assert launches[0][1]['input_bytes'] == b'Exact literal request.\n'
        assert launches[0][1]['max_output'] == 4096
        assert not fixture.broker.snapshot()['leases']
    finally:
        registry.close()


@pytest.mark.parametrize('validation', ['utterance.wav', 'output.wav'])
def test_actual_pcm_validation_cancels_during_frames_before_hash_or_publication(
        fixture, monkeypatch, validation):
    from carr_library.speech import runtime
    frames = 3 * 65536
    def render(text, voice, output, settings, cancelled):
        assert not cancelled()
        with wave.open(str(output), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
            stream.writeframes(b'\x10\x01' * frames)
    fixture.provider.render = render
    original_info, original_read = runtime._wave_info, wave.Wave_read.readframes
    reads, interrupted, active = [], [], False
    def read(self, count):
        block = original_read(self, count)
        if active:
            reads.append((count, len(block)))
        return block
    def info(path, *, cancelled=lambda: False):
        nonlocal active
        if Path(path).name != validation:
            return original_info(path, cancelled=cancelled)
        def checked():
            if reads:
                fixture.cancel.set()
            return cancelled()
        active = True
        try:
            return original_info(path, cancelled=checked)
        except InterruptedError:
            interrupted.append(Path(path).name)
            raise
        finally:
            active = False
    monkeypatch.setattr(wave.Wave_read, 'readframes', read)
    monkeypatch.setattr(runtime, '_wave_info', info)
    item = admission.ReaderSynthesisRequest(fixture.registry, fixture.registry.voices()[0],
        SpeechSettings(max_output_bytes=1024 * 1024), root=fixture.root,
        cancelled=fixture.cancel.is_set, dispatch=lambda callback: callback(),
        verify=lambda **kwargs: None)
    with pytest.raises(InterruptedError, match='cancelled'):
        item.run('Validate actual PCM, then cancel.', fixture.output / 'never.wav')
    assert interrupted == [validation] and reads == [(65536, 2 * 65536)]
    assert not (fixture.output / 'never.wav').exists()
    assert not list(fixture.output.glob('.speech-*')) and not fixture.broker.snapshot()['leases']
