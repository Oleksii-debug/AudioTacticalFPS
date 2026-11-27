"""Real codec/publication batches; intent is deliberately not commit authority."""
from array import array
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
import wave

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.audio import AudioBudget, AudioRuntime, Effect
from carr_library.audio import conversion
from carr_library.audio import conversion_batch as module
from carr_library.audio.conversion import ConversionSettings, convert_audio, review_conversion
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import encode, read_json
from carr_library.operations import ResourceBroker, ResourceLimits


def tone(path, frequency=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = array('h', (round(4000 * math.sin(2 * math.pi * frequency * index / 24000))
                      for index in range(6000)))
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        stream.writeframes(data.tobytes())
    return path


@pytest.fixture
def peer(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg/FFprobe are required.'
    root, output = tmp_path / 'library', tmp_path / 'output'
    root.mkdir(); output.mkdir()
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    first, second = tone(tmp_path / 'a' / 'Україна.wav'), tone(tmp_path / 'b' / 'Україна.wav', 880)
    return SimpleNamespace(root=root, output=output, sources=[first, second], broker=broker,
        service=module.AudioConversionBatch(root),
        budget=AudioBudget(timeout=30, max_output_bytes=8 * 1024**2, reserve_disk_bytes=0),
        settings=ConversionSettings(channels=1))


def review(peer, *, sources=None, resume=False, format='wav', settings=None):
    return peer.service.prepare(peer.sources if sources is None else sources, peer.output,
        format=format, settings=peer.settings if settings is None else settings,
        budget=peer.budget, resume=resume)


def target(peer, value, index=0):
    return peer.output / value.plan['sources'][index]['output']


def environment():
    # Bind the real current imports, whether source checkout or installed wheel.
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))),
                PYTHONDONTWRITEBYTECODE='1')


def test_fresh_review_is_nonencoding_explicit_and_disambiguates_names(peer, monkeypatch):
    originals = [path.read_bytes() for path in peer.sources]
    monkeypatch.setattr(AudioRuntime, 'convert', lambda *a, **k: pytest.fail('Review encoded audio.'))
    chosen = review(peer)
    assert [row['output'] for row in chosen.plan['sources']] == ['001-Україна.wav', '002-Україна.wav']
    assert [row['source'] for row in chosen.plan['sources']] == list(map(str, peer.sources))
    assert chosen.plan['id'] == sha256(encode({k: v for k, v in chosen.plan.items() if k != 'id'})).hexdigest()
    assert not (peer.output / module.MANIFEST).exists()
    assert not any(path.suffix == '.wav' for path in peer.output.iterdir())
    assert [path.read_bytes() for path in peer.sources] == originals
    assert peer.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('format', ['wav', 'flac'])
def test_real_batch_retains_measured_receipts_and_never_reencodes_completed(peer, monkeypatch, format):
    before = [path.read_bytes() for path in peer.sources]
    chosen = review(peer, format=format)
    result = peer.service.run(chosen)
    assert result['status'] == 'completed' and result['done'] == 2 and result['receipt_persisted']
    document = read_json(peer.output / module.MANIFEST)
    for index, row in enumerate(document['items']):
        assert row['status'] == 'completed' and row['receipt']['audio']['frames'] == 6000
        assert row['receipt']['audio']['sha256'] == sha256(target(peer, chosen, index).read_bytes()).hexdigest()
        assert row['receipt']['source_sha256'] == sha256(before[index]).hexdigest()
    identities = [(path.read_bytes(), path.stat().st_mtime_ns) for path in
                  (target(peer, chosen), target(peer, chosen, 1))]
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Resume reencoded a completed output.'))
    resumed = peer.service.run(review(peer, resume=True, format=format))
    assert resumed['status'] == 'completed' and resumed['done'] == 2
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in
            (target(peer, chosen), target(peer, chosen, 1))] == identities
    assert [path.read_bytes() for path in peer.sources] == before
    assert peer.broker.snapshot()['leases'] == []


