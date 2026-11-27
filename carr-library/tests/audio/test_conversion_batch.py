"""Actual FFmpeg batch conversions and prepared-publication recovery.

No synthetic renderer, skipped prerequisite, private model or physical device
stands in for measured files. Failure seams preserve the real encoder/publisher.
"""
from array import array
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioCancelled, AudioError, Effect
from carr_library.audio import conversion_batch as module
from carr_library.audio import dsp
from carr_library.durable_state import read_json
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.runtime_settings import audio_runtime


def tone(path, *, duration=.125, frequency=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    values = array('h', (round(4000 * math.sin(2 * math.pi * frequency * index / 24000))
                         for index in range(round(duration * 24000))))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    return path


@pytest.fixture
def peer(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir(); outputs.mkdir()
    first = tone(inputs / 'a' / 'same name.wav')
    second = tone(inputs / 'b' / 'same name.wav', frequency=880)
    budget = AudioBudget(timeout=30, max_output_bytes=64 * 1024**2,
                         max_alloc_bytes=16 * 1024**2, reserve_disk_bytes=0, threads=1)
    runtime = audio_runtime(outputs, state_root=app.root / 'state',
                            input_roots=(inputs,), budget=budget)
    # Missing actual tools is a failed prerequisite, never a skipped acceptance.
    assert Path(runtime.ffmpeg).is_file() and Path(runtime.ffprobe).is_file()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(
        cpu_slots=1, memory_bytes=256 * 1024**2, disk_bytes=256 * 1024**2,
        min_free_disk_bytes=0))
    service = module.AudioConversionBatch(runtime, library_root=app.root, broker=broker)
    return SimpleNamespace(app=app, inputs=inputs, outputs=outputs, first=first,
        second=second, runtime=runtime, broker=broker, service=service)


def chosen(peer, sources=None, *, format='wav', name='reviewed', **kwargs):
    return peer.service.prepare(sources or [peer.first, peer.second], peer.outputs / name,
                                 format=format, channels=1, **kwargs)


def environment():
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))))


def test_review_is_bounded_nonpublishing_and_disambiguates_exact_selected_paths(peer):
    before = {str(path): path.read_bytes() for path in (peer.first, peer.second)}
    plan = chosen(peer)
    assert not Path(plan['directory']).exists()
    assert [row['output'] for row in plan['sources']] == ['001-same name.wav', '002-same name.wav']
    assert [row['source'] for row in plan['sources']] == [str(peer.first), str(peer.second)]
    assert all(row['information']['frames'] == 3000 for row in plan['sources'])
    assert plan['id'] == module._plan_id(plan)
    assert peer.broker.snapshot()['leases'] == []
    assert {str(path): path.read_bytes() for path in (peer.first, peer.second)} == before


@pytest.mark.parametrize('format', ['wav', 'flac'])
def test_actual_batch_keeps_per_file_receipts_and_failed_inputs_without_clobber(peer, monkeypatch, format):
    broken = peer.inputs / 'broken.wav'
    broken.write_bytes(b'Not an encoded sound file.')
    originals = {str(path): path.read_bytes() for path in (peer.first, broken, peer.second)}
    plan = chosen(peer, [peer.first, broken, peer.second], format=format)
    calls, closure = [], []
    convert = peer.runtime.convert
    def actual(*args, **kwargs):
        calls.append(str(args[0]))
        assert peer.broker.snapshot()['usage']['cpu_slots'] == 1
        try:
            return convert(*args, **kwargs)
        finally:
            closure.append(peer.broker.snapshot())
    monkeypatch.setattr(peer.runtime, 'convert', actual)
    result = peer.service.run(plan)
    assert result['status'] == 'partial' and result['done'] == 2 and result['failed'] == 1
    assert [row['status'] for row in result['items']] == ['completed', 'failed', 'completed']
    assert calls == [str(peer.first), str(peer.second)]
    assert all(snapshot['usage']['cpu_slots'] == 1 for snapshot in closure)
    for row in result['items']:
        path = Path(plan['directory']) / row['output']
        if row['status'] == 'completed':
            assert path.is_file()
            assert sha256(path.read_bytes()).hexdigest() == row['candidate']['sha256']
            assert row['candidate']['frames'] == 3000 and row['candidate']['channels'] == 1
        else:
            assert not path.exists() and row['error']
    assert read_json(Path(plan['directory']) / module.MANIFEST) == result
    assert {str(path): path.read_bytes() for path in (peer.first, broken, peer.second)} == originals
    assert peer.broker.snapshot()['leases'] == []


