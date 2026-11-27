"""Canonical PR196 cache inclusion and typed historical byte-scope contracts.

The 128-case matrix preserves the canonical two-worker/four-suffix/four-cache-
layout/four-envelope population. Large boundary cases validate declared
streaming budgets only; they never pretend to measure 64 GiB throughput.
"""
from hashlib import sha256
import json

import pytest

from carr_library import backup_historical as historical
from carr_library import legacy_transcription as recovery
from carr_library import legacy_transcription_schema as schema
from carr_library.backup_legacy_state import MAX_STATE_BYTES, MAX_STATE_PARTS, validate_state
from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup, verify_backup
from tests.storage.legacy_transcription_fixture import canonical_data
from tests.storage.test_legacy_transcription_recovery import app_at, common_backup

GIB = 1024**3
JOB, SOURCE, KEY = '1' * 32, '2' * 32, '3' * 64


def entry(name, size=1):
    return {'filename': name, 'size_bytes': size, 'sha256': '4' * 64}


def envelope(shape, names, *, generated=False):
    version = {'v3-implicit': 3, 'v3-state': 3, 'v4-state': 4,
               'v4-sidecars': 4, 'v5-state': 5}[shape]
    value = {'version': version, 'kind': 'carr-library', 'schema_version': 5,
             'schema_fingerprint': '5' * 64, 'database': {}, 'assets': []}
    if shape in ('v3-state', 'v4-state', 'v5-state'):
        value['state'] = [dict(row, filename=row['filename'].removeprefix('state/')) for row in names]
    else:
        value['sidecars'] = names
    if shape != 'v3-implicit':
        value['generated_audio_included'] = generated
    if shape == 'v4-state':
        value['pending'] = {'fixture': 'nonempty; this unit checks only historical manifest accounting'}
    if shape == 'v5-state':
        value['pending'] = None
        value['reading_state'] = entry('reading-state.json')
    return value


@pytest.mark.parametrize('worker', ['worker', 'worker-history/' + '1' * 32 + '-' + '2' * 32])
@pytest.mark.parametrize('suffix', ['cache.json', 'receipt.json', 'json', 'txt'])
@pytest.mark.parametrize('layout', ['', '33/', 'verified-v1/', 'verified-v1/33/'])
@pytest.mark.parametrize('shape', ['v3-implicit', 'v3-state', 'v4-sidecars', 'v5-state'])
def test_manifest_requires_explicit_optional_asr_cache_inclusion(worker, suffix, layout, shape):
    base = f'state/transcription/jobs/{JOB}/'
    cache = base + f'{worker}/logs/cache/{layout}{KEY}.{suffix}'
    retained = base + f'inputs/{SOURCE}/source.wav'
    accepted = base + f'artifacts/{SOURCE}/{JOB}/transcript.txt'
    result = base + f'{worker}/results/{SOURCE}/source.txt'
    rows = [entry(retained), entry(accepted), entry(result),
            entry(cache, 0 if suffix == 'txt' else 1)]
    value = envelope(shape, rows, generated=True)
    assert schema.is_cache_file(cache)
    assert all(not schema.is_cache_file(name) for name in (retained, accepted, result))
    if shape != 'v3-implicit':
        assert historical.validate_manifest(value) in ('root', 'state', 'state-reading')
        value['generated_audio_included'] = False
    with pytest.raises(ValueError, match='Transcription cache requires explicit manifest inclusion'):
        historical.validate_manifest(value)
    value = envelope(shape, rows[:-1], generated=False)
    assert historical.validate_manifest(value) in ('root', 'state', 'state-reading')
    assert {row['filename'] for row in historical.root_entries(value)} >= {retained, accepted, result}


def reseal(backup, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_restore_rejects_cache_contradicting_rehashed_manifest_inclusion(tmp_path):
    app, _ = app_at(tmp_path)
    old = canonical_data(tmp_path / 'old', complete=True)
    cache = old.job_root / 'worker/logs/cache/verified-v1/33' / (KEY + '.txt')
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'Inert optional recognition cache; never speech output.')
    backup = tmp_path / 'canonical-backup'
    _, value = common_backup(app, backup, old)
    assert any(schema.is_cache_file(row['filename']) for row in value['sidecars'])
    value['generated_audio_included'] = True
    allowed = reseal(backup, value)
    verify_backup(backup, allowed)
    restored = restore_backup(backup, tmp_path / 'allowed-inert', allowed)
    row, = recovery.jobs(restored)
    assert recovery.read_transcript(restored, row['reference'], old.source['id'])['text'] == old.text
    assert not (restored / 'state/transcription').exists()
    value['generated_audio_included'] = False
    denied = reseal(backup, value)
    destination = tmp_path / 'never-published'
    with pytest.raises(ValueError, match='Transcription cache requires explicit manifest inclusion'):
        restore_backup(backup, destination, denied)
    assert not destination.exists()
    assert sha256((backup / 'manifest.json').read_bytes()).hexdigest() == denied
    assert cache.read_bytes() == b'Inert optional recognition cache; never speech output.'


