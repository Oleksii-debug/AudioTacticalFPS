"""Declared admission for the supplied worker; no hardware or quality claim."""
import os

from .models import ResourceRequest, ResourceUnavailable

MIB = 1024 ** 2
GPU_REMAPPING = ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'HIP_VISIBLE_DEVICES', 'ROCR_VISIBLE_DEVICES')


def transcription_request(definition, job, limits):
    settings = definition['settings']
    if limits.memory_bytes == 0:
        raise ResourceUnavailable('Configure a positive declared memory allowance before transcription.')
    device = settings['device']
    if device not in ('cpu', 'cuda'):
        raise ResourceUnavailable('Choose CPU or CUDA explicitly for shared resource admission; no automatic device fallback was performed.')
    gpu = 'cuda:0' if device == 'cuda' else None
    capacity = dict(limits.gpu_devices).get(gpu, 0) if gpu else 0
    if gpu and not capacity:
        raise ResourceUnavailable('The selected CUDA device needs an explicit shared resource allowance.')
    if gpu and any(os.environ.get(name) for name in GPU_REMAPPING):
        raise ResourceUnavailable('Clear inherited GPU device remapping before reserving the selected CUDA device.')
    sizes = sorted((row['bytes'] for row in definition['inputs']), reverse=True)
    captures = sum(sizes[:settings['parallel_files']])
    formats = sum(settings['output_' + name] for name in ('txt', 'srt', 'vtt', 'json'))
    # Follow the reviewed donor's conservative whole-pool admission until
    # actual worker/model peaks are measured. Disk includes concurrent immutable
    # captures and a declared text/checkpoint allowance, not a measured output
    # duration or a new limit on accepted receipt bytes.
    disk = captures + 32 * MIB + len(sizes) * (formats + 1) * 8 * MIB
    return ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=limits.memory_bytes,
        gpu_device=gpu, gpu_memory_bytes=capacity, disk_bytes=disk, disk_path=job)

def production_request(plan, root, limits, *, music_devices=(), sound_seconds=None):
    """Reserve declared audio working copies without claiming measured duration.

    The existing ChunkCache still enforces the project's hard disk budget.
    Speech uses the donor's conservative half-second per source character;
    explicit pauses and measured local WAV durations remain exact inputs.
    """
    import math
    from ..audio.models import ProductionPlan, Line, Pause, Sound
    if not isinstance(plan, ProductionPlan):
        raise ValueError('A typed production plan is required for resource admission.')
    if limits.memory_bytes == 0:
        raise ResourceUnavailable('Configure a positive declared memory allowance before production.')
    devices = set(music_devices)
    if devices - {'cpu', 'cuda'}:
        raise ResourceUnavailable('Choose an explicit supported CPU or CUDA music device.')
    gpu = 'cuda:0' if 'cuda' in devices else None
    capacity = dict(limits.gpu_devices).get(gpu, 0) if gpu else 0
    if gpu and not capacity:
        raise ResourceUnavailable('The selected music CUDA device needs an explicit shared resource allowance.')
    if gpu and any(os.environ.get(name) for name in GPU_REMAPPING):
        raise ResourceUnavailable('Clear inherited GPU device remapping before reserving the selected music device.')
    seconds, used_roles = 0.0, set()
    sounds = sound_seconds or {}
    for scene in plan.scenes:
        for event in scene.events:
            if isinstance(event, Line):
                seconds += len(event.text) * .5
                used_roles.add(event.role)
            elif isinstance(event, Pause):
                seconds += event.seconds
            elif isinstance(event, Sound):
                duration = sounds.get(event.asset)
                if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                    raise ResourceUnavailable('Measure each selected local sound before reserving production resources.')
                seconds += duration
    pcm = math.ceil(max(1.0, seconds) * plan.sample_rate * 4)
    stems = len(used_roles) + 1 if plan.export_stems else 0
    music = int(any(scene.music is not None for scene in plan.scenes))
    # Cache, staged mix, independent publication, stems, generated music,
    # optional encoding and metadata may coexist on the selected output disk.
    disk = pcm * (8 + 3 * stems + 3 * music + int(plan.output_format != 'wav')) + 32 * MIB
    return ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=limits.memory_bytes,
        gpu_device=gpu, gpu_memory_bytes=capacity, disk_bytes=disk, disk_path=root)


def document_request(root, scratch, limits, *, original_bytes=0):
    """Use the existing native parser's 3 GiB limit as a declared allowance.

    Windows has time/process isolation, not a claimed address-space sandbox.
    Disk estimates cover retained sources/checkpoints and separate parser
    scratch; they do not replace the existing format and extraction budgets.
    """
    from ..catalog.streaming_pdf import MAX_PDF_BYTES
    if type(original_bytes) is not int or not 0 <= original_bytes <= MAX_PDF_BYTES:
        raise ValueError('Invalid document original byte reservation.')
    return ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=3 * 1024 ** 3,
        disk_bytes=max(1024 * MIB, original_bytes + 256 * MIB), disk_path=root,
        additional_disks=((scratch, max(512 * MIB, original_bytes + 64 * MIB)),))