def test_cancel_after_first_actual_commit_then_resume_never_reencodes_completed(peer, monkeypatch):
    plan = chosen(peer)
    stop, calls = threading.Event(), []
    convert = peer.runtime.convert
    def actual(source, *args, **kwargs):
        calls.append(str(source))
        return convert(source, *args, **kwargs)
    monkeypatch.setattr(peer.runtime, 'convert', actual)
    def progress(done, total, phase):
        if done == 1:
            stop.set()
    result = peer.service.run(plan, cancelled=stop.is_set, progress=progress)
    assert result['status'] == 'cancelled' and result['done'] == 1
    first = Path(plan['directory']) / plan['sources'][0]['output']
    before = (first.read_bytes(), first.stat().st_mtime_ns)
    assert calls == [str(peer.first)] and peer.broker.snapshot()['leases'] == []
    completed = peer.service.run(plan, resume=True)
    assert completed['status'] == 'completed' and completed['done'] == 2
    assert calls == [str(peer.first), str(peer.second)]
    assert (first.read_bytes(), first.stat().st_mtime_ns) == before
    assert peer.broker.snapshot()['leases'] == []


def test_late_actual_publish_exception_reconciles_only_bound_output(peer, monkeypatch):
    plan = chosen(peer, [peer.first])
    publish = dsp.publish_file
    def committed(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError('Observed post-link cleanup fault.')
    monkeypatch.setattr(dsp, 'publish_file', committed)
    result = peer.service.run(plan)
    assert result['status'] == 'completed' and result['done'] == 1
    assert 'committed' in result['warning']
    assert result['items'][0]['candidate']['sha256'] == sha256(
        (Path(plan['directory']) / result['items'][0]['output']).read_bytes()).hexdigest()
    monkeypatch.setattr(peer.runtime, 'convert', lambda *a, **k: pytest.fail('Committed audio was encoded again.'))
    assert peer.service.run(plan, resume=True)['done'] == 1


def test_actual_owner_death_after_link_recovers_exact_owned_staging_without_second_encode(peer, monkeypatch):
    plan = chosen(peer, [peer.first])
    plan_path = peer.outputs / 'selected-plan.json'
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    program = r'''
import json,os,sys
from pathlib import Path
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio import dsp
from carr_library.audio.conversion_batch import AudioConversionBatch
from carr_library.operations import ResourceBroker
plan=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
runtime=AudioRuntime(Path(plan['directory']).parent,ffmpeg=plan['tools']['ffmpeg']['path'],
    ffprobe=plan['tools']['ffprobe']['path'],input_roots=(Path(plan['sources'][0]['source']).parent,),
    budget=AudioBudget(timeout=30,max_output_bytes=64*1024**2,max_alloc_bytes=16*1024**2,
                       reserve_disk_bytes=0,threads=1))
service=AudioConversionBatch(runtime,library_root=Path(sys.argv[2]),
    broker=ResourceBroker.for_app(Path(sys.argv[2])))
publish=dsp.publish_file
def crash(*args,**kwargs):
    publish(*args,**kwargs)
    os._exit(23)
dsp.publish_file=crash
service.run(plan)
'''
    child = subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8', '-c', program,
                            str(plan_path), str(peer.app.root)], env=environment(),
                           capture_output=True, text=True, timeout=45)
    assert child.returncode == 23, child.stdout + child.stderr
    directory = Path(plan['directory'])
    saved = read_json(directory / module.MANIFEST)
    row, = saved['items']
    assert row['status'] == 'prepared' and saved['done'] == 0
    output = directory / row['output']
    staging = directory / row['candidate']['staging']['relative']
    assert output.is_file() and staging.is_file()
    assert output.stat().st_nlink == staging.stat().st_nlink == 2
    before = output.read_bytes()
    monkeypatch.setattr(peer.runtime, 'convert', lambda *a, **k: pytest.fail('Published bytes were synthesized twice.'))
    result = peer.service.run(plan, resume=True)
    assert result['status'] == 'completed' and result['done'] == 1
    assert output.read_bytes() == before and output.stat().st_nlink == 1
    assert not staging.exists()
    assert peer.broker.snapshot()['leases'] == []


def test_completed_receipt_does_not_adopt_changed_output_or_an_unbound_collision(peer):
    plan = chosen(peer)
    stop = threading.Event()
    result = peer.service.run(plan, cancelled=stop.is_set,
        progress=lambda done, total, phase: stop.set() if done == 1 else None)
    first = Path(plan['directory']) / result['items'][0]['output']
    first.write_bytes(b'Unrelated replacement; preserve it.')
    changed = first.read_bytes()
    refused = peer.service.run(plan, resume=True)
    assert refused['status'] != 'completed' and first.read_bytes() == changed
    assert not (Path(plan['directory']) / result['items'][1]['output']).exists()


def test_source_change_after_review_refuses_before_creating_any_output(peer):
    plan = chosen(peer)
    before = peer.first.stat()
    changed = bytearray(peer.first.read_bytes())
    changed[-1] ^= 1
    peer.first.write_bytes(changed)
    os.utime(peer.first, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match='changed'):
        peer.service.run(plan)
    assert not Path(plan['directory']).exists() and peer.broker.snapshot()['leases'] == []


