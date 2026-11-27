"""Actual headless domain routing retains one selected runtime and producer.

The runtime-binding seam is observed; Book PCM and ffprobe execution remain
real. Passive plans never activate media, and a pending child retains lifetime.
"""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.audio import local_runtime, cleanup, process
from carr_library.studio import cli
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location(
    'assistant_cli_book_fixture', Path(__file__).parents[1] / 'studio' / 'test_book_audio.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
book = _fixture.book


def observe(e, monkeypatch):
    calls, created = [], []
    class ObservedProduction(ProductionService):
        def __init__(self, root, **options):
            super().__init__(root, voices=e.voices, broker=e.production.broker,
                             runtime_factory=e.production.runtime_factory, **options)
            created.append(self)
        def close(self):
            calls.append('production.close')
            super().close()
    @contextmanager
    def bind(root, *, strict, cancelled):
        assert root == e.app.root and strict is True and not cancelled()
        calls.append('bind')
        try:
            yield object()
        finally:
            calls.append('unbind')
    monkeypatch.setattr(cli, 'ProductionService', ObservedProduction)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', bind)
    return calls, created


def instructions(e, tmp_path, step):
    source = tmp_path / 'instructions.json'
    source.write_text(json.dumps({'steps': [step.to_dict()]}), encoding='utf-8')
    return ['--data-dir', str(e.app.root), '--workspace', e.workspace,
            '--output-root', str(e.output.parent), '--instructions', str(source)]


@pytest.mark.parametrize('tool', ['book.review', 'book.prepare'])
def test_actual_passive_book_execution_uses_bound_owner_without_media_activation(book, tmp_path, monkeypatch, tool):
    e = book
    selected = _fixture.plan(e)
    e.service.save(selected)
    review = e.service.review(selected, e.output)
    args = {'plan': selected.id, 'plan_sha256': selected.fingerprint, 'name': e.output.name}
    if tool == 'book.prepare':
        args['review_sha256'] = review.fingerprint
    calls, owners = observe(e, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive Book review/preparation attempted selected audio activation.')
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', forbidden)
    assert cli.assistant_main([*instructions(e, tmp_path, Step(tool, args)), '--run']) == 0
    assert calls == ['production.close'] and len(owners) == 1
    assert owners[0]._closed and not e.voices.received and not tuple(e.output.iterdir())
    job, = AssistantService(e.app).jobs()
    assert job.status == 'complete'
    assert len(e.production.jobs()) == (2 if tool == 'book.prepare' else 0)


def test_actual_headless_book_run_binds_once_closes_producer_first_and_reuses_without_runtime(book, tmp_path, monkeypatch):
    _fixture.require_audio()
    e = book
    selected, ready = _fixture.prepared(e)
    calls, owners = observe(e, monkeypatch)
    step = Step('book.run', {'plan': selected.id, 'plan_sha256': selected.fingerprint,
                            'name': e.output.name, 'review_sha256': ready.fingerprint})
    assert cli.assistant_main([*instructions(e, tmp_path, step), '--run']) == 0
    assert calls == ['bind', 'production.close', 'unbind']
    assert len(owners) == 1 and owners[0]._closed and len(e.voices.received) == 2
    job, = AssistantService(e.app).jobs()
    assert job.status == 'complete' and len(job.results[0]['jobs']) == 2
    before = {row['path']: Path(row['path']).read_bytes() for row in job.results[0]['artifacts']}
    def forbidden(*args, **kwargs):
        pytest.fail('Verified completed Book receipt replay attempted media activation.')
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', forbidden)
    assert cli.assistant_main(['--data-dir', str(e.app.root), '--resume', job.id, '--run']) == 0
    assert calls == ['bind', 'production.close', 'unbind', 'production.close']
    assert len(e.voices.received) == 2 and len(owners) == 2 and owners[-1]._closed
    assert before == {path: Path(path).read_bytes() for path in before}


def test_actual_effect_inventory_execution_uses_binding_but_preview_does_not(book, tmp_path, monkeypatch):
    _fixture.require_audio()
    e = book
    calls, owners = observe(e, monkeypatch)
    args = instructions(e, tmp_path, Step('audio.effects'))
    assert cli.assistant_main(args) == 0
    assert calls == ['production.close'] and AssistantService(e.app).jobs() == ()
    assert cli.assistant_main([*args, '--run']) == 0
    assert calls == ['production.close', 'bind', 'production.close', 'unbind']
    job, = AssistantService(e.app).jobs()
    assert job.status == 'complete'
    assert '.wav' in job.results[0]['installed_runtime']['formats']
    assert all(owner._closed for owner in owners)


def test_actual_pending_native_inspection_keeps_cli_binding_and_producer_until_quiescence(book, tmp_path, monkeypatch):
    import wave
    _fixture.require_audio()
    e = book
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x00\x00' * 3000)
    calls, producers = observe(e, monkeypatch)
    release, owners, selected = threading.Event(), [], []
    original = process._stop_owned
    def stop(child):
        assert original(child) is True
        if not selected and str(child.args[-1]) == str(source):
            selected.append(child)
        if selected and child is selected[0] and not release.is_set():
            raise TimeoutError('Synthetic missing exit acknowledgement after real inspection')
        return True
    def pending(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    monkeypatch.setattr(process, '_stop_owned', stop)
    monkeypatch.setattr(cleanup, 'pending_cleanup', pending)
    args = instructions(e, tmp_path, Step('audio.inspect', {'path': str(source)}))
    try:
        with pytest.raises(cleanup.AudioCleanupPending):
            cli.assistant_main([*args, '--input-root', str(tmp_path), '--run'])
        assert selected and selected[0].poll() == 0
        assert calls == ['bind'] and len(producers) == 1 and not producers[0]._closed
        assert len(e.production.broker.snapshot()['leases']) == 1
        stage, = (e.app.root / 'runtime').glob('assistant-audio-*')
        assert stage.is_dir()
        release.set()
        assert all(owner.close() for owner in owners)
        assert calls == ['bind', 'production.close', 'unbind'] and producers[0]._closed
        assert not stage.exists() and e.production.broker.snapshot()['leases'] == []
        assert source.is_file()
    finally:
        release.set()
        for owner in owners:
            owner.close()



@pytest.mark.parametrize('masked', [False, True])
def test_close_origin_pending_retries_owned_producer_before_unbinding(book, tmp_path, monkeypatch, masked):
    from carr_library import runtime_settings

    _fixture.require_audio()
    e = book
    calls, producers, deferred = [], [], []
    first, second = threading.Event(), threading.Event()
    first_owner = cleanup.CleanupOwner(first.is_set)
    second_owner = cleanup.CleanupOwner(second.is_set)

    class Registry:
        def close(self):
            calls.append('registry.close')
            owner = first_owner if not first_owner.quiescent else second_owner
            if not owner.quiescent:
                pending = cleanup.AudioCleanupPending(
                    'Synthetic owned registry close awaits native acknowledgement',
                    cleanup_owner=owner)
                if masked:
                    raise RuntimeError('Registry finalization failed') from pending
                raise pending

    registry = Registry()
    monkeypatch.setattr(runtime_settings, 'voice_registry', lambda root: registry)

    class OwnedProduction(ProductionService):
        def __init__(self, root, **options):
            super().__init__(root, broker=e.production.broker,
                             runtime_factory=e.production.runtime_factory, **options)
            producers.append(self)
        def close(self):
            calls.append('production.close')
            if not self._closed:
                assert self.voices is registry
            super().close()

    @contextmanager
    def bind(root, *, strict, cancelled):
        assert root == e.app.root and strict and not cancelled()
        calls.append('bind')
        try:
            yield object()
        finally:
            calls.append('unbind')

    def pending(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        deferred.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)

    monkeypatch.setattr(cli, 'ProductionService', OwnedProduction)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', bind)
    monkeypatch.setattr(cleanup, 'pending_cleanup', pending)
    try:
        with pytest.raises(cleanup.AudioCleanupPending) as raised:
            cli.assistant_main([
                *instructions(e, tmp_path, Step('audio.effects')), '--run'])
        owner, = deferred
        assert raised.value.cleanup_owner is owner
        assert calls == ['bind', 'production.close', 'registry.close']
        assert len(producers) == 1 and producers[0]._closed
        assert producers[0]._voices is registry
        job, = AssistantService(e.app).jobs()
        assert job.status == 'complete'
        assert '.wav' in job.results[0]['installed_runtime']['formats']
        assert e.production.broker.snapshot()['leases'] == []

        assert owner.close() is False
        assert calls == ['bind', 'production.close', 'registry.close']
        first.set()
        assert first_owner.close() is True
        assert owner.close() is False
        assert calls == ['bind', 'production.close', 'registry.close',
                         'production.close', 'registry.close']
        assert producers[0]._voices is registry and 'unbind' not in calls

        second.set()
        assert second_owner.close() is True
        assert owner.close() is True
        assert calls == ['bind', 'production.close', 'registry.close',
                         'production.close', 'registry.close',
                         'production.close', 'registry.close', 'unbind']
        assert producers[0]._voices is None
        before = tuple(calls)
        assert owner.close() is True and tuple(calls) == before
    finally:
        first.set()
        second.set()
        first_owner.close()
        second_owner.close()
        for owner in deferred:
            owner.close()