@pytest.mark.parametrize('shape', ['v3-implicit', 'v3-state', 'v4-sidecars', 'v5-state'])
def test_all_noncache_historical_shapes_restore_accepted_results_and_rebackup(tmp_path, shape):
    from carr_library.reader.reading_state import _encode
    app, _ = app_at(tmp_path)
    old = canonical_data(tmp_path / 'old', complete=True)
    backup = tmp_path / 'historical'
    _, original = common_backup(app, backup, old)
    value = envelope(shape, original['sidecars'], generated=False)
    for field in ('schema_version', 'schema_fingerprint', 'database', 'assets'):
        value[field] = original[field]
    if shape == 'v5-state':
        raw = _encode({}, {}).encode('utf-8')
        (backup / 'reading-state.json').write_bytes(raw)
        value['reading_state'] = dict(filename='reading-state.json',
                                     size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = reseal(backup, value)
    original_manifest = (backup / 'manifest.json').read_bytes()
    verify_backup(backup, checksum)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (restored / historical.DIRECTORY / checksum / 'manifest.json').read_bytes() == original_manifest
    assert not (restored / 'state/transcription').exists()
    row, = recovery.jobs(restored)
    assert row['reference']['kind'] == 'historical'
    assert recovery.read_transcript(restored, row['reference'], old.source['id'])['text'] == old.text
    assert recovery.read_transcript(restored, row['reference'], old.source['id'], edited=True)['text'] == 'Збережена окрема правка.'
    second = tmp_path / 'rebackup'
    digest = LibraryApp(restored).backup(second)
    verify_backup(second, digest)


@pytest.mark.parametrize('shape', ['v3-implicit', 'v3-state', 'v4-sidecars', 'v5-state'])
@pytest.mark.parametrize('sizes,accepted', [
    ([32 * GIB], True), ([32 * GIB + 1], False),
    ([32 * GIB, 32 * GIB], True), ([32 * GIB, 32 * GIB, 1], False),
])
def test_canonical_input_declarations_keep_32gib_per_source_64gib_aggregate(shape, sizes, accepted):
    rows = [entry(f'state/transcription/jobs/{JOB}/inputs/{index:032x}/source.wav', size)
            for index, size in enumerate(sizes, 1)]
    value = envelope(shape, rows)
    if accepted:
        assert historical.validate_manifest(value) in ('root', 'state', 'state-reading')
    else:
        with pytest.raises(ValueError):
            historical.validate_manifest(value)
    assert schema.MAX_SOURCE_BYTES == 32 * GIB and schema.MAX_INPUT_BYTES == 64 * GIB


@pytest.mark.parametrize('shape', ['v3-state', 'v4-state', 'v5-state'])
def test_typed_asr_allowance_does_not_increase_generic_historical_limit_or_depth(shape):
    assert MAX_STATE_BYTES == 32 * GIB and MAX_STATE_PARTS == 2048
    typed = [entry(f'state/transcription/jobs/{JOB}/inputs/{index:032x}/source.wav', 32 * GIB)
             for index in (1, 2)]
    deep = 'state/' + '/'.join(['ordinary'] * 20) + '/retained.bin'
    generic = entry(deep, MAX_STATE_BYTES)
    value = envelope(shape, [*typed, generic])
    assert historical.validate_manifest(value) in ('state', 'state-reading')
    generic['size_bytes'] += 1
    with pytest.raises(ValueError, match='byte budget'):
        historical.validate_manifest(envelope(shape, [*typed, generic]))
    with pytest.raises(ValueError, match='byte budget'):
        validate_state([entry('ordinary.bin', MAX_STATE_BYTES + 1)])


def test_explicit_whole_archive_budget_still_bounds_typed_streaming_declarations():
    rows = [entry(f'state/transcription/jobs/{JOB}/inputs/{index:032x}/source.wav', 32 * GIB)
            for index in (1, 2)]
    with pytest.raises(ValueError, match='byte budget'):
        historical.validate_manifest(envelope('v3-state', rows), max_bytes=64 * GIB - 1)


def test_implicit_v3_dispatch_remains_disjoint_from_current_reader_and_scoped_asr():
    value = envelope('v3-implicit', [entry('reading-state.json')])
    assert historical.manifest_layout(value) is None
    value['sidecars'] = [entry('state/transcription/' + 'a' * 64 + '/jobs/' + JOB + '/job.json')]
    assert historical.manifest_layout(value) is None
    value['sidecars'] = [entry('state/transcription/jobs/' + JOB + '/job.json')]
    assert historical.manifest_layout(value) == 'root'
