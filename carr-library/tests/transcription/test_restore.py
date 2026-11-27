"""Archived recognition workspaces remain inert through full data restore."""
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.transcription import TranscriptionService


def test_restore_scrubs_archived_launch_state_without_changing_accepted_results(synthetic_asr_runtime,
                                                                             tmp_path):
    runtime = synthetic_asr_runtime
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state/transcription')
    def register():
        inspected = service.inspect_runtime(runtime.python, runtime.worker, runtime.model)
        return service.register_runtime('Synthetic protocol fixture', runtime.python,
            runtime.worker, runtime.model, owner_trusted=True,
            expected_fingerprint=inspected['fingerprint'])
    profile = register()
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic retained source identity.')
    job = service.create_job(profile['id'], [source])
    assert service.run(job['id'])['status'] == 'complete'
    original = service.read_transcript(job['id'])
    service.save_edit(job['id'], job['sources'][0]['id'], 'Окрема збережена правка.')
    edit = service.read_transcript(job['id'], edited=True)
    (runtime.python.parent / 'vcruntime-synthetic.dll').write_bytes(b'Reviewed support identity; never loaded.')
    changed = register()
    service.rebind_runtime(job['id'], changed['id'])
    archive, = (service._job_root(job['id']) / 'worker-history').iterdir()
    transient = ('job_config.json', 'settings_live.json', 'job_status.json', 'pause.flag', 'stop.flag')
    for name in transient:
        if not (archive / name).exists():
            (archive / name).write_bytes(b'Synthetic obsolete control flag.')
    # A file manifest need not recreate empty cache-only directories. Exercise
    # preservation of actual historical user data alongside inert controls.
    historical_log = archive / 'logs' / 'progress.log'
    historical_log.parent.mkdir(exist_ok=True)
    historical_log.write_text('Synthetic archived run log.\n', encoding='utf-8')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranscriptionService(restored_root / 'state/transcription')
    restored_archive, = (restored._job_root(job['id']) / 'worker-history').iterdir()
    assert all(not (restored_archive / name).exists() for name in transient)
    assert all((archive / name).exists() for name in transient)
    assert (restored_archive / 'logs/progress.log').read_bytes() == historical_log.read_bytes()
    assert all(profile['trusted'] is False for profile in restored.list_runtimes())
    assert restored.read_transcript(job['id']) == original
    assert restored.read_transcript(job['id'], edited=True) == edit
    assert restored.run(job['id'])['status'] == 'complete'
