"""Actual CLI restore reports preserved data without implying job activation."""
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile
import wave

import pytest

from carr_library.__main__ import main
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json


def test_cli_backup_audio_choice_reaches_authenticated_manifest(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic reporting fixture')
    key = sha256(b'synthetic reporting PCM cache').hexdigest()
    audio = app.root / 'state/studio/cache' / (key + '-0123456789.wav')
    audio.parent.mkdir(parents=True)
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 160)
    atomic_json(audio.with_name(key + '.json'), {'schema': 1, 'key': key, 'file': audio.name,
        'sha256': sha256(audio.read_bytes()).hexdigest(), 'duration': .01,
        'sample_rate': 16000, 'channels': 1})
    destination = tmp_path / 'backup'
    assert main(['--data-dir', str(app.root), '--backup', str(destination),
                 '--backup-include-generated']) == 0
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['generated_audio_included'] is True
    assert (destination / audio.relative_to(app.root)).read_bytes() == audio.read_bytes()
    output = capsys.readouterr().out
    assert 'Generated audio stored inside this library is included.' in output
    assert 'External exports, engine/model packs and operating-system credentials are separate.' in output
    assert sha256((destination / 'manifest.json').read_bytes()).hexdigest() in output


def test_cli_legacy_restore_explains_preserved_provider_records(tmp_path, capsys):
    fixtures = Path(__file__).parents[1] / 'fixtures/legacy-backups-5580d4d'
    entry = json.loads((fixtures / 'fixtures.json').read_text())['archives']['v4']
    archive = fixtures / 'v4.zip'
    assert sha256(archive.read_bytes()).hexdigest() == entry['archive_sha256']
    source = tmp_path / 'legacy'
    source.mkdir()
    with ZipFile(archive) as incoming:
        incoming.extractall(source)
    destination = tmp_path / 'restored'
    assert main(['--data-dir', str(destination), '--restore', str(source),
                 '--manifest-sha256', entry['manifest_sha256']]) == 0
    output = capsys.readouterr().out
    assert 'Preserved, not activated — provider-settings:' in output
    assert 'Restored — catalog-and-originals:' in output
    assert 'They have not been opened or downloaded.' in output
    assert not (destination / 'provider-settings').exists()
    assert (destination / 'legacy-backups' / entry['manifest_sha256']).is_dir()
    assert LibraryApp(destination).workspaces()[0].id == entry['workspace']


def test_audio_backup_flag_cannot_silently_launch_another_operation(capsys):
    with pytest.raises(SystemExit) as rejected:
        main(['--backup-include-generated', '--self-test'])
    assert rejected.value.code == 2
    assert '--backup-include-generated requires --backup' in capsys.readouterr().err


def test_application_backup_cancellation_does_not_publish_a_destination(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic cancellation fixture')
    destination = tmp_path / 'backup'
    with pytest.raises(InterruptedError):
        app.backup(destination, include_generated=True, cancelled=lambda: True)
    assert not destination.exists()
