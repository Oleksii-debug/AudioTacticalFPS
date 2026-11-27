"""Real bounded >512 MiB historical queue restore/import/rebackup acceptance.

This test-only producer physically writes PCM in fixed chunks. It does not start
a playback device, infer speech, qualify 4 GiB inputs or substitute sparse data.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from time import monotonic
import wave

MIB = 1024**2
GIB = 1024**3


def estimate_disk_bytes(pcm_bytes):
    if type(pcm_bytes) is not int or not 513 * MIB <= pcm_bytes <= 3072 * MIB:
        raise ValueError('Choose an actual PCM payload between 513 and 3072 MiB.')
    # Old source, inert restore, managed copy, complete backup, final restore
    # and temporary publication can overlap. Duplicate queue items reuse bytes.
    return 8 * (pcm_bytes + 44) + 2 * GIB


def _hash(path, stopping):
    digest, size = sha256(), 0
    with path.open('rb') as stream:
        while True:
            stopping()
            block = stream.read(MIB)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def _physical_pcm(path, pcm_bytes, stopping):
    path.parent.mkdir(parents=True, exist_ok=False)
    block = bytes(range(256)) * (MIB // 256)
    count = 0
    with path.open('xb') as output:
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 8000, pcm_bytes // 2, 'NONE', 'PCM'))
            while count < pcm_bytes:
                stopping()
                amount = min(len(block), pcm_bytes - count)
                audio.writeframesraw(block[:amount])
                count += amount
        output.flush()
        os.fsync(output.fileno())
    if count != pcm_bytes or path.stat().st_size != pcm_bytes + 44:
        raise AssertionError('The physical fixture does not contain the selected complete PCM payload.')


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', type=Path, required=True,
                        help='New isolated test directory; existing paths are refused.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-package', type=Path, required=True,
                        help='Exact installed carr_library/__init__.py path.')
    parser.add_argument('--mib', type=int, default=513)
    parser.add_argument('--seconds', type=int, default=3600)
    args = parser.parse_args(argv)
    if not 60 <= args.seconds <= 7200:
        parser.error('The complete acceptance deadline must be between 60 and 7200 seconds.')
    required = estimate_disk_bytes(args.mib * MIB)
    import carr_library
    from carr_library.application import LibraryApp, open_existing_library
    from carr_library.audio import legacy_playlist, legacy_playlist_state, playlist
    from carr_library.audio.playback_processing import PlaybackControls
    from carr_library.backup_historical import DIRECTORY
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    expected = args.expected_package.resolve(strict=True)
    if Path(carr_library.__file__).resolve(strict=True) != expected:
        raise RuntimeError('Acceptance must use the explicitly selected installed product.')
    for module in (legacy_playlist, legacy_playlist_state, playlist):
        if expected.parent not in Path(module.__file__).resolve(strict=True).parents:
            raise RuntimeError('An acceptance dependency came from outside the selected product.')
    work = args.workdir.absolute()
    if work.exists() or work.is_symlink():
        raise ValueError('Choose a new isolated acceptance directory.')
    if not work.parent.is_dir() or work.parent.is_symlink():
        raise ValueError('The selected acceptance parent must already be an ordinary directory.')
    available = shutil.disk_usage(work.parent).free
    if available < required:
        raise OSError(f'The selected acceptance requires {required} free bytes before producing data.')
    work.mkdir(mode=0o700)
    started, timings, checks = monotonic(), {}, []
    def stopping():
        if monotonic() - started >= args.seconds:
            raise TimeoutError('The real queue acceptance exceeded its explicit overall deadline.')
        return False
    def phase(name, action):
        before = monotonic()
        result = action()
        timings[name] = round(monotonic() - before, 3)
        return result
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    app = LibraryApp(work / 'source-library')
    archive = work / 'canonical-backup'
    phase('create_empty_base', lambda: create_backup(app, archive, cancelled=stopping))
    metadata = json.loads((archive / 'manifest.json').read_bytes())
    if metadata['version'] != 2:
        raise AssertionError('The empty canonical compatibility fixture must start from basic schema 2.')
    original = work / 'physical-source' / 'fixture.wav'
    pcm_bytes = args.mib * MIB
    phase('write_physical_pcm', lambda: _physical_pcm(original, pcm_bytes, stopping))
    size, checksum = phase('hash_original', lambda: _hash(original, stopping))
    member = 'state/studio/inputs/' + checksum + '.audio'
    retained = archive / member
    retained.parent.mkdir(parents=True)
    original.replace(retained)
    item = dict(filename=member, label='Retained Україна recording', sha256=checksum,
                size_bytes=size, frames=pcm_bytes // 2, sample_rate=8000,
                channels=1, sample_width=2)
    raw_queue = _json(dict(schema='carr-playback-queue/1',
        items=[item, dict(item, label='Second selected occurrence')], index=1, frame=7911,
        volume=31, muted=True, balance=-.4, skips=[11, 43],
        output=dict(index=127, name='Historical device', interface='inert-old-interface',
                    manufacturer=1, product=2, driver=3, channels=2)))
    queue_path = archive / legacy_playlist_state.NAME
    queue_path.parent.mkdir(parents=True)
    queue_path.write_bytes(raw_queue)
    metadata.update(version=4, generated_audio_included=False, sidecars=[
        dict(filename=member, size_bytes=size, sha256=checksum),
        dict(filename=legacy_playlist_state.NAME, size_bytes=len(raw_queue),
             sha256=sha256(raw_queue).hexdigest())])
    manifest = _json(metadata)
    (archive / 'manifest.json').write_bytes(manifest)
    manifest_pin = sha256(manifest).hexdigest()
    phase('verify_actual_historical_backup', lambda: verify_backup(archive, manifest_pin, cancelled=stopping))
    restored_path = phase('restore_inert_history', lambda: restore_backup(
        archive, work / 'restored-library', manifest_pin, cancelled=stopping))
    restored = open_existing_library(restored_path)
    check('restore leaves historical queue inactive', not (restored.root / legacy_playlist_state.NAME).exists())
    historical = restored.root / DIRECTORY / manifest_pin
    raw_member = historical / 'files' / sha256(legacy_playlist_state.NAME.encode()).hexdigest()
    check('restore preserves exact old queue bytes', raw_member.read_bytes() == raw_queue)
    historical_audio = historical / 'files' / sha256(member.encode()).hexdigest()
    check('actual retained historical input exceeds 512 MiB', historical_audio.stat().st_size > 512 * MIB)
    # A real complete copy exists under the restored root; remove only our owned
    # old fixture so migration cannot quietly depend on its original location.
    shutil.rmtree(archive)
    reviewed = phase('review_retained_history', lambda: legacy_playlist.review_archive(
        restored.root, manifest_pin, voice='sapi5', language='uk-UA', cancelled=stopping))
    imported = phase('import_current_managed_queue', lambda: legacy_playlist.import_reviewed(
        reviewed, cancelled=stopping))
    store = playlist.PlaylistStore(restored.root, imported.project_id)
    current = phase('verify_current_queue', lambda: store.review(cancelled=stopping))
    check('saved order and historical coordinate survive explicit import',
          [row.label for row in current.state.items] == [item['label'], 'Second selected occurrence']
          and (current.state.index, current.state.frame, current.state.accuracy) == (1, 7911, 'historical_cursor'))
    check('old controls and device do not become current settings',
          current.state.controls == PlaybackControls())
    proof_path = store.audio / legacy_playlist_state.PROVENANCE_NAME
    proof = json.loads(proof_path.read_bytes())
    check('current project retains exact raw provenance', proof['queue_utf8'].encode('utf-8') == raw_queue)
    current_pcm = store.paths(current)[0]
    check('current PCM is a separate unaliased retained file',
          current_pcm.stat().st_nlink == 1 and historical_audio.stat().st_nlink == 1
          and (current_pcm.stat().st_dev, current_pcm.stat().st_ino)
          != (historical_audio.stat().st_dev, historical_audio.stat().st_ino))
    backup = work / 'complete-backup'
    backup_pin = phase('complete_backup', lambda: create_backup(restored, backup, cancelled=stopping))
    phase('verify_complete_backup', lambda: verify_backup(backup, backup_pin, cancelled=stopping))
    final_path = phase('restore_complete_under_new_root', lambda: restore_backup(
        backup, work / 'final-library', backup_pin, cancelled=stopping))
    final = open_existing_library(final_path)
    final_store = playlist.PlaylistStore(final.root, imported.project_id)
    final_review = phase('review_relocated_queue', lambda: final_store.review(cancelled=stopping))
    check('relocated managed queue preserves exact typed state', final_review.state == current.state)
    check('relocated project preserves original provenance bytes',
          (final_store.audio / legacy_playlist_state.PROVENANCE_NAME).read_bytes() == proof_path.read_bytes())
    check('relocated history preserves raw queue independently',
          (final.root / DIRECTORY / manifest_pin / 'files' / raw_member.name).read_bytes() == raw_queue)
    measured_size, measured_sha = phase('hash_final_retained_pcm', lambda: _hash(final_store.paths(final_review)[0], stopping))
    check('relocated PCM matches every original byte', (measured_size, measured_sha) == (size, checksum))
    report = dict(status='PASS', fixture='physically_written_pcm16', bytes=size, frames=pcm_bytes // 2,
                  sha256=checksum, selected_mib=args.mib, required_free_bytes=required,
                  observed_initial_free_bytes=available, phases_seconds=timings, checks=checks,
                  historical_backup='PASS', explicit_current_import='PASS', complete_rebackup='PASS',
                  actual_audio_output='NOT_RUN', nvda='NOT_RUN', four_gib_capacity='NOT_RUN')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_json(report))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