def test_cancel_between_actual_commits_retains_first_and_explicit_resume_only_encodes_second(peer, monkeypatch):
    cancelled, calls = Event(), []
    actual = module.convert_audio
    def convert(request, **options):
        calls.append(request.source)
        return actual(request, **options)
    monkeypatch.setattr(module, 'convert_audio', convert)
    chosen = review(peer)
    result = peer.service.run(chosen, cancelled=cancelled.is_set,
        progress=lambda done, total, phase: cancelled.set() if done == 1 else None)
    assert result['status'] == 'cancelled' and result['done'] == 1
    old = target(peer, chosen).read_bytes(), target(peer, chosen).stat().st_mtime_ns
    assert not target(peer, chosen, 1).exists() and calls == [peer.sources[0]]
    final = peer.service.run(review(peer, resume=True))
    assert final['status'] == 'completed' and calls == peer.sources
    assert (target(peer, chosen).read_bytes(), target(peer, chosen).stat().st_mtime_ns) == old


def test_invalid_second_source_retains_actual_first_receipt_and_no_third_attempt(peer):
    broken = peer.sources[1].parent / 'broken.wav'
    broken.write_bytes(b'Explicit selected bytes, but not audio.')
    chosen = review(peer, sources=[peer.sources[0], broken, peer.sources[1]])
    result = peer.service.run(chosen)
    assert result['status'] == 'partial' and result['done'] == 1
    assert [row['status'] for row in result['items']] == ['completed', 'failed', 'pending']
    assert target(peer, chosen).is_file()
    assert not target(peer, chosen, 1).exists() and not target(peer, chosen, 2).exists()
    assert broken.read_bytes() == b'Explicit selected bytes, but not audio.'
    assert peer.broker.snapshot()['leases'] == []


def test_actual_same_byte_competitor_is_uncertain_and_never_recovered(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    real = conversion.publish_file
    competitor = []
    def competing(staged, destination, **options):
        raw = Path(staged).read_bytes()
        Path(destination).write_bytes(raw)
        competitor.append(raw)
        return real(staged, destination, **options)
    monkeypatch.setattr(conversion, 'publish_file', competing)
    result = peer.service.run(chosen)
    assert len(competitor) == 1
    assert result['status'] == 'attention' and result['done'] == 0
    saved = read_json(peer.output / module.MANIFEST)['items'][0]
    assert saved['status'] == 'uncertain' and saved['receipt'] is None and saved['candidate']
    assert target(peer, chosen).read_bytes() == competitor[0]
    before = target(peer, chosen).stat().st_mtime_ns
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Unreceipted output was reencoded.'))
    outcome = peer.service.run(review(peer, sources=peer.sources[:1], resume=True))
    assert outcome['status'] == 'attention' and outcome['done'] == 0
    assert target(peer, chosen).read_bytes() == competitor[0]
    assert target(peer, chosen).stat().st_mtime_ns == before


_CRASH = r"""
import json, os, sys
from pathlib import Path
from carr_library.audio import conversion
from carr_library.audio import conversion_batch as module
from carr_library.audio.models import AudioBudget
from carr_library.audio.conversion import ConversionSettings
assert Path(module.__file__).resolve() == Path(sys.argv[1]).resolve()
root, source, output, boundary = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
service = module.AudioConversionBatch(root)
chosen = service.prepare([source], output, settings=ConversionSettings(channels=1),
    budget=AudioBudget(timeout=30, max_output_bytes=8*1024**2, reserve_disk_bytes=0))
if boundary == 'after-link':
    real = conversion.publish_file
    def crash(staged, destination, **options):
        real(staged, destination, **options)
        os._exit(87)
    conversion.publish_file = crash
else:
    real = module.atomic_json
    def crash(path, value, **options):
        if value['items'][0]['status'] == 'completed':
            os._exit(87)
        return real(path, value, **options)
    module.atomic_json = crash
service.run(chosen)
raise AssertionError('The actual commit boundary was not reached.')
"""


@pytest.mark.parametrize('boundary', ['after-link', 'before-success-receipt'])
def test_actual_child_death_retains_uncertain_output_without_hash_adoption(peer, monkeypatch, boundary):
    completed = subprocess.run([sys.executable, '-B', '-c', _CRASH,
        str(Path(module.__file__).resolve()), str(peer.root), str(peer.sources[0]),
        str(peer.output), boundary], env=environment(), capture_output=True, timeout=90)
    assert completed.returncode == 87, (completed.stdout, completed.stderr)
    document = read_json(peer.output / module.MANIFEST)
    row = document['items'][0]
    assert row['status'] == 'intent' and row['receipt'] is None
    path = peer.output / document['plan']['sources'][0]['output']
    raw, info = path.read_bytes(), path.lstat()
    assert sha256(raw).hexdigest() == row['candidate']['audio']['sha256']
    assert info.st_nlink == (2 if boundary == 'after-link' else 1)
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('An intent was adopted or duplicated.'))
    final = peer.service.run(review(peer, sources=peer.sources[:1], resume=True))
    assert final['status'] == 'attention' and final['done'] == 0
    assert path.read_bytes() == raw and path.lstat().st_nlink == info.st_nlink
    assert read_json(peer.output / module.MANIFEST)['items'][0]['receipt'] is None


