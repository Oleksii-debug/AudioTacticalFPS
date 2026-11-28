"""Admission arithmetic without claiming measured runtime/model capacities."""
from dataclasses import replace
from pathlib import Path

import pytest

from carr_library.operations import ResourceLimits, ResourceUnavailable
from carr_library.operations.admission import GPU_REMAPPING, transcription_request
from carr_library.transcription_receipt import DEFAULT_SETTINGS

MIB = 1024 ** 2


def definition(**settings):
    return {'settings': {**DEFAULT_SETTINGS, **settings},
            'inputs': [{'bytes': size} for size in (3 * MIB, 9 * MIB, 5 * MIB)]}


def test_cpu_reserves_all_declared_compute_and_each_parallel_capture(tmp_path):
    limits = ResourceLimits(cpu_slots=3, memory_bytes=2048 * MIB)
    request = transcription_request(definition(parallel_files=2), tmp_path, limits)
    assert request.cpu_slots == 3 and request.memory_bytes == 2048 * MIB
    assert request.gpu_device is None and request.gpu_memory_bytes == 0
    assert request.disk_path == tmp_path
    assert request.disk_bytes == (9 + 5 + 32 + 3 * 2 * 8) * MIB


def test_each_selected_output_adds_its_declared_metadata_allowance(tmp_path):
    limits = ResourceLimits()
    request = transcription_request(definition(parallel_files=3, output_txt=True,
        output_srt=True, output_vtt=True, output_json=True), tmp_path, limits)
    assert request.disk_bytes == (3 + 9 + 5 + 32 + 3 * 5 * 8) * MIB


@pytest.mark.parametrize('device', ['auto', 'unreviewed-device'])
def test_unknown_device_never_falls_back_to_cpu(tmp_path, device):
    with pytest.raises(ResourceUnavailable, match='explicitly'):
        transcription_request(definition(device=device), tmp_path, ResourceLimits())


def test_zero_memory_policy_never_starts_unbudgeted_inference(tmp_path):
    with pytest.raises(ResourceUnavailable, match='memory allowance'):
        transcription_request(definition(), tmp_path, ResourceLimits(memory_bytes=0))


def test_cuda_requires_exact_configured_device_and_positive_memory(tmp_path):
    with pytest.raises(ResourceUnavailable, match='CUDA device'):
        transcription_request(definition(device='cuda'), tmp_path,
            ResourceLimits(gpu_devices=(('cuda:1', 1024 * MIB),)))


def test_cuda_reserves_the_whole_explicitly_declared_device(tmp_path, monkeypatch):
    for name in GPU_REMAPPING:
        monkeypatch.delenv(name, raising=False)
    limits = ResourceLimits(gpu_devices=(('cuda:0', 4096 * MIB),))
    request = transcription_request(definition(device='cuda'), tmp_path, limits)
    assert request.gpu_device == 'cuda:0' and request.gpu_memory_bytes == 4096 * MIB
    assert request.memory_bytes == limits.memory_bytes


@pytest.mark.parametrize('name', GPU_REMAPPING)
def test_remapped_cuda_is_rejected_before_worker_selection(tmp_path, monkeypatch, name):
    for candidate in GPU_REMAPPING:
        monkeypatch.delenv(candidate, raising=False)
    monkeypatch.setenv(name, '1')
    with pytest.raises(ResourceUnavailable, match='remapping'):
        transcription_request(definition(device='cuda'), tmp_path,
            ResourceLimits(gpu_devices=(('cuda:0', 4096 * MIB),)))


def test_cpu_selection_does_not_reserve_an_inherited_cuda_device(tmp_path, monkeypatch):
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '1')
    request = transcription_request(definition(device='cpu'), tmp_path, ResourceLimits())
    assert request.gpu_device is None
