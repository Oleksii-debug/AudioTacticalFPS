"""Real shared leases around product render calls; renderer fixtures are contract-only."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceTimeout
from carr_library.operations.studio import render_with_resources
from carr_library.audio.jobs import RenderCancelled


def plan():
    return SimpleNamespace(to_dict=lambda: {'purpose': 'synthetic admission contract'})


def test_existing_renderer_runs_under_the_same_heavy_media_lease_and_releases(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    broker = ResourceBroker.for_app(private)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    seen = []
    def renderer(value, destination, **options):
        lease, = broker.snapshot()['leases']
        assert lease['operation_id'] == 'studio:synthetic'
        assert lease['request']['devices'] == ['heavy-media']
        assert options['cancelled']() is False
        seen.append((value, destination))
        return {'status': 'synthetic-contract-result'}
    selected = plan()
    result = render_with_resources(private, selected, tmp_path / 'output',
        engine=object(), ffmpeg=tmp_path / 'unused-native-runtime', renderer=renderer,
        operation_id='studio:synthetic')
    assert result['status'] == 'synthetic-contract-result' and seen == [(selected, tmp_path / 'output')]
    assert broker.snapshot()['leases'] == []


def test_an_existing_asr_lease_prevents_studio_entry_before_any_renderer_call(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    broker = ResourceBroker.for_app(private)
    broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    called = []
    with broker.reserve(ResourceRequest(devices=('heavy-media',)), operation_id='asr:synthetic', timeout=0):
        with pytest.raises(ResourceTimeout):
            render_with_resources(private, plan(), tmp_path / 'output',
                engine=object(), ffmpeg=tmp_path / 'unused-runtime',
                renderer=lambda *a, **k: called.append(True), admission_timeout=0)
    assert called == [] and broker.snapshot()['leases'] == []


def test_cancelled_admission_preserves_product_cancellation_type_without_starting(tmp_path):
    called = []
    with pytest.raises(RenderCancelled):
        render_with_resources(tmp_path / 'library', plan(), tmp_path / 'output',
            engine=object(), ffmpeg=tmp_path / 'unused-runtime', cancelled=lambda: True,
            renderer=lambda *a, **k: called.append(True))
    assert called == []


def test_resource_failure_never_suppresses_existing_render_failure(tmp_path):
    def renderer(*args, **kwargs):
        raise RuntimeError('Synthetic actual renderer failure')
    with pytest.raises(RuntimeError, match='actual renderer failure'):
        render_with_resources(tmp_path / 'library', plan(), tmp_path / 'output',
            engine=object(), ffmpeg=tmp_path / 'unused-runtime', renderer=renderer)
    assert ResourceBroker.for_app(tmp_path / 'library').snapshot()['leases'] == []