def test_successful_output_with_failed_receipt_write_is_reported_without_retry(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    real = module.atomic_json
    def fail(path, value, **options):
        if value['items'][0]['status'] == 'completed':
            raise OSError('Controlled receipt publication failure.')
        return real(path, value, **options)
    monkeypatch.setattr(module, 'atomic_json', fail)
    result = peer.service.run(chosen)
    assert result['status'] == 'attention' and result['done'] == 1 and not result['receipt_persisted']
    assert result['unrecorded_commits'] == [chosen.plan['sources'][0]['output']]
    assert target(peer, chosen).is_file()
    assert read_json(peer.output / module.MANIFEST)['items'][0]['receipt'] is None
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Missing receipt became execution authority.'))
    resumed = peer.service.run(review(peer, sources=peer.sources[:1], resume=True))
    assert resumed['status'] == 'attention' and resumed['done'] == 0


def test_actual_final_receipt_sync_failure_keeps_visible_commit_and_honest_notice(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    real = module.atomic_json
    def uncertain(path, value, **options):
        result = real(path, value, **options)
        if value['items'][0]['status'] == 'completed':
            raise OSError('Controlled post-publication directory sync failure.')
        return result
    monkeypatch.setattr(module, 'atomic_json', uncertain)
    result = peer.service.run(chosen)
    assert result['status'] == 'attention' and result['done'] == 1 and result['receipt_persisted']
    assert read_json(peer.output / module.MANIFEST)['items'][0]['status'] == 'completed'
    assert target(peer, chosen).is_file()


@pytest.mark.parametrize('changed', ['source-bytes', 'source-replaced', 'folder', 'library', 'runtime'])
def test_reviewed_identity_change_refuses_publication(peer, monkeypatch, changed):
    chosen = review(peer, sources=peer.sources[:1])
    if changed == 'source-bytes':
        raw = bytearray(peer.sources[0].read_bytes()); raw[-1] ^= 1
        peer.sources[0].write_bytes(raw)
    elif changed == 'source-replaced':
        old = peer.sources[0].with_suffix('.old'); peer.sources[0].rename(old)
        peer.sources[0].write_bytes(old.read_bytes())
    elif changed in ('folder', 'library'):
        path = peer.output if changed == 'folder' else peer.root
        path.rename(path.with_name(path.name + '-old')); path.mkdir()
    else:
        real = peer.service._tools
        def different(runtime, check):
            values = real(runtime, check)
            values['ffmpeg']['sha256'] = 'f' * 64
            return values
        monkeypatch.setattr(peer.service, '_tools', different)
        monkeypatch.setattr(AudioRuntime, 'capabilities',
            lambda *a, **k: pytest.fail('A changed reviewed runtime was executed.'))
    try:
        result = peer.service.run(chosen)
    except ValueError:
        pass
    else:
        assert result['done'] == 0 and result['status'] != 'completed'
    assert not target(peer, chosen).exists()


@pytest.mark.parametrize('changed', ['source', 'destination', 'cancel'])
def test_checkpoint_precedes_existing_final_authority_checks(peer, changed):
    output = peer.output / 'direct.wav'
    request = review_conversion(peer.root, peer.sources[0], output,
        settings=peer.settings, budget=peer.budget)
    stopped = Event()
    def checkpoint(result, staged, runtime):
        if changed == 'source':
            raw = bytearray(peer.sources[0].read_bytes()); raw[-1] ^= 1
            peer.sources[0].write_bytes(raw)
        elif changed == 'destination':
            output.write_bytes(b'Explicit competing output.')
        else:
            stopped.set()
    with pytest.raises((ValueError, AudioCancelled, FileExistsError)):
        convert_audio(request, cancelled=stopped.is_set, before_publish=checkpoint)
    if changed == 'destination':
        assert output.read_bytes() == b'Explicit competing output.'
    else:
        assert not output.exists()


def test_checkpoint_never_runs_after_actual_visible_commit(peer, monkeypatch):
    output = peer.output / 'late.wav'
    request = review_conversion(peer.root, peer.sources[0], output,
        settings=peer.settings, budget=peer.budget)
    checkpoints, stopped = [], Event()
    real = conversion.publish_file
    def published(*args, **options):
        real(*args, **options)
        stopped.set()
    monkeypatch.setattr(conversion, 'publish_file', published)
    def checkpoint(result, staged, runtime):
        assert not output.exists() and not stopped.is_set()
        checkpoints.append(result.audio.sha256)
    result = convert_audio(request, cancelled=stopped.is_set, before_publish=checkpoint)
    assert output.is_file() and checkpoints == [result.audio.sha256]
    assert result.durability_confirmed and result.cleanup_warning is None


@pytest.mark.parametrize('invalid', ['existing-output', 'private-output', 'duplicate-source', 'too-many'])
def test_invalid_explicit_selection_refuses_encoding(peer, monkeypatch, invalid):
    sources, directory = peer.sources, peer.output
    if invalid == 'existing-output':
        (directory / '001-Україна.wav').write_bytes(b'Preexisting output.')
    elif invalid == 'private-output':
        directory = peer.root
    elif invalid == 'duplicate-source':
        sources = [peer.sources[0], peer.sources[0]]
    else:
        sources = [peer.sources[0]] * 65
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Invalid selection encoded.'))
    with pytest.raises((ValueError, FileExistsError)):
        peer.service.prepare(sources, directory, settings=peer.settings, budget=peer.budget)
    assert not (directory / module.MANIFEST).exists()


def test_completed_output_mutation_refuses_reuse_without_overwrite(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    assert peer.service.run(chosen)['status'] == 'completed'
    path = target(peer, chosen)
    raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Changed output was replaced.'))
    result = peer.service.run(review(peer, sources=peer.sources[:1], resume=True))
    assert result['status'] == 'attention' and path.read_bytes() == bytes(raw)


@pytest.mark.parametrize('mutation', ['foreign-plan', 'unknown-field', 'intent-as-receipt', 'oversized'])
def test_manifest_corruption_never_reopens_stored_source_or_runs_codec(peer, monkeypatch, mutation):
    chosen = review(peer, sources=peer.sources[:1])
    assert peer.service.run(chosen)['status'] == 'completed'
    path = peer.output / module.MANIFEST
    value = read_json(path)
    if mutation == 'foreign-plan':
        value['plan']['sources'][0]['source'] = str(peer.output / 'not-authorized.wav')
    elif mutation == 'unknown-field':
        value['extra_authority'] = True
    elif mutation == 'intent-as-receipt':
        value['items'][0]['status'] = 'intent'
    if mutation == 'oversized':
        path.write_bytes(b' ' * (module.MAX_MANIFEST_BYTES + 1))
    else:
        path.write_bytes(encode(value))
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Corrupt receipt ran a codec.'))
    with pytest.raises(ValueError):
        review(peer, sources=peer.sources[:1], resume=True)
    assert not (peer.output / 'not-authorized.wav').exists()


def test_manifest_semantic_change_after_review_refuses_even_with_unchanged_stat(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    assert peer.service.run(chosen)['status'] == 'completed'
    resumed = review(peer, sources=peer.sources[:1], resume=True)
    path = peer.output / module.MANIFEST
    value = read_json(path); value['items'][0]['error'] = 'Competing metadata.'
    path.write_bytes(encode(value))
    # Reproduce a metadata-only comparison's false admission.
    original_stamp = resumed.manifest_stamp[0]
    real_stamp = module._stamp
    monkeypatch.setattr(module, '_stamp', lambda info: original_stamp if info.st_ino == path.stat().st_ino else real_stamp(info))
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Changed manifest was admitted.'))
    with pytest.raises(ValueError, match='changed since review'):
        peer.service.run(resumed)


def test_existing_manifest_no_clobber_does_not_adopt_same_bytes(peer, monkeypatch):
    chosen = review(peer, sources=peer.sources[:1])
    real = module.atomic_json
    def competing(path, value, **options):
        Path(path).write_bytes(encode(value))
        return real(path, value, **options)
    monkeypatch.setattr(module, 'atomic_json', competing)
    with pytest.raises(FileExistsError):
        peer.service.run(chosen)
    assert read_json(peer.output / module.MANIFEST)['items'][0]['status'] == 'pending'
    assert not target(peer, chosen).exists()


def test_full_backup_keeps_external_manifest_external_and_restore_cannot_reauthorize_it(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    source = tone(tmp_path / 'explicit.wav')
    output = tmp_path / 'exports'; output.mkdir()
    settings, budget = ConversionSettings(channels=1), AudioBudget(timeout=30, max_output_bytes=8*1024**2)
    service = module.AudioConversionBatch(app.root)
    chosen = service.prepare([source], output, settings=settings, budget=budget)
    assert service.run(chosen)['status'] == 'completed'
    original_manifest = (output / module.MANIFEST).read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=digest)
    assert not list(restored.rglob(module.MANIFEST))
    assert (output / module.MANIFEST).read_bytes() == original_manifest
    with pytest.raises(ValueError, match='explicit current review'):
        module.AudioConversionBatch(restored).prepare([source], output, settings=settings,
            budget=budget, resume=True)


@pytest.mark.parametrize('linked', ['source', 'manifest', 'completed-output'])
def test_actual_hard_link_is_refused_without_deleting_either_name(peer, monkeypatch, linked):
    if linked == 'source':
        original = peer.sources[0]
        alias = original.with_name('same-inode.wav')
        os.link(original, alias)
        before = original.read_bytes()
        with pytest.raises(ValueError):
            review(peer, sources=peer.sources[:1])
        assert alias.read_bytes() == before and original.read_bytes() == before
        assert not (peer.output / module.MANIFEST).exists()
        return
    chosen = review(peer, sources=peer.sources[:1])
    assert peer.service.run(chosen)['status'] == 'completed'
    path = peer.output / module.MANIFEST if linked == 'manifest' else target(peer, chosen)
    alias = peer.output / 'independent-hard-link'
    os.link(path, alias)
    before = path.read_bytes()
    monkeypatch.setattr(module, 'convert_audio', lambda *a, **k: pytest.fail('Linked state/output admitted a codec.'))
    if linked == 'manifest':
        with pytest.raises(ValueError):
            review(peer, sources=peer.sources[:1], resume=True)
    else:
        outcome = peer.service.run(review(peer, sources=peer.sources[:1], resume=True))
        assert outcome['status'] == 'attention'
    assert path.read_bytes() == before and alias.read_bytes() == before and path.stat().st_nlink == 2


def test_progress_observer_failure_cannot_reverse_actual_batch_receipts(peer):
    def unavailable(*args):
        raise RuntimeError('Controlled optional progress observer failure.')
    chosen = peer.service.prepare(peer.sources, peer.output, settings=peer.settings,
        budget=peer.budget, progress=unavailable)
    result = peer.service.run(chosen, progress=unavailable)
    assert result['status'] == 'completed' and result['done'] == 2
    assert all(row['receipt'] for row in read_json(peer.output / module.MANIFEST)['items'])


def test_single_converter_keeps_its_exact_resource_admission_for_each_batch_item(peer, monkeypatch):
    actual, observed = AudioRuntime.convert, []
    def admitted(runtime, *args, **options):
        value = peer.broker.snapshot()
        observed.append(value)
        assert value['usage']['cpu_slots'] == peer.budget.threads
        return actual(runtime, *args, **options)
    monkeypatch.setattr(AudioRuntime, 'convert', admitted)
    result = peer.service.run(review(peer))
    assert result['status'] == 'completed' and len(observed) == 2
    assert peer.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('suffix', ['.exe', '.EXE', '.com', '.bat', '.cmd', '.bin'])
def test_actual_file_identity_accepts_name_derived_windows_execute_bits(tmp_path, suffix):
    # These are inert bytes, never an executable invocation. On Windows the
    # filename can affect st_mode while the opened descriptor has no suffix.
    path = tmp_path / ('reviewed-tool' + suffix)
    content = b'Exact source identity retained across named and opened metadata.'
    path.write_bytes(content)
    before = path.stat()
    result = module._file_identity(path, len(content), lambda: None)
    assert result['sha256'] == sha256(content).hexdigest()
    assert result['size'] == len(content)
    assert result['stamp'] == list(conversion._stamp(before))
    assert path.read_bytes() == content