def test_output_race_before_actual_link_preserves_foreign_bytes_and_other_items(peer, monkeypatch):
    plan = chosen(peer)
    save = peer.service._save
    raced = []
    def after_prepare(directory, value, **kwargs):
        result = save(directory, value, **kwargs)
        row = value['items'][0]
        if row['status'] == 'prepared' and not raced:
            raced.append(directory / row['output'])
            raced[0].write_bytes(b'Foreign occupant; never replace.')
        return result
    monkeypatch.setattr(peer.service, '_save', after_prepare)
    result = peer.service.run(plan)
    assert raced and raced[0].read_bytes() == b'Foreign occupant; never replace.'
    assert result['status'] != 'completed'
    assert result['done'] == 0
    assert not (Path(plan['directory']) / plan['sources'][1]['output']).exists()
    assert peer.broker.snapshot()['leases'] == []


def test_scope_revocation_in_final_candidate_callback_prevents_publication(peer, monkeypatch):
    plan = chosen(peer, [peer.first])
    revoked = threading.Event()
    def guard():
        if revoked.is_set():
            raise ValueError('The explicitly reviewed library changed.')
    peer.service.scope_guard = guard
    save = peer.service._save
    def after_prepare(directory, value, **kwargs):
        result = save(directory, value, **kwargs)
        if value['items'][0]['status'] == 'prepared':
            revoked.set()
        return result
    monkeypatch.setattr(peer.service, '_save', after_prepare)
    result = peer.service.run(plan)
    assert revoked.is_set() and result['done'] == 0 and result['status'] == 'failed'
    assert not (Path(plan['directory']) / plan['sources'][0]['output']).exists()
    assert peer.broker.snapshot()['leases'] == []


def test_manifest_write_failure_after_real_commit_reports_recoverable_output(peer, monkeypatch):
    plan = chosen(peer, [peer.first])
    save = peer.service._save
    def full_disk(directory, value, **kwargs):
        if any(row['status'] == 'completed' for row in value['items']):
            raise OSError('No space for final status.')
        return save(directory, value, **kwargs)
    monkeypatch.setattr(peer.service, '_save', full_disk)
    result = peer.service.run(plan)
    assert result['status'] == 'attention' and result['done'] == 1
    assert 'committed' in result['warning']
    saved = read_json(Path(plan['directory']) / module.MANIFEST)
    assert saved['items'][0]['status'] == 'prepared' and saved['done'] == 0
    monkeypatch.setattr(peer.service, '_save', save)
    monkeypatch.setattr(peer.runtime, 'convert', lambda *a, **k: pytest.fail('Committed output was repeated.'))
    assert peer.service.run(plan, resume=True)['status'] == 'completed'


def test_explicit_limits_and_hardlinks_refuse_before_output_creation(peer):
    with pytest.raises(ValueError, match='one to 64'):
        chosen(peer, [peer.first] * 65)
    linked = peer.inputs / 'linked.wav'
    os.link(peer.first, linked)
    with pytest.raises(ValueError):
        chosen(peer, [linked])
    assert list(peer.outputs.iterdir()) == []


def test_runtime_change_during_actual_admission_refuses_before_any_encode(peer, monkeypatch):
    copied = peer.inputs / ('reviewed-ffmpeg' + Path(peer.runtime.ffmpeg).suffix)
    shutil.copyfile(peer.runtime.ffmpeg, copied)
    copied.chmod(Path(peer.runtime.ffmpeg).stat().st_mode)
    peer.runtime.ffmpeg = str(copied)
    plan = chosen(peer, [peer.first])
    reserve = peer.broker.reserve
    @contextmanager
    def changed(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            if kwargs['operation_id'] == 'audio-convert:' + plan['id']:
                with copied.open('ab') as stream:
                    stream.write(b'Changed after actual resource admission; never execute.')
            yield lease
    monkeypatch.setattr(peer.broker, 'reserve', changed)
    monkeypatch.setattr(peer.runtime, 'convert', lambda *a, **k: pytest.fail('Changed runtime was executed.'))
    result = peer.service.run(plan)
    assert result['status'] == 'failed' and result['done'] == 0
    assert peer.broker.snapshot()['leases'] == []
    assert not (Path(plan['directory']) / plan['sources'][0]['output']).exists()


def test_prepublication_cancel_refuses_new_output_and_keeps_prepared_receipt(peer, monkeypatch):
    plan = chosen(peer, [peer.first])
    stop = threading.Event()
    save = peer.service._save
    def prepared(directory, value, **kwargs):
        result = save(directory, value, **kwargs)
        if value['items'][0]['status'] == 'prepared':
            stop.set()
        return result
    monkeypatch.setattr(peer.service, '_save', prepared)
    result = peer.service.run(plan, cancelled=stop.is_set)
    assert result['status'] == 'cancelled' and result['done'] == 0
    assert not (Path(plan['directory']) / plan['sources'][0]['output']).exists()
    assert result['items'][0]['candidate'] is not None
    assert peer.broker.snapshot()['leases'] == []
